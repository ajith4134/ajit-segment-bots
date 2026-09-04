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

## Ranking by messages was the wrong ranking

The audit ranks by message count, and acting on that ranking directly would have
been a mistake. Measured per part with `/proc`, message volume and CPU cost do
not line up at all:

    part-appetite-meter      15.5% of messages   0.070 cores
    tick-size-resolver       13.0% of messages   0.023 cores
    intra-bar-fill-sequencer  not in the top 20  0.735 cores
    fill-volume-capper        not in the top 20  0.725 cores

**`part-appetite-meter` is not a defect.** The whole `part-resource-usage` loop --
the meter plus all four consumers, `hog-detector`, `duty-cycle-planner`,
`switching-planner` and `off-state-verifier` -- costs **0.125 cores end to end**,
1% of the box, to meter 318 parts once a second. A change check cannot help it:
sampled directly, **0 of 12** consecutive readings were identical, because every
part's CPU rate moves in the low bits every sweep. That is precisely the shape
`PacedPublisher` exists for, and the meter already paces itself with a tick floor
at `part_usage_cadence_seconds`. The only remaining levers are a slower cadence,
which makes the governor slower to see a hog, and one snapshot message instead of
318, which changes the payload and all four readers. Neither is worth 1% of a
box, so it was left alone.

**The two parts that were actually expensive published nothing.**
`intra-bar-fill-sequencer` and `fill-volume-capper` were 1.46 cores, 27% of the
spine, between them -- both consumers of `historical-window`, walking every bar of
every window that arrived. `historical-bar-store` was restating those windows
under the shared 1 s refresh: **`window_changes` 77 against `window_refreshes`
70,699**, each carrying up to 1,440 bars. And `walk-forward-splitter` reported
`refused_gappy_windows: 70,776` -- it refused every single one, so the whole 1.46
cores produced nothing that could ever become a split.

Reading a level publisher's own standing is what found it. A part can be the most
expensive thing on the machine and appear nowhere in a ranking of publishers.

## What changed, and what it cost

| part | lever | before | after |
|---|---|---|---|
| `historical-bar-store` | own refresh, 30 s against a 60 s bar | 70,699 refreshes | 12 |
| `intra-bar-fill-sequencer` + `fill-volume-capper` | downstream of the above | 1.55 cores | off the top 8 |
| `correlation-cluster-mapper` | own remap interval, 15 s | 0.404 cores | 0.119 |
| `tick-size-resolver` | first change check it has ever had | 1,204 msg/s | off the top 8 |
| `failing-part-detector` | own refresh, 4 s inside a 10 s bound | 997 msg/s | off the top 8 |
| `part-appetite-meter` | none -- measured, not a defect | 0.070 cores | unchanged |

    spine   5.32 cores, 9,731 msg/s  ->  3.41 cores, 4,646 msg/s

`correlation-cluster-mapper` is worth its own note: the first fix tried was
`regime-classifier`'s -- forget symbols nothing is feeding -- and it did nothing,
`symbols_forgotten_silent` 0 with the universe growing 467 to 548. The symbols
were not silent; prices were arriving for all of them, and `subjects_gone_quiet`
correctly declined to drop any. The cost was quadratic in a genuine universe, so
the lever was the cadence: `correlation_window_length` is 256 observations, about
an hour, and it was recorrelating every pair once a second.

## expiry-day-zero-to-hero-detector: 0.957 cores to judge the same thing repeatedly

Once the storms were gone this was the largest single cost on the spine by a
factor of three, and it had fired nothing. Its tick judged **every instrument the
catalogue carries** -- 102,940 of them -- on every wake, and the first thing
`detect` asked was whether the contract expires today, which
`_is_expiry_today` answered by converting two timestamps to dates. About 200,000
date conversions a tick, to reach the answer it had reached the tick before.

An expiry date is a property of the contract and cannot change between ticks. So
it is decided once, when the listing arrives, and the listing is filed under the
date it expires; the tick asks the index what expires today. `instruments_known`
and `instruments_expiring_today` both go on health, so a quiet day reads as
"nothing expires today" rather than as a stopped detector.

**The live part could not be made to answer for itself.** After the fix it ran ten
minutes with `listings_seen` at zero: `broker-instrument-catalogue-reader`
restates the master on its own long interval, and the detector's process had been
restarted after the last burst. So the cost was measured directly instead, against
the real NSE master, at the 102,940 rows the live catalogue carries
(`measure_the_expiry_sweep.py`):

     live expiries      walked    every instrument   todays expiries
                 1      91,388            0.326 c           0.320 c
                 2      45,762            0.330 c           0.172 c
                 5      18,308            0.331 c           0.068 c
                10       9,222            0.364 c           0.031 c
                20       4,698            0.347 c           0.014 c
                40       2,436            0.339 c           0.007 c

The old sweep costs the same whatever the master looks like, because it walks all
of it. The new one costs whatever today's slice is. NSE carries weekly index
expiries beside monthly stock ones for many underlyings at once, so ten or more
live expiry dates is the row to read: **0.33 cores to 0.03**.

The first version of this script reported no saving at all, because it built its
catalogue by repeating one captured NIFTY ladder verbatim -- which puts 89% of the
instruments on a single expiry date, a shape the real master does not have. How
many contracts actually expire on a given day is the one thing this measurement
does not know, so it reports the curve rather than guessing a row.

The systemd accounting agrees with the diagnosis. Across this part's earlier
lives, whenever it had the catalogue: `56min 29s CPU over 59min 55s` and
`57min 7s over 1h 3s` -- 0.94 and 0.91 of a core. The benchmark's 0.33 is one
sweep a second; the live part is woken by its inputs and swept about three times
that.

## Re-running it

    .venv/bin/python measurements/2026-09-04-what-burns-cpu-with-no-trades/audit_level_publishing.py

`audit-before-the-fixes.txt` is the run above, taken while the defects were live;
`audit-after-the-fixes.txt` is the same probe once they were deployed.
