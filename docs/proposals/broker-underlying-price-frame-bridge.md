# broker-underlying-price-frame-bridge

Given by the user 2026-09-01 ("do what is best... according to the design
and your decision", picking up the options-segment-bots audit). Closes the
gap identified against `docs/superpowers/specs/2026-09-01-options-segment-
bots-design.md` section 4: five opportunity-scanner detectors are already
segment-agnostic and need no code change, but nothing publishes
`symbol-price-frame` for the index-options segment's underlyings (NIFTY,
BANKNIFTY, SENSEX). `broker-price-frame` (already built) is deliberately a
separate type from `symbol-price-frame` (docs/proposals/broker-price-quote-
samplers.md), so this part is the explicit bridge R-01 requires.

Consumes `broker-instrument-listing` (to resolve which instrument_key is
which tracked underlying, by trading_symbol) and `broker-price-frame` (the
live prices). Produces `symbol-price-frame` -- reusing the crypto type
verbatim, not a new one, so `regime-classifier`, `mean-reversion-detector`,
`cointegration-pair-finder`, `spread-reversion-detector` and
`momentum-burst-detector` need zero changes.

**Not standing up a running index-options segment.** `segment_id` is one
global runtime setting today, set to `futures`; this part is declared and
tested in isolation and is not added to `operate/run_live_spine.py`. That is
a separate, larger question (concurrent multi-segment execution) explicitly
deferred to a later plan, per spec section 6.
