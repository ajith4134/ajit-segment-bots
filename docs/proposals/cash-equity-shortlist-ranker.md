# cash-equity-shortlist-ranker

**Proposed 2026-09-05.** Ranks today's ordinary NSE shares into a top-N
shortlist by momentum, volume surge, 52-week range proximity, gap from the
prior close, VWAP deviation and ATR-normalised move -- never open interest,
which does not exist on a cash equity.

## The gap

`broker-symbol-universe-bridge` derives cash-equity's universe correctly (every
ordinary share no F&O segment covers -- 2,444 of them, 2026-09-05) but
publishes all of them, unranked and uncapped. That is an order of magnitude
past the cliff the *options* chains were already capped at 50 to avoid: this
project's own `cointegration-pair-finder`/`spread-reversion-detector` fail as
the square of the universe, and `correlation-cluster-mapper` needed a derived,
budgeted pass-rate to survive a universe this size (`runtime.toml`, 2026-09-05
note, 2.98M pairs). The operator asked (2026-09-05) for the top 50 names
actually worth scanning today instead.

A second, separate gap: `segment_underlying_trading_symbols` in
`cash-equity-intraday.toml` -- the list `segment_that_trades` and
`instrument_selector`'s `segment_resolver_from_settings` use to decide whose
capital owns a fill -- was still the pre-derivation 14-name static list,
disconnected from both the 2,444-share universe and this shortlist. Fixed
alongside this part: both ownership resolvers accept a live shortlist override
for a segment whose `segment_universe_selection` is not `"stated"`.

## What it does

Consumes `broker-instrument-listing` (filtered the same way
`equity-opportunity-profiler` filters, `EquityWithoutADerivative.admits`, T-6),
`equity-historical-profile`, `liquidity-grade`, `broker-price-frame` and
`candle`. Tracks each candidate's session open, cumulative volume and
volume-weighted average price from the `candle` stream, resetting at the IST
session-day boundary. Blends six percentile-ranked signals
(`runtime.cash_equity_shortlist.rank_cash_equity_candidates`, shared with
`operate/replay_a_captured_session.py` so a replay ranks the same way the live
spine would) within a liquidity-qualified pool, and publishes
`cash-equity-shortlist`: today's top `cash_equity_shortlist_size` symbols, a
level restated on an interval.

It has no opinion about F&O exclusion (T-4) -- a share that happens to also be
an F&O underlying can appear in the profiled/ranked set here; excluding it from
cash-equity's actual universe is `broker-symbol-universe-bridge`'s question,
asked of this part's output.

## What goes in it, and why it is bounded

`cash_equity_shortlist_size` (50, matching the standing "hold at 50" ruling)
and `cash_equity_shortlist_liquidity_pool_size` (the eligible pool before the
blend runs) are both named settings, never literals in the ranking code
(RL-061). The six blend weights must sum to 1.0
(`ShortlistWeightsInvalid`) -- a settings typo that silently rescaled the blend
would change what "half the weight is momentum" means without anyone stating a
new number.

## What this does NOT fix

- **Circuit-limit proximity is not a signal here.** `equity-opportunity-
  profiler` does not fetch it yet (see that proposal); the field exists on
  `equity-historical-profile` and is `None` until it does.
- **NSE delivery percentage is not a signal here**, for the same reason
  (Akamai-blocked, see the profiler's proposal).
- **This part does not itself cap `symbol-universe`.** `broker-symbol-
  universe-bridge` reads `cash-equity-shortlist` and applies the cap where
  cash-equity's universe is actually published; this part only computes and
  states the shortlist.

## Contract

```
consumes  broker-instrument-listing, equity-historical-profile, liquidity-grade,
          broker-price-frame, candle
produces  cash-equity-shortlist, part-health
resource_class      compute-bound
rate_risk           changes-the-answer
skipped_tick_effect delays
```

## Settings

| setting | meaning |
|---|---|
| `cash_equity_shortlist_size` | how many symbols the shortlist keeps |
| `cash_equity_shortlist_liquidity_pool_size` | how many of the most-liquid candidates are eligible before the blend runs |
| `cash_equity_shortlist_momentum_weight` | share of the blend spent on today's move so far |
| `cash_equity_shortlist_volume_weight` | share spent on volume-so-far against the average day |
| `cash_equity_shortlist_week_52_weight` | share spent on proximity to the 52-week high or low |
| `cash_equity_shortlist_gap_weight` | share spent on the session's gap from the prior close |
| `cash_equity_shortlist_vwap_weight` | share spent on deviation from the session's own VWAP |
| `cash_equity_shortlist_atr_weight` | share spent on today's move sized against average true range |
| `cash_equity_shortlist_restatement_interval` | how often the shortlist is restated when unchanged |

## Verification

```
python3 dashboard/check_contracts.py
python3 dashboard/check_payload_reads.py
python3 dashboard/check_part_calls.py
python3 -m pytest tests/runtime/test_cash_equity_shortlist.py \
    tests/parts/market_data_feed/test_equity_opportunity_profiler.py \
    tests/parts/market_data_feed/test_cash_equity_shortlist_ranker.py -q
```
