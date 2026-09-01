# tail-crowding-detector: order flow replaces funding, sentiment retired

Given by the user 2026-09-01 ("keep going on tail-crowding-detector next"),
applying the standing crypto-retirement guidance.

**Funding, replaced.** `funding_rate`'s crowding role -- the price of
consensus, an extreme value meaning the crowd is paying to stay in -- is
played honestly by order-flow imbalance from `broker-open-interest` (buy
quantity less sell quantity, over their total, summed per underlying via
`runtime/underlying_open_interest.py`). `FROM_FUNDING` becomes
`FROM_ORDER_FLOW` in `runtime/bot_opinion.py`, and everything downstream
that pattern-matches on that constant (`tail-follow-conviction-model`) moves
with it.

**Sentiment, retired without a replacement.** No Indian sentiment data
source exists. Three sources become two; `tail_crowding_minimum_sources`
(2) now means both, not two-of-three -- an honestly stricter bar, not a
weakened one.

**Cascade, traced before removing anything:**
- `sentiment-reading`'s only other consumer, `idea-generator`, turned out to
  be dead code -- the payloads are drained and their fields never read
  (confirmed against the part's own docstring, which lists its four real
  sources and sentiment isn't one of them). Removed from `idea-generator`'s
  consumes too, which makes `sentiment-reading` fully orphaned.
- `social-sentiment-reader` retired: nothing reads `sentiment-reading`
  anymore.
- `bot-scorecard`, the other type `idea-generator` was draining, IS one of
  its four stated sources ("what the scorecards say is missing") but its
  wiring into gap detection was never finished. Left alone, named rather
  than silently dropped -- a real gap, but a separate task from this
  retirement.

`funding-forecast` now has one consumer left (`leverage-selector`, which
itself may turn out not to need it at all for buy-only options --
un-investigated). `funding-rate-forecaster` can't retire until that clears.
