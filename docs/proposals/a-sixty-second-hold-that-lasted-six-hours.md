# A sixty-second hold that lasted six hours, and the cap that refused the sixth trade

**Written 2026-08-27, after the question "why is nothing opening or closing".**

Nothing had filled since the spine started at 21:38 the evening before.
`paper-fill-simulator` had seen 31 orders in six and a half hours and filled
none; `position-close-detector` had `fills_seen 0` and its 16 open symbols were
all checkpoint restores. Two independent causes, both measured on the running
spine rather than read out of the code.

## 1. A flap report is an event, and it was read as a permanent verdict

`position-sizer` was switched off at 22:17:27 for `hog-under-contention` and was
still off six hours later. It was not alone: thirteen parts went the same way,
all of them between 22:00 and 22:58, and every one of them had flipped four
times inside two minutes just before it went.

`switch-oscillation-damper` is what notices that. It counts transitions per part
and publishes a `FlapReport` carrying `hold_for_seconds` — 60 at the base, and
larger the more a part flaps, so a part that keeps oscillating is held longer
rather than at a fixed penalty. The report also carries `observed_at_ns`.

`switching-planner` read neither:

    if part_id in inputs.flap_reports:
        return "flapping"

Presence, and nothing else. And `flap_reports` is a `LatestByKey` built with no
`maximum_age_seconds`, so the report stayed in the mapping for the life of the
process. A hold the damper asked to last a minute lasted until the spine was
restarted.

**This is the same defect the governor already fixed once, in a new place.** On
2026-08-26 an unbounded `LatestByKey` over `part-resource-usage` meant every part
the governor had ever switched off still looked like it was running — 214 of
them — so no shed part could be observed gone and none could come back. The
question CLAUDE.md draws from that is *what makes this level's keys go away*, and
for `flap-report` the answer is not an age bound at all: the report states its
own expiry. Reading `hold_for_seconds` against `observed_at_ns` is the only
reading that honours what the damper actually asked for, and it is what
`_flap_hold_is_still_running` now does.

The cost was not the thirteen parts. It was that `position-sizer` was one of
them, and with the sizer gone nothing could turn a trade intent into an order at
all — the same shape as the eight hours of 2026-08-25, when the ratchet left
`order-book-reader` and `tick-size-resolver` off and no order was placed.

The evidence, replaying the damper's own thresholds (4 transitions, 120s window)
over the spine's switch log: fourteen parts crossed the flap threshold after the
21:38 start, and thirteen of them are exactly the thirteen the heartbeat table
listed as silent. The fourteenth's last flip was an `on`, so it was running.

## 2. Five trades at full size, and the sixth was refused

With the sizer alive the chain still produced nothing tradeable.
`trade-capital-bounds-gate` had taken 568 sized orders and passed **zero**,
refusing all 568 as `not-tradeable` — which is the sizer's own refusal carried
forward. The sizer's standing said why:

    actionable_intents_seen 2338    sized 0
    missing_entry_price     1358
    refused_no_risk_allowed  531    (= refused_by_exposure-limiter)
    missing_stop_price       436

`risk_maximum_total_fraction` was 0.05 against a per-position cap of 0.01 — its
own note says it plainly: *"five trades at full size is the point at which
nothing new is opened"*. The book was carrying 13.1% of the allotment in risk
against that 5% cap, so every symbol read `NO_RISK_ALLOWED`.

**The operator's instruction, 2026-08-27: how many trades may be open at once is
the bot's decision — twenty, forty, or more if it judges them worth taking.**
So the total and per-cluster caps are removed, and `ExposureLimiter` now accepts
`inf` as a cap the operator has taken away.

Three things that are deliberately *not* part of this:

- **Zero is still refused.** "No cap" and "a cap of nothing" are opposite
  instructions, and a zero would refuse every trade while reading like
  permission.
- **`inf`, not a large number.** A cap of 1000% would still bind somewhere and
  would read as an estimate of something nobody estimated.
- **Per-trade bounds stay.** `risk_maximum_per_position_fraction` (1%) still
  sizes each trade and is now the only cap that ever binds, and
  `maximum_capital_per_trade` (100 USDT against a 10,000 USDT paper allotment) is
  still the capital behind one. The count is unlimited; the size of each is not.

The reason string is not left to format an infinity — `{inf:.0%}` prints `inf%`,
which reads like a measurement of something. A removed cap says it was removed.

The segment is on paper money (RL-005). Both caps are to be restored, or
replaced with numbers chosen for real money, before `money_mode` is ever `live`;
the setting notes say so where an operator will actually read them.

## Measured after the restart

    position-sizer      26,146 intents,  sized 6,809,  refused_no_risk_allowed 0
    exposure-limiter    binding cap: per-position on all 2,648 limits issued
    bounds gate         refused_not_tradeable 0  (was 568 of 568)
    paper fills         4 filled, 2 stops triggered, fees charged
    trade lifecycle     trades_started 30, trades_filled 4

`shrunk_by_free_capital` is now 6,807 of those 6,809: the binding constraint is
the capital behind a trade rather than a risk ceiling, which is the honest place
for it to bind.

## Still open

- **`missing_entry_price` is 68% of the intents the sizer sees** (17,754 of
  26,146 on the run after the restart). It traces upstream to
  `instrument-selector`, which refused 399,179 intents for having no recent trade
  or quote and knows a quote for only 104 symbols.
- **`exploration_maximum_open_pairs` is 1.** That is a separate, deliberate
  budget — a long from one bot against a short from another, opened to buy
  information rather than expectancy — and it is left alone here rather than
  swept up with the risk caps.
- **`refused_already_filled` 31 and `cancels_for_an_unknown_order` 2** on the
  paper book, unexplained.
