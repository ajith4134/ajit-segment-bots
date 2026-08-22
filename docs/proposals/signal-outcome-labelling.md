# Proposal: `signal-outcome-labeller` — teaching the models before any trade exists

**Proposed by Claude, 2026-08-22, and agreed by the user the same day.**
Blueprint edit: `dashboard/blueprint_edits/apply_2026-08-22_signal_outcome_labelling.py`.

## The finding this answers

Phase 3 set out to carry a live trade from the venue feed to a simulated fill.
Wiring the parts surfaced a cycle with no entrance:

    trade-intent        needs a directional-opinion
    directional-opinion needs a calibrated conviction from bull-conviction-model
    bull-conviction-model needs training-label
    training-label      is produced only by label-builder
    label-builder       needs closed-trade
    closed-trade        needs a position, which needs a fill, which needs a trade

Measured, not inferred. An untrained `OnlineLearner` returns exactly 0.5 —
`logistic(0)` with every weight at zero — and reports `is_fitted` false; before
`minimum_feature_observations` values of a feature exist it cannot standardise one
at all and returns `features_used == 0`, which the model reports as *nothing
usable* rather than as a conviction. So the composer forms no opinion, the arbiter
receives none, and nothing downstream ever runs.

This is not a defect in any part. Each one is refusing correctly: none of them will
claim a measurement it does not have. **The gap is that the blueprint's only source
of `training-label` is the outcome of a trade, and the system cannot make its first
trade without one.**

## What is proposed

One part, in `learning-loop`:

| | |
|---|---|
| **id** | `signal-outcome-labeller` |
| **consumes** | `entry-candidate`, `market-data` |
| **produces** | `training-label`, `part-health` |
| **role** | score whether a detector's expected move happened inside its own horizon |

A detector's candidate already states everything a label needs: the direction it
expects, the size of move it expects, and the horizon it expects it in. The prices
that then arrive say what happened. The label is the comparison — and it requires
no trade, no position, no fill and no capital.

**It learns from the live feed, not from a replay (RL-071).** This part sits on the
bus like every other: it reads the `entry-candidate`s the detectors raise now and
the `market-data` the venues are sending now, and it scores the one against the
other as the horizon elapses. Nothing here reads the tape. The user's rule is that
the bot trades on live prices exactly as it would with real money, and a system
that trained on replayed history while claiming to trade live would be a backtest
wearing a bot's clothes.

What that costs is time rather than correctness: labels accrue at the rate the
detectors actually fire, so the model becomes trained when the market has shown it
enough, and not before. The tape stays what it has always been — the durable record
of what the venues sent, and the fixture the real-data tests run on (RL-063).

## Why this is not `label-builder` doing more

`label-builder` turns a **trade's** outcome into a label: it consumes
`closed-trade`, `peak-excursion` and `cost-estimate`, so its label is net of what
the trade actually cost to enter and leave. A signal's outcome has no cost basis,
no fill price and no holding period — it is a different measurement of a different
thing, and merging them would put two responsibilities in one part, which T-6
forbids and which would make a model unable to tell which kind of label it was
learning from.

They also disagree deliberately. A signal can be right while the trade that
followed it loses money, and the difference between the two is exactly what
`closed-trade-decoding` exists to explain. Keeping the labels separate is what
makes that difference visible instead of averaged away.

## Why not `near-miss-recorder`

`near-miss-recorder` already consumes `entry-candidate` and `market-data` and
produces `near-miss-episode` — a candidate that was raised and not traded, with
what the market then did. That is close, and it is not the same:

- it exists to make **abstentions** teach, so its subject is the decision not to
  trade, not the detector's claim;
- it produces an episode, not a label, and nothing trains on it;
- it also consumes `trade-intent` and `directional-opinion`, which are exactly the
  types that do not exist during a cold start.

## What it does not do

- **It does not invent data.** The label comes from candidates the detectors
  actually raised and prices the venues actually sent, as they arrived. RL-063
  holds: real data, never a fixture. RL-071 holds: live prices, never a replay.
- **It does not make the model good.** It makes the model *trained*, which is the
  difference between a conviction that is a measurement and one that is a starting
  point. Whether the resulting model has an edge is what the backtesting block and
  the promotion gates exist to answer, and neither of them is relaxed by this.
- **It does not shorten the path to live money.** Paper first (RL-005), and every
  gate between paper and live stays where it is.

## The honest caveat

A signal outcome is a **proxy** for a trade outcome. It ignores the spread, the
fees, the slippage and the fact that a position has to be exited. A model trained
only on signal outcomes will be systematically optimistic about how much of a
predicted move it can keep.

That is acceptable as a bootstrap and not as a destination. As real closed trades
accrue, `label-builder`'s labels arrive alongside these, and the model learns from
both — with the difference between them being a measurement worth having rather
than an error to hide. The labels this part produces carry their source so that
difference can be seen rather than assumed.
