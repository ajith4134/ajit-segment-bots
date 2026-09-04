# What burns CPU and RAM with no trades — 2026-09-04

The question: the box sat at load 10.6 of 12 cores and 26.7 GB of 29 GB for ten
hours with the Indian market shut since 15:30 IST and no trade ever placed.

## What was actually happening

**The spine was being OOM-killed roughly hourly and nobody had noticed.**

    journalctl --user --since "-7 days" | grep -c "oom-kill\|OOM killer killed"
    115

    Sep 04: 05:34, 05:56, 06:05, 06:28, 06:31, 07:21, 07:21, 08:36,
            09:48, 10:48, 14:23, 14:43, 15:47, 15:59      -- fourteen times

`ajit-spine.service: Failed with result 'oom-kill'` each time. The supervisor
restarts, 315 parts come back, and the cycle runs again. Load average is partly
that: 483 governor switch-flips in the hour sampled, 242 off and 241 on, each
restart re-importing numpy into a fresh process.

## The RAM: one part, 10 MB/s

    10.04 MB/s   13,866 MB   instruction-replayer
     0.52 MB/s    1,737 MB   regime-transition-tagger
     0.11 MB/s      330 MB   counterfactual-replayer
     0.10 MB/s      331 MB   near-miss-recorder
     0.10 MB/s      333 MB   bull-exit-plan-proposer
     0.10 MB/s      333 MB   bear-exit-plan-proposer
    total 11.24 MB/s = 40.4 GB/hour, on a 29 GB box

`instruction-replayer` had replayed nothing -- every counter in its standing read
zero throughout. **A part can be idle and fatal at the same time.**

### The mechanism, and the wrong first answer

`historical-bar-store` built a window per symbol once per health interval and
stamped each one `f"window-{self._sequence}"`. A counter, so the same unchanged
window got a **new identity every second**, each carrying `backtest_window_bars`
(1,440) bars. `instruction-replayer` keyed a `LatestByKey` on `window_id` and so
retained every restatement.

It leaked hardest with the market **shut**: nothing changes, so every second's
window is the same one under a new name.

The obvious fix -- give that assembly a `maximum_age_seconds` -- **would not have
worked**. `LatestByKey.mapping()` filtered expired keys out of the copy it
returned and left `_by_key` whole, so a bound answered staleness and never
growth. The two are the same question whenever the key space is not finite.

Three changes, all needed:

1. `historical-bar-store` derives `window_id` from what the window *is* (venue,
   symbol, interval, range, holes), so restating it is recognisable as a
   restatement.
2. It publishes through `LevelPublisherByKey`, so an unchanged window is not put
   on the bus again until its refresh is due.
3. `LatestByKey` **drops** an expired key rather than only hiding it, counted on
   `expired_keys` so the dropping stays visible.

A sweep for the same shape elsewhere found 16 assemblies keyed by something with
no finite key space and no bound; 12 are now bounded, and the 4 left carry the
reason in the code (their producers publish on an event rather than restating a
level, so a bound would silently retire something still live).

## The CPU: one part, 74% of all messages

    part                        published    received   per recv   level pub
    regime-classifier          61,750,997       2,013     30676x   yes
    signal-excursion-profiler   6,383,689           0   no input   yes
    execution-cost-model        2,490,406      14,321       174x   no
    part-appetite-meter         2,334,235           0   no input   no
    tick-size-resolver          1,747,849       9,319       188x   no
    historical-bar-store        1,433,472      14,208       101x   yes
    correlation-cluster-mapper    664,509         639      1040x   yes

`regime-classifier` held **3,209 symbols, 3,208 of them restored from a
checkpoint written in the crypto era**, and published a regime for every one of
them once per health interval. 99.2% of those classifications said
"unclassified", because the symbols they named had no prices at all -- the part
had received 1,190 of them.

Two changes: a symbol whose series has gone silent past its own gap bound is
dropped (the rule lives on `RollingWindow`, because the window's next print would
clear that series anyway), and the regime publishes through `LevelPublisherByKey`
so an unchanged one is not restated on every burst wake.

## The finding worth keeping from the audit

**The keepalive refresh is per key.** A part holding N keys publishes N messages
per `level_refresh_interval_seconds` forever, changed or not. That is why
`signal-excursion-profiler` publishes 6.4M messages while receiving nothing and
while its level publisher is correctly installed: it holds several hundred keys
and refreshes each once a second. The change-check only removes the traffic
*above* that floor.

So the lever on a storming part is usually **how many keys it holds**, not
whether it has a change check. `regime-classifier` went from 3,209 keys to the
live universe; that, not the change check, is what removes the 74%.

Parts naming a level-publishing shape: **17 of 343**. The audit script ranks the
rest by what they actually cost, so the next one to fix is measured rather than
guessed.

## signal-excursion-profiler, the one the audit named next

With the regime storm gone it was 43% of everything left: **13,044,004 messages
published while receiving no labels at all**. 838 keys -- 419 symbols on both
sides, every one of them restored from a checkpoint -- refreshed once a second,
times the five parts that consume `excursion-profile`.

**Its fix is deliberately not regime-classifier's.** Dropping keys is right for a
regime, whose window is rebuilt from a few minutes of prices; it would be wrong
here, where the distributions took 52,958 settled claims to fit and are
checkpointed precisely so a restart does not throw them away. What was wrong was
only the cadence, so only the cadence changed:
`excursion_profile_refresh_interval_seconds`.

Ten seconds is safe because **every consumer keeps what it drains** --
`profit-lock`, `stop-target-placer`, both exit-plan proposers and
`tail-trailing-exit-planner` each read the type through `Batch` and store it per
symbol (`bull_exit_plan_proposer.py:224`). So the refresh is what a cold reader
needs once, not a cadence anything depends on. That cold-start wait is the real
cost and is why it is not longer: an exit-plan proposer refuses to plan without a
fitted profile, and a position is open while it waits.

    signal-excursion-profiler   3,815 msg/s  ->  374 msg/s   (1st -> 7th)
    spine total                15,630 msg/s  ->  6,706 msg/s
    unchanged_profiles_skipped                     268,160

That last number is the point of putting it on health. A change check whose skip
count reads zero is a change check doing nothing, and it looks exactly like one
that works -- which is how this part's own `identity_of` was found wanting once
already.

**What the fix exposed.** With the storms gone the largest single CPU consumer on
the box is `part_health_api` at 1.03 cores -- the board, which is not a part. And
the top of the audit is now `broker-instrument-catalogue-reader` (18%) and
`part-appetite-meter` (17%), both publishing steadily while receiving nothing.

**A note on the restart.** The spine sheds hard during its start-up spike and the
governor brought every part back: 135 parts at 17:41, 318 by 17:48, 160 `on`
flips against 17 `off`. The shed reason was `conserving-a-short-runway` -- the
survival tier, bound by LLM quota in 37,935 of 38,244 readings, from a
`subscription-quota-watch` that has never seen a provider header. Worth its own
look: the tier that governs conservation is decided by a measurement nobody has
wired a key for.

## Re-running it

    .venv/bin/python measurements/2026-09-04-what-burns-cpu-with-no-trades/audit_level_publishing.py

`audit-before-the-fixes.txt` is the run above, taken while the defects were live;
`audit-after-the-fixes.txt` is the same probe once they were deployed.
