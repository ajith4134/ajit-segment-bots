# Broker price level sampler

Given by the user 2026-09-01 ("keep going into that audit now" — the
options segment bots' consumes/produces audit), designed against the
crypto build's own `price-level-sampler` precedent, applied by
`dashboard/blueprint_edits/apply_2026-09-01_broker_price_quote_samplers.py`.
1 part, 1 data type added to `broker-adapter`; `expiry-day-zero-to-hero-
detector`'s consumes redirected from `symbol-price-frame` to
`broker-price-frame`.

`broker-quote-level-sampler`/`broker-quote-frame` (the plan's original
second half) are deliberately deferred — see "What this does not do yet"
below.

## Why this is the first cut of the audit, not a side detour

Auditing `opportunity-scanner`'s 9 surviving detectors (§ the retirement
slice) against their consumes shows `symbol-price-frame` and
`symbol-quote-frame` — crypto's throttled, cadence-published views of raw
ticks — appearing in nearly every one of them, and in `bull-bot`,
`bear-bot`, `profit-tailgating-bot`, `ai-brain` and `instrument-selector`
too. Both are produced by dedicated sampler parts
(`price-level-sampler`/`quote-level-sampler`) living in crypto's
`market-data-feed` category, not inline in each consumer — the exact
reason stated in `price-level-sampler`'s own docstring: 37 parts reading
only symbol/price/moment from every trade, so the sampling happens once,
not 37 times, and downstream delivery volume stops scaling with trading
volume or universe size.

Nothing downstream can be swapped to a real Indian type until that same
substrate exists for Indian data. This is that substrate — the
dependency-ordered first cut, not a detour from the audit.

## Design, mirroring the crypto parts' own shape

- **`broker-price-level-sampler`**: consumes `broker-market-data` (the
  `LtpUpdate` stream already live from `broker-market-feed-reader`),
  produces `broker-price-frame` on a cadence. Same frame-splitting
  discipline as `price-level-sampler` (the bus refuses a datagram over
  131,072 bytes) even though Phase A's ~200-symbol universe is nowhere
  near crypto's 2,590 symbol-venue pairs — the same part shape, T-1,
  rather than a special case for being smaller today.
- **Broker-scoped (`broker_id`), not venue-scoped** — same concept
  crypto's `venue_id` fields carried, renamed to match this domain's own
  vocabulary rather than reusing the crypto field name on a type that
  means something structurally different.

`expiry-day-zero-to-hero-detector` (declared in the retirement slice,
not yet implemented) is redirected from `symbol-price-frame` to
`broker-price-frame` and implemented in this same edit — a part this
session owns end to end, so closing `broker-price-frame`'s R-01 loop
here rather than surgically modifying a mature crypto part's real code
(`regime-classifier`: 240+ lines, checkpointing, settings-driven
thresholds) to do it. That audit is still ahead, not skipped.

## What this does not do yet

- **`broker-quote-level-sampler`/`broker-quote-frame`**: no real
  quote-consuming part is ready to pair with it in this same edit —
  declaring the producer alone would itself be an orphan output.
  Deferred to whichever later slice first swaps a real quote consumer
  (`spread-reversion-detector`, `instrument-selector`, ...).
- The actual crypto→broker swap for `opportunity-scanner`'s other 8
  detectors, `bull-bot`/`bear-bot`/`profit-tailgating-bot`/`ai-brain`'s
  parts, and `instrument-selector` — real, mature code, real risk if
  rushed. Now unblocked (the substrate exists) but not done here.
