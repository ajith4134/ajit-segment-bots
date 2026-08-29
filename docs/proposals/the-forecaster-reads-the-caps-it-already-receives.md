# The forecaster reads the funding-formula facts the catalogue already carries

**Proposed 2026-08-29, after tracing why 100% of every bull and bear feature
vector has ever been incomplete.**

## What stopped

`funding-rate-forecaster.observe_funding_parameters` (named `observe_venue_
parameters` before this change) was never called anywhere in the codebase, and
its data type was not even declared on the part's own `consumes`. Every call to
`forecast()` therefore refused `NO_SYMBOL_PARAMETERS`, `predicted_rate` stayed
`None` on every forecast ever produced, and `bull-feature-builder` /
`bear-feature-builder` were missing `funding_forecast_change` on 129/129 and
186/186 feature vectors respectively -- the only feature missing on literally
every vector either bot ever built.

## What both venues already publish, and where it was going

`symbol-catalogue-reader` already fetches everything the formula needs, in the
same round-trip `docs/proposals/venue-declared-funding-facts.md` wired the rate
and interval from:

|                    | binance-usdm                                    | bybit-linear                          |
|---|---|---|
| cap                | `adjustedFundingRateCap` on `fundingInfo`, per symbol (measured 2026-08-28: 0.003 to 0.02 across the captured set) | `fundingCap` on the `tickers` response the reader already reads, per symbol (0.00333 on BTCUSDT, 0.005 elsewhere in the same capture) |
| floor              | `adjustedFundingRateFloor` on the same response | not stated separately; this venue documents the bound as symmetric, so floor is taken as the cap's negative |
| interest rate      | `interestRate` on `premiumIndex`, per symbol    | not stated by any endpoint read here -- left `None`, not guessed |
| interval           | already wired (`venue-declared-funding-facts.md`) | already wired |

All four were being fetched, and three of them were being dropped on the floor
of `ContractFunding`'s two-field constructor. No new HTTP request on either
venue.

## Why per symbol, not per venue

`FundingParameters` was keyed by `venue_id` alone. Measured 2026-08-28,
Binance's own `fundingInfo` splits 444 symbols at a four-hour settlement
interval, 314 at eight, 2 at one -- and its cap runs 0.003 to 0.02 across the
same read. A single venue-wide constant would have silently mis-timed or
mis-capped the forecast for whichever symbols sit off the majority value, which
is the exact "assumed the usual eight hours" failure `venue-declared-funding-
facts.md` already found and rejected for the rate and interval. `FundingParameters`
is now keyed by `(venue_id, symbol)`, matching how the data actually varies.

`averaging_window_seconds` is retired rather than fetched: the premium index is
averaged over the settlement interval itself, so `interval_seconds` already
carries it, and a second unfetched number would have been a placeholder wearing
a field name.

## What is still a setting, and why

`premium_clamp` -- the ±0.05% interest-vs-premium clamp this part's own
docstring already documents as the formula -- is not on any endpoint either
venue's adapter reads in this codebase. It stays a named setting
(`funding_premium_clamp`, `~/.config/ajit-segment-bots/settings/runtime.toml`)
with its provenance stated as reference knowledge from the published formula,
not a live reading, per RL-061.

## The change to the blueprint -- one edge

    funding-rate-forecaster: consumes += symbol-universe

No new part. No new data type. `symbol-universe` already carries venue-declared
per-symbol contract facts -- `price_increment` rides there for `tick-size-
resolver`, and the funding rate/interval already ride there for `instrument-
selector`. The three new fields (`funding_rate_cap`, `funding_rate_floor`,
`funding_interest_rate_per_interval`) are the same class of fact from the same
responses, carried the same way.

## What this does not claim

That the resulting forecast is accurate for every symbol -- Bybit's floor is a
documented convention (symmetric around the published cap), not a second
reading, and its interest rate is genuinely undeclared, so Bybit symbols
forecast with `interest_rate_per_interval` absent and are refused rather than
guessed. It closes the gap between a formula this part already knows how to
compute and the facts it needs to compute it, for the venue and the fields that
state them.
