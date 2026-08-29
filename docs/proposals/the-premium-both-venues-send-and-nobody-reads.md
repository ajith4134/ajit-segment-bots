# The premium both venues already send, and the forecaster that has never seen one

**Proposed 2026-08-28, after tracing why the tailgating bot has never opened a
position.**

## What is broken

`funding-rate-forecaster` has been running on the live spine for its whole life
and has produced nothing:

    market-data received      5,017,806
    forecasts_made                    0
    premium_observations              0
    refused_no_premium_observations   0
    refused_no_venue_parameters       0

Not one refusal either — it never reaches the code that would refuse. Its
`start_part` reads `market-data`, calls `trades_in(...)`, discards the result and
returns an empty tuple of symbols, so `forecast()` is never called for anything.
The docstring says why, and says it honestly:

> The premium a funding rate is averaged from is mark minus index, and neither is
> on market-data, which carries trades, candles and books. With no premium to
> observe the forecaster forecasts nothing and its standing says so: a blueprint
> gap (RL-062), not a forecast of zero funding.

That was the right call when it was written. It is no longer the true state of
this system, because the premium arrived in the codebase afterwards and nothing
connected it.

## What this costs, which is not only the tailgater

`funding-forecast` is consumed by **six** parts:

| part | what it cannot do without one |
|---|---|
| `tail-crowding-detector` | one of its three crowding sources, and it needs two |
| `bull-feature-builder` | a feature on every vector it builds |
| `bear-feature-builder` | the same |
| `funding-skew-detector` | its entire reason to exist — it detects funding skew |
| `leverage-selector` | the carry cost of holding leverage overnight |
| `cross-segment-signal-bridge` | one of its four inputs |

Measured on 2026-08-28, `tail-crowding-detector` reported `NOT_MEASURED` for all
37 candidates it was ever handed — funding unavailable 37 times of 37 — which is
why `tail-follow-conviction-model` formed zero convictions and the tailgater has
never published an opinion.

## Both venues already send it, and one adapter method already reads it

`VenueAdapter.read_premiums` is abstract and implemented on both venues. Measured
against the venues' own documentation and this codebase's readers:

| | binance-usdm | bybit-linear |
|---|---|---|
| topic | `!markPrice@arr@1s` — every listed symbol in one frame | `tickers.<symbol>` |
| measured | 740 symbols in a single message, once a second (2026-08-26) | one symbol per frame |
| mark / index | `p` / `i` | `markPrice` / `indexPrice` |
| declared next rate | `r` | `fundingRate` |
| already subscribed? | no — its own stream | **yes**, this is the topic `venue-quote-stream-reader` reads for quotes |

So one venue costs one more stream and the other costs nothing at all, and the
code that parses both was written and has never been called by anything.

## The change to the blueprint

**One new part and one new data type**, which is the shape T-6 asks for — grow by
adding a part, never by making a part cleverer:

    venue-premium-stream-reader
        consumes  stream-plan, venue-standing
        produces  venue-premium, part-health

    funding-rate-forecaster
        consumes  market-data, venue-premium        (+ venue-premium)

The new part is the fourth of the same shape as `venue-trade-stream-reader`,
`venue-quote-stream-reader` and `order-book-reader`: it reads a plan, opens the
venue's socket, and turns messages into one named data type. It carries no
judgement, so under RL-060 it stays deterministic transport with no learned
component.

`market-data` stays on the forecaster's consumes. It is what tells the forecaster
which symbols are live at all, and dropping an input in the same edit that adds
one is how a part quietly loses a capability nobody was watching.

## What this deliberately does not do

- **It does not make the premium a level on `market-data`.** That wire already
  carried trades and candles, and a wire with one name and several payload shapes
  is what defeated both contract checkers on 2026-08-25 — a candle crashing a
  reader of trades is the same defect this would re-create with a premium.
- **It does not read `P`, Binance's estimated settlement price, as the index.**
  That is the venue's own forecast; a premium computed against it is a premium
  against a number the venue made up rather than against the basket it marks to.
  The adapter already refuses to, and this proposal keeps that.
- **It does not touch the declared funding rate on `symbol-universe`.** That is a
  listing fact and a different proposal
  (`venue-declared-funding-facts.md`). A declared rate says what the venue will
  charge at the next settlement; the premium says what it will charge at the one
  after. Both are wanted and they are not substitutes.

## How this will be verified

- The forecaster's `premium_observations` and `forecasts_made` climb off zero on
  the live spine, and `funding-forecast` appears in some part's published counts.
- `tail-crowding-detector`'s `sources_unavailable.funding-is-the-price-of-consensus`
  stops climbing, and `readings` produce something other than `NOT_MEASURED`.
- `check_contracts.py`, `check_payload_reads.py` and `check_part_calls.py` hold.
