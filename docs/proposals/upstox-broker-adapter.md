# Broker adapter (Indian markets) — first parts

Given by the user 2026-09-01 (the crypto→India conversion in `docs/goal.md`),
designed in `docs/superpowers/specs/2026-09-01-upstox-adapter-design.md`,
applied by `dashboard/blueprint_edits/apply_2026-09-01_upstox_broker_adapter.py`.
4 parts, 1 block, 7 data types added.

## Scope

Only the read-side substrate: keep a session token valid, know what's
tradable, stream live prices, write the tape. RL-068's own build order —
substrate, then market-data-feed, then the governor spine, then the trading
vertical — applies here exactly as it did for crypto, so this edit does not
reach into order placement or margin. Those are declared later, when this
segment actually moves toward live orders and has something real to wire
them into; declaring them now would mean inventing a consumer that doesn't
exist yet just to satisfy R-01, which is the placeholder-graph the no-
placeholder discipline (RL-062) exists to prevent.

## Why the parts are broker-agnostic

Every part id below names no broker. This matches the existing
`execution-venue-adapter`/`market-data-feed` parts exactly — `venue-trade-
stream-reader` never became `binance-trade-stream-reader`; which venue it
talks to is a settings choice, resolved through an adapter registry, not
baked into the part's identity (T-1, T-4). The same shape applies here: these
four parts are the first of what the goal's six-broker build order will run
against five more adapters later, each satisfying the same `BrokerAdapter`
contract (spec §7) without the parts themselves changing at all.

## Why new data types instead of reusing `market-data`/`candle`/`order-book-snapshot`

Those three already exist and are shaped compatibly close to what a broker
feed produces. They were not reused here anyway, for the same reason this
project split `candle` out of `market-data` in the first place: reusing a
type id wires a new producer straight into every existing consumer of that
type by R-01's own mechanism, and every current consumer of `market-data`
(`feed-gap-detector`, `cross-venue-price-consolidator`, `segment-bot`, …) was
built and tested against a crypto venue's shape and semantics. Feeding them
an Upstox record silently — correct-looking JSON, wrong assumptions
underneath — is exactly the "one wire, three shapes" failure this project's
own history warns about. New, broker-scoped type ids keep this edit fully
self-contained; unifying the two domains' types, if it ever makes sense, is
a decision for whenever the crypto side is actually retired, not a side
effect of adding an adapter.

## The parts

| Part | Reads | Writes | Does |
|---|---|---|---|
| `broker-token-refresh-scheduler` | — (settings + secrets) | `broker-token-standing` | runs once daily before market open; TOTP auto-login (spec §3), persists the day's token |
| `broker-instrument-catalogue-reader` | — (broker's public instrument files) | `broker-instrument-listing` | daily fetch + parse of the broker's instrument master (spec §4) |
| `broker-market-feed-reader` | `broker-token-standing`, `broker-instrument-listing` | `broker-market-data`, `broker-candle`, `broker-order-book-snapshot`, `broker-open-interest`, `broker-option-greeks` | opens the broker's feed, decomposes one bundled message per instrument into its separate record kinds (spec §5) |
| `broker-market-tape-writer` | all five kinds above | — | persists every record to the tape before anything else reads it, mirroring "history accrues only in real time" for this segment too |

`broker-market-feed-reader` consuming its own token/instrument-list inputs
rather than reading them directly off disk itself is the same T-2 discipline
the crypto adapters already follow — a reader is handed what it needs by a
declared producer, not by reaching around the data plane.

## What's still not wired

`broker-market-data`/`candle`/`order-book-snapshot`/`open-interest`/`option-
greeks` currently have exactly one consumer each: the tape writer. Nothing
in this edit reads the tape back out, scores an opportunity against it, or
routes an order — that's the segment-bot vertical for Indian markets, not
yet designed. The board will show these four parts at `DECLARED` and no
further, honestly, until code and a real downstream exist.
