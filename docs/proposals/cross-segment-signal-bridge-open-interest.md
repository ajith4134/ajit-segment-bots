# cross-segment-signal-bridge: open interest replaces whale/funding, 6-segment vocabulary

Given by the user 2026-09-01 ("keep going on cross-segment-signal-bridge
next"), applying the standing crypto-retirement guidance.

**Two problems, traced before touching anything:**

1. `WHALE_FLOW` (on-chain transfers) and `FUNDING_SKEW` (perpetual funding)
   are crypto-only signal kinds with no honest Indian equivalent by name.
   `FUNDING_SKEW`'s *role* -- an unusual, forwardable observation about crowd
   positioning -- does have one: `OPEN_INTEREST_SURGE`, built from
   `broker-open-interest` the same way the bull/bear feature builders
   already read it (`runtime/underlying_open_interest.py`). `WHALE_FLOW` has
   no analogue at all -- deleted, not replaced.
2. The part's whole segment vocabulary (`FUTURES`/`SPOT`/`OPTIONS`, the old
   3 crypto segments) is hardcoded into `RELEVANT_TO` and `start_part`'s
   `others` computation. Moved to the real 6:
   `index-options`/`stock-options`/`index-futures`/`stock-futures`/
   `commodities`/`cash-equity`.

**`PRICE_DISLOCATION` and `POSITIONING` were already segment-agnostic** and
needed no change beyond the vocabulary swap -- `PRICE_DISLOCATION` becomes
the honest carrier of what funding skew used to proxy for anyway, once
index-futures exists: the same underlying priced differently in a
derivative than in the market it settles against is a basis, not a guess.

**A real bug found and fixed along the way:** `observe_whale_transfer`'s and
`observe_funding`'s return values -- the actual computed `CrossSegmentSignal`
objects -- were discarded in `start_part`. Those two signal kinds never
reached the bus in the original crypto implementation either. Fixed by
having `read_observations` return directly-observed signals alongside its
request tuples, and `tick()` publish both.

**Cascade result:** `whale-transfer-reader`'s only consumer was this bridge.
Once its consumes moved off `whale-transfer`, nothing reads that type at
all -- the producer is retired too, same cascade discipline as the earlier
options-scanner-first-slice. The `whale-transfer` data type stays declared
(undeleted), matching how `onchain-flow` was left in place when
`onchain-flow-aggregator` retired.

**Not done here:** `cross-segment-lesson-bridge` shares the same old
`FUTURES`/`SPOT`/`OPTIONS` vocabulary and needs the identical relabeling --
separate part, separate slice. `funding-forecast` still has two real
consumers (`leverage-selector`, `tail-crowding-detector`); `funding-rate-
forecaster` can't retire until both clear.
