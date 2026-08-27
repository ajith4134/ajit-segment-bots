# Four parts that needed an input nobody had given them

**Written 2026-08-26, after fixing the instruction chain surfaced four more defects.**

All four were found by measuring the live spine rather than by reading code, and
three of them share one shape: **a part's code already reached for something the
blueprint never gave it.** Each was launchable, each reported itself healthy, and
each was quietly producing nothing or producing something wrong.

## 1. A crash that needed a candidate to become visible

`tail-trailing-exit-planner`'s `start_part` ended with

    return tuple((candidate, None) for candidate in candidates.payloads())

and `plan()` opened with `left = remaining.remaining_fraction`. Every candidate
was handed a literal `None` and the attribute was read off it unconditionally, so
the part crash-looped the moment a follow-candidate arrived.

**It had never arrived before.** `tail-mover-qualifier` had seen 1,197
entry-candidates and qualified its first 3 on the day the scanner's vocabulary
was widened. The defect was written long before and only became reachable when
something upstream started working — which is the failure mode this project keeps
meeting, and the reason a part that has never done anything is not a part that
works.

`start_part` even carried the wiring behind a dead guard —
`if "move-remaining" in context.declaration.consumes` — against a declaration that
did not list it. `tail-move-remaining-estimator` had been publishing the estimate
all along, to nobody but the conviction model.

**Now:** the planner consumes `move-remaining` with an age bound, and a candidate
with no estimate is refused by name (`NO_MOVE_REMAINING`) rather than planned
without the check. A tailgater joining a move already running has no target of
its own, so how much of the move is left is the one thing it must not guess at.

## 2. Every training label ever built said the same thing about the market

`SignalOutcomeLabeller.observe_candidate` took `regime_name: str = "any"` and
`start_part` never passed one. So `TrainingLabel.regime` has been the constant
`"any"` on every label the system has produced, and everything that learns per
regime — `SignalCalibrator`, `hypothesis-regime-tagger`, the conviction models —
was pooling regimes it could not tell apart into one number that describes none
of them.

The default also read like a claim about the market rather than an admission
about the reader. It is now `REGIME_NOT_KNOWN`, and it is only used when
`regime-classifier` has genuinely not classified the symbol — its own
`UNCLASSIFIED` state is passed through as not-known rather than as a regime,
because "the estimator could not decide" and "no reading arrived" are one fact
downstream and should not be two words.

The reading is bounded by `regime_reading_maximum_age_seconds`. **Every
`LatestByKey` in this codebase needs that question asked of it** — what makes its
keys go away — because an unbounded one is what made every part the governor had
switched off still look alive to it, and what cost `position-sizer` a fifty-six
minute stale price.

## 3. A wire that carried 544,798 times and carried nothing

`liquidation-cluster-mapper` published 544,798 `liquidation-map` messages in one
run. Every one was `refused-no-margin-schedule`, none held a single cluster, and
its own `maps_published` standing read **0** while the bus counted 544,798.
Nothing ever called `observe_margin_schedule`; the part's own docstring said the
schedule "is not on any input this part declares" and left it there.

Downstream, `liquidation-cascade-detector` ran 844,858 tests and rejected
844,858 of them as `no-cluster-in-reach`, which is the correct answer to an empty
map and indistinguishable on any board from a market with no clusters in it.

**Measured on the venues, 2026-08-26, not taken from a summary:**

    GET /v5/market/risk-limit?category=linear&symbol=BTCUSDT
      -> 35 tiers, empty cursor, one public call, no key
    GET /v5/market/risk-limit?category=linear
      -> 407 rows covering 15 symbols and a cursor: ~56 pages for ~840 symbols
    GET /fapi/v1/leverageBracket
      -> {"code":-2014,"msg":"API-key format invalid."}   SIGNED

So the ladder is read per captured symbol on Bybit (50 calls, not 56 pages for a
universe nothing watches) and in one signed call on Binance.

**The ladder is per symbol, not per venue.** The mapper keyed its schedule by
venue alone, and that is not what either venue publishes: Bybit's BTCUSDT starts
at a 0.33% maintenance margin and its thinner contracts start several times
higher. A venue-wide ladder taken from one symbol would put every other symbol's
liquidation price in the wrong place, and wrong in the same direction for all of
them — which is worse than no map, because a map is acted on.

**The two venues state a tier from opposite ends**, and reading one as the other
is a silent, total error. Bybit gives `riskLimitValue`, a **ceiling** — so a
tier's floor is the ceiling of the tier below, and reading it as a floor would
price every position under the first ceiling with the wrong rate, which is every
position this bot takes. Binance gives `notionalFloor` and `notionalCap`, so the
floor is read directly.

### The signed half, and what is deliberately not done here

`runtime/venues/venue_signing.py` implements HMAC-SHA256 request signing and
reads the key from the environment. **No key is created, stored, or committed by
this change, and none is present on this box.** The convention is the one
`api-key-pool-rotator` already states for this project: the mechanism is complete
and the key set is honestly empty (RL-062).

With no key, Binance's `margin_schedule_requests` returns an **empty tuple** —
not an unsigned request. An unsigned attempt is a request spent against a rate
limit shared with the endpoints that keep the tape running, to be told what is
already known locally. The reason is on the board and names only the variables
that were looked for:

    AJIT_BINANCE_USDM_API_KEY
    AJIT_BINANCE_USDM_API_SECRET

A **read-only** key is sufficient. Nothing in this path places an order, and the
only private endpoint any adapter names is a leverage-bracket read; a key with
trading permission would grant this process authority it has no code to use.
`RequestSigner.__repr__` is overridden because a dataclass prints its fields and
this object is held by adapters that appear in tracebacks — a secret in a
traceback is a secret in the journal.

The two adapter methods are **not abstract**. On 2026-08-25 an abstract
`read_premiums` was added and implemented for Binance only, so
`BybitLinearAdapter` could not be constructed and every part that loads an
adapter crash-looped — invisibly for hours, because the running spine held the
pre-change code in memory. A default that answers "this adapter has no schedule
to offer" cannot do that to a venue nobody has got to yet.

## 4. A staleness bound answering the wrong question

`signal-outcome-labeller` refused **1,053 of 1,397** claims for a stale price —
62% of everything the detectors raised, discarded before it could become a label.

The bound was not wrong; it was answering a different question. `price_staleness_from`
derives it from the round-trip taker fee: *how old may a price be before acting on
it costs more than the round trip does*. That is exactly right for a part about to
size or place an order. This part never does either — it records where price stood
when a claim was made, and its price is contaminated when it has drifted far enough
to distort the barrier the claim will be judged against.

That barrier is `signal_label_move_fraction` = 0.2%, against a 0.11% round trip.
Because the bound goes as the **square** of the materiality, the fee-derived
figure is more than three times tighter than this part's own question warrants:

    fee-derived      (0.00110 / 0.000898)^2 = 1.50 s
    barrier-derived  (0.00200 / 0.000898)^2 = 4.96 s

`price_staleness_from` now takes the materiality from its caller, so a part states
what "material" means for the judgement it is about to make. It is still derived
from a measured quantity — never a number chosen to admit more claims.

## Still open

- **`largest_z` and `largest_burst_z` are updated after every rejection**, so they
  read 0.0 across 660,000 observations. Peak-of-what-fired, not peak-observed —
  there is no way to see how close anything came, which is the number that would
  say whether a threshold is near or hopeless.
- **`check_part_calls.py` did not catch `writer.live_instructions()`**, a property
  called as a method. It follows calls a part makes to its own object; a property
  read is not a call until it is one.
