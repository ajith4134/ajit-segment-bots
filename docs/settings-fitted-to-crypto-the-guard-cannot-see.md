# Settings fitted to crypto that the drift guard cannot see

Found 2026-09-13, after `profit-lock`'s three settings turned out to be fitted to crypto with
no venue named in their notes. `dashboard/measure_objectives.py` counts a setting as crypto-fitted
only when its note matches `CRYPTO_PROVENANCE` -- and that pattern is **case-sensitive**, so
"Binance" and "Bybit" never match. Fixing the case alone takes its unmarked count from 4 to 12.
The larger blind spot is below: notes that justify a number by a crypto *measurement* -- "the
captured thirty", "both venues", "the liquid captured symbols", "trades a second" -- without
naming a venue at all.

**How the list was made.** All 960 settings in the operator's `runtime.toml`: 870 carry no refit or
classification marker; 759 of those were first written before the 2026-09-01 pivot; 688 are read by
a part on the live spine. Their notes were triaged by what they cite and every candidate with a
market-scaled unit was read by hand. The 34 below cite crypto evidence for a number a live part acts on.
**Progress:** 2026-09-13 — `liquidity_tradeable_cost_fraction`, `liquidity_thin_cost_fraction`, `regime_window_length`, `regime_minimum_observations` refitted (plus `regime_trending_hurst_above` / `regime_reverting_hurst_below`, which were not in this list and should have been). 30 remain.
2026-09-13 (later) — the whole cost group, from one measurement of the NSE option touch on the book tape
(`measurements/2026-09-13-indian-option-spreads/`: 427,632 in-session snapshots, 2,413 contract-days,
half spread p50 0.3167% per snapshot, 0.3265% print-weighted, p80 print-weighted round trip 2.37%).
`maker_fee_rate` recorded INERT (only a non-Upstox fill reads it); the other six refitted, plus
`resting_order_maximum_distance_fraction` (1% "One percent", no basis, clamped the new prior) — another
this list missed because its note cites nothing. `exploration_maximum_cost_fraction` had become
unpassable again after the 2026-09-12 fee refit (pair cost 1.71% against a 0.25% ceiling). Live after a
restart: `counterfactual-replayer` `round_trip_cost_charged` 0.0011 -> 0.008532. **23 remain.**
Still open: the guard's `CRYPTO_PROVENANCE` is still case-sensitive.

## Costs and spreads taken from the crypto venues (7)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| ~~`maker_fee_rate`~~ DONE 2026-09-13 | 0.0002 fraction of notional | paper-fill-simulator | "Both venues publish 0.0200% maker" |
| ~~`exploration_maximum_cost_fraction`~~ DONE 2026-09-13 | 0.0025 fraction of notional | exploration-pair-opener | "4x taker_fee_rate (0.0022 at the current 0.00055 rate)" |
| ~~`regret_cost_fraction`~~ DONE 2026-09-13 | 0.0011 fraction of notional | counterfactual-replayer, regret-tracker | "Two taker fees at the higher venue's rate" |
| ~~`slippage_prior_cost_fraction`~~ DONE 2026-09-13 | 0.001 fraction of price | slippage-learner | "a spread and a fee on a liquid symbol" |
| ~~`backtest_prior_half_spread_fraction`~~ DONE 2026-09-13 | 0.0005 fraction of price | execution-cost-model | "the liquid captured symbols on 2026-08-22" |
| ~~`limit_walk_prior_step_fraction`~~ DONE 2026-09-13 | 0.0002 fraction of price | limit-price-walker | "about the spread on the liquid captured symbols" |
| ~~`resting_order_prior_distance_fraction`~~ DONE 2026-09-13 | 0.002 fraction of price | resting-order-cancel-policy | "ten spreads on the liquid symbols" |

## Move sizes fitted to crypto's intraday range (8)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| `bear_entry_prior_extension_floor` | 0.01 fraction of price | bear-entry-timer | "below the 1.5% largest intraday move measured on the captured thirty" |
| `tail_prior_normal_move_fraction` | 0.01 fraction of price | tail-copy-selector, tail-move-remaining-estimator, tail-mover-qualifier | "under the 1.5% largest intraday move measured on the captured thirty today" |
| `tail_prior_trail_fraction` | 0.01 fraction of price | tail-trailing-exit-planner | "as for profit_lock_prior_retracement" |
| `forecast_prior_absolute_return` | 0.001 fraction of price | entropy-magnitude-forecaster | "the median five-minute move on the liquid captured symbols today" |
| `liquidity_tradeable_cost_fraction` | 0.005 fraction of price | liquidity-grader | "a third of the 1.5% stop the exit plans set" — **live 2026-09-13: liquidity-grader grades 68 of 91 symbols untradeable and 4 thin** |
| `liquidity_thin_cost_fraction` | 0.02 fraction of price | liquidity-grader | "more than the stop" — **live 2026-09-13: same grader, 68 of 91 untradeable** |
| `bear_setup_weight_prior_loss_fraction` | 0.02 fraction of price | bear-setup-weight-learner | "the stop the plans set plus slippage" |
| `bear_setup_weight_prior_win_fraction` | 0.02 fraction of price | bear-setup-weight-learner | "so the prior tail ratio is one" |

## Counts and times sized to crypto print rates (13)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| `bull_feature_short_window` | 64 observations | bull-feature-builder | "202-883 trades a second ... on Binance aggregates" |
| `bear_feature_short_window` | 64 observations | bear-feature-builder | "202-883 trades a second ... on Binance aggregates" |
| `bull_feature_minimum_observations` | 64 observations | bull-feature-builder | "set to the short window" |
| `bear_feature_minimum_observations` | 64 observations | bear-feature-builder | "set to the short window" |
| `regime_window_length` | 1024 trades | regime-classifier | "measured on 60000 real trades from four symbols" — **live 2026-09-13: regime-classifier unclassified 20,901, symbols_forgotten_silent 748** |
| `regime_minimum_observations` | 1024 trades | regime-classifier | "set equal to the window" — **live 2026-09-13: regime-classifier unclassified 20,901** |
| `liquidity_turnover_window` | 60 observations | liquidity-grader | "about a minute on the captured symbols" |
| `order_participation_cap` | 0.1 fraction of traded volume | fill-volume-capper, participation-capped-order-splitter | "the thinnest captured symbol trades about 30 prints a minute" |
| `outage_silence_seconds` | 60.0 seconds since a symbol's last print | venue-outage-rider | "the top-30 symbols captured all print far inside a minute" |
| `tail_window_length` | 50 prints | tail-move-remaining-estimator, tail-mover-qualifier | "a few minutes on the captured symbols" |
| `spread_reversion_horizon` | 60.0 seconds | bear-position-invalidation-watcher, bull-position-invalidation-watcher, label-builder, spread-reversion-detector | "0.07 s on a symbol printing 200 a second" |
| `limit_walk_cadence` | 2.0 seconds | limit-price-walker | "a few prints on the captured symbols" |
| `order_latency_prior` | 0.25 seconds | order-latency-simulator | "REST order placement to both venues ... 80 to 200 ms" |

## Crypto mechanisms: funding, venue maintenance, signed requests (6)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| `bear_maximum_carry_fraction_of_horizon` | 0.01 fraction of price | bear-setup-filter | "the most funding a short may be projected to pay" |
| `bear_invalidation_carry_fraction_of_expected_move` | 0.5 fraction | bear-position-invalidation-watcher | "its funding carry" |
| `live_balance_tolerance_fraction` | 0.02 fraction of the allocation | live-balance-divergence-watch | "about a day of funding and fees" |
| `event_risk_scheduled_window` | 900.0 seconds | event-risk-limiter | "repricing around a funding settlement or a listing" |
| `event_risk_announcement_window` | 3600.0 seconds | event-risk-limiter | "both venues announce maintenance at least an hour ahead" |
| `clock_drift_warning` | 1.0 seconds | clock-skew-monitor | "both venues reject a signed request" |

## Not counted here

- Plumbing (inbox sizes, timeouts, checkpoints, heartbeats) and learning priors whose notes cite no market
  measurement: 428 settings judged market-independent by their own reasoning, not re-read one by one.
- The four the guard already counts (`feed_jump_threshold_increments`, `feed_jump_threshold_fraction`,
  `volatility_gap_minimum_fraction`, `reference_price_maximum_age_seconds`).
