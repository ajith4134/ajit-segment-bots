# Options segment bots — first slice: retire crypto-only detectors, add zero-to-hero

Given by the user 2026-09-01 (options segment bots brainstorm →
"small first slice now"), designed in
`docs/superpowers/specs/2026-09-01-options-segment-bots-design.md`,
applied by `dashboard/blueprint_edits/apply_2026-09-01_options_scanner_first_slice.py`.
5 parts retired, 1 part added, 0 new data types (the new part reuses
`broker-instrument-listing`/`broker-market-data`/`broker-option-greeks`,
already declared by the market-data-feed work).

## Scope

Deliberately narrow. The full options-segment-bot conversion needs
`segments.members` relabeled from crypto's `spot`/`futures`/`options` to
`index-options`/`stock-options`, and every surviving template part's
consumes/produces audited for crypto-only types (`bull-feature-builder`
reads `funding-forecast` and `symbol-universe`, `instrument-selector`
reads `symbol-price-frame`/`symbol-quote-frame` — none of which any Indian
producer will ever supply). That audit is real, dozens-of-parts work,
explicitly **not** done here — the user chose to slice it rather than
rush it. This edit is only the bounded, fully-traced piece: retire what
has no honest Indian equivalent, add what the user asked for.

## What gets retired, and why the cascade stops where it does

Four detectors in `opportunity-scanner` are genuinely crypto-only —
funding rate, on-chain whale transfers, liquidation clusters, and social
sentiment have no Indian-equity-options equivalent built, and inventing
one would be exactly the fabricated-to-fit-a-checker move this project
refuses elsewhere:

| Retired | Why |
|---|---|
| `funding-skew-detector` | funding rate is a crypto perpetual concept |
| `whale-flow-detector` | on-chain transfer data has no equity analogue |
| `liquidation-cascade-detector` | crypto leverage liquidation clusters, not modelled for Indian margin |
| `sentiment-shift-detector` | no Indian sentiment source built |

**Cascade traced one level, not assumed:** each retired detector's
consumed types were checked for other real consumers before deciding
whether the producer becomes an orphan.

- `funding-forecast`, `whale-transfer`, `liquidation-map`,
  `sentiment-reading` all have **other real consumers**
  (`leverage-selector`, `cross-segment-signal-bridge`, `stop-target-placer`,
  `idea-generator`, etc.) — their producers stay, untouched, still needed
  by parts this slice does not touch.
- `onchain-flow` is consumed **only** by `whale-flow-detector` — its
  producer, `onchain-flow-aggregator`, becomes a genuine orphan output the
  moment the detector is gone, so it is retired too. `onchain-flow-
  aggregator` itself consumes nothing, so the cascade stops there —
  verified, not assumed.

## What gets added

`expiry-day-zero-to-hero-detector` — the user's own addition. Index
options only (NIFTY, BANK NIFTY, SENSEX — the three with liquid weekly
expiries). Spots a deep out-of-the-money option cheap enough that a late
move in the underlying toward its strike could multiply its price many
times over before the session closes, driven by gamma exploding as
expiry approaches.

Consumes the market-data-feed work's own real Indian types
(`broker-instrument-listing`, `broker-market-data`, `broker-option-greeks`)
for the option itself, plus `symbol-price-frame` for the underlying —
the same still-unaudited crypto-shaped type every other surviving
detector in this category currently reads, since that audit is exactly
what this slice defers. Not a new inconsistency; the same one every
other kept detector is already in.

## Not done here, named so it isn't lost

- `segments.members` still lists `spot`/`futures`/`options` — not yet
  relabeled to `index-options`/`stock-options`.
- `bull-feature-builder`, `bear-feature-builder` (crypto-shaped, both read
  `funding-forecast`), `instrument-selector`, and the rest of
  `bull-bot`/`bear-bot`/`profit-tailgating-bot`/`ai-brain`'s parts are
  unaudited — still declared exactly as crypto left them.
- `funding-rate-forecaster`, `whale-transfer-reader`,
  `liquidation-cluster-mapper`, `social-sentiment-reader` all stay
  declared (they still have real consumers) — none of that is a
  statement that they belong in the Indian build; it's a statement that
  removing them wasn't this slice's job.
