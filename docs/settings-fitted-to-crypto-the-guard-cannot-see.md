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
2026-09-13 (later still) — the move-size group, measured on the 2026-09-07/08 tape with each part's own definition
(`measurements/2026-09-13-indian-option-move-sizes/`, 4,075,399 in-session option prints): bounce above the
256-print mean p50 1.17%, completed sustained move p50 1.00%, five-minute |return| p50 0.29%. Refitted
`bear_entry_prior_extension_floor` 0.0117, `tail_prior_normal_move_fraction` 0.0100 (numerically unchanged, now
measured -- and it is the only normal move the tail parts ever use, because nothing calls
`observe_completed_move`), `forecast_prior_absolute_return` 0.0029, `tail_prior_trail_fraction` 0.0609,
`bear_setup_weight_prior_loss_fraction` / `_win_fraction` 0.0642 (the 6.09% option stop plus a half spread),
and one more this list missed, `tail_minimum_trail_fraction` 0.002 -> 0.0196 ("a few spreads"). **17 remain.**
The guard's `CRYPTO_PROVENANCE` is case-insensitive now: its count went 8 -> 18, ten notes naming "Binance" or
"Bybit" it could never see, two of them false (a part named usdt-pnl-accountant, a misattributed comment).
2026-09-13 (evening) — the counts-and-times group, and **a correction to the move-size group**. Two facts the
move-size pass missed, both in `measurements/2026-09-13-indian-observation-cadence/`:
**`symbol-price-frame` carries underlyings only** (its one producer on this feed is
`broker-underlying-price-frame-bridge`), so bear-entry-timer, the tail parts, both feature builders,
regime-classifier and venue-outage-rider never see a contract price. Contracts travel on `market-data`. And **an
observation is a distinct trade**: `RollingWindow` skips a re-delivered (time, price), and only 17.8% of option
tape records are a new trade. The move-size pass measured option tape records, so four values were on the wrong
series and are corrected by the refit script's new `CORRECTIONS` step:
`bear_entry_prior_extension_floor` 0.0117 -> 0.000049, `tail_prior_normal_move_fraction` 0.0100 -> 0.000059,
`tail_prior_trail_fraction` 0.0609 -> 0.00043, `tail_minimum_trail_fraction` 0.0196 -> 0.00042. The old crypto
1% was twenty times the p95 underlying bounce, so the timer and the qualifier could never have fired on an
underlying either way.
Counts and times: feature windows (64 / 512 / minimum 64, both bots) kept, now measured (64 underlying trades
span p50 104s; the conviction checkpoints learned at 64); `tail_window_length` 50 kept (p50 80s);
`liquidity_turnover_window` 60 -> 10; `limit_walk_cadence` 2 -> 12s; `outage_silence_seconds` 60 -> 900
(shortest candidate where ordinary trading never read 0.8 of underlyings silent; sampled on 16-18 underlyings,
re-measure on the first full 216-underlying session); `order_participation_cap` 0.1 -> 0.16 (touch against
exchange candle volume). `spread_reversion_horizon` and `order_latency_prior` recorded MEASURED BUT INCONCLUSIVE:
the Indian half-life is unmeasured, and an order's latency cannot be measured on paper (Upstox reads 28-54 ms).
**8 of the 34 remain**: the six crypto mechanisms and those two.
**Reopened, not in this list:** `regime_window_length` and the two Hurst thresholds were fitted on option sessions
(commit 7f8e1e0), but regime-classifier reads `symbol-price-frame` -- underlyings. Re-measure on underlyings.
**Not a setting:** 95.8% of journalled entry candidates name a contract, and every part above that windows
`symbol-price-frame` has no window for a contract, so it stands those candidates down. That is wiring.
2026-09-13 (night) — the crypto-mechanisms group. None of the six gated anything live: nothing calls
`observe_funding_rate` or `observe_funding_settlement`, `event-risk-limiter` has never received a `market-event`,
and the balance watch is not live. Measured in `measurements/2026-09-13-indian-carry-and-clock/`:
**theta is the Indian carry, and it is large** -- |theta| / premium p50 3.9% a day (17.0% trade-weighted) on
contracts not expiring that day, p50 40% an hour on expiry day; over an hour p80 1.31%, so a 1% bound would bind.
The two bear carry settings and both event windows are recorded NEEDS AN INDIAN MECHANISM: theta from
`broker-option-greeks` integrated over holding time (and for bull positions too -- a one-sided carry filter is the
shape of funding, not of an option), and the unbuilt `results-calendar-reader`, `macro-event-calendar-reader`,
`regulator-circular-reader` and `exchange-filing-reader`. `live_balance_tolerance_fraction` 0.02 -> 0.0136, the
heaviest measured day of Upstox charges against a Rs75 lakh allocation (no funding on a bought option).
`clock_drift_warning` 1.0 kept on an Indian basis: Upstox requests carry no timestamp, so nothing is rejected;
1.0s is `reference_price_minimum_age_seconds`, the tightest age judgment. Clock offset p5 9-12 ms both days (the
clock is fine), but half-hour medians of 42s and 66s on 09-08 and 0.5-14s through much of 09-07 -- this system
receiving late, which `clock-skew-monitor` would report as drift. **The whole list is now classified: 0 of the 34
unexamined; 6 need a mechanism, 2 measured but inconclusive.**
Wiring findings from this group: the balance watch can never read a live Upstox balance (it names no segment)
and compares equity, which moves by P&L; `clock-skew-monitor` conflates lag with skew. Seen on the spine, not
caused here: `order-flow-state-encoder` escalated "taking longer every tick" 9 times on 09-12 and 16 times on 09-13.
**2026-09-13 (late) — CORRECTION: the evening entry above is wrong about the wiring.** It says
`symbol-price-frame` carries underlyings only. It has two producers: `broker-underlying-price-frame-bridge`
and **`price-level-sampler`, which reads `market-data` and publishes every subscribed instrument** (1,995
symbols live). Found because regime-classifier's own checkpoint held 1,775 contract series beside 220
underlyings, while a regime correction built on the same false premise was about to be applied -- that
change was restored from the refit backup before the spine ever loaded it. Consequently the "not a setting"
wiring finding above (contract candidates have no window) is false, and the four first-round corrections put
contract-judging parts on underlying scale -- `tail_prior_normal_move_fraction` 0.000059 read an ordinary 1%
contract move as 170 normal moves. A second correction round (`SECOND_CORRECTIONS`, marker
`CORRECTED AGAIN 2026-09-13`), measured on contracts and underlyings as distinct trades
(`measurements/2026-09-13-indian-frame-as-parts-see-it/`): `bear_entry_prior_extension_floor` 0.0129
(contract p50), `tail_prior_normal_move_fraction` 0.0093 (contract p50), `tail_prior_trail_fraction` back to
0.0609 and `tail_minimum_trail_fraction` back to 0.0196; `outage_silence_seconds` 900 -> 600 (over all 1,389
symbols the rider judges, 600s is the shortest threshold that never read 0.8 silent); regime bands
0.696/0.429 -> 0.649/0.192, the pooled p95/p5 at 256 (in use they read 31.0% of contract and 53.5% of
underlying windows reverting; now 5.0% each way). `regime_window_length` 256 kept but its rule is **not met**:
on contract trades the estimator's spread is 0.149 at 256 and 0.128 at 1024, never under 0.1. Feature-window and
`tail_window_length` notes corrected to state contract spans (64 trades about 6 minutes trade-weighted).
Classification of the 34 is unchanged by this.
2026-09-13 (last) — **the drift guard's own list, 18 -> 6, all 6 measured but inconclusive.** Found live:
`capital_state_parts` still named `usdt-pnl-accountant`, renamed `inr-pnl-accountant` on 2026-09-12, so the
rupee P&L accountant was being restarted blindly and could be swapped without a flat book; corrected.
Inert (reader off the spine, or a branch nothing reaches): the two symbol-selection settings, `book_depth_levels`,
both venue reconnect backoffs, `api_key_rejection_rest`, and `feed_jump_threshold_increments` (nothing calls
`set_price_increment`). `settings_recheck_interval` market-independent (the match was a comment belonging to the
next setting). `price_gap_warmup_gaps` recorded as already Indian. Measured in
`measurements/2026-09-13-indian-guard-remainder/`: `reference_price_maximum_age_seconds` 60 kept on Indian evidence
(contract p95 drift 5.56% at 30s, 7.14% at 60s, against the 6.09% option stop); `volatility_gap_minimum_fraction`
0.2 -> 0.29 (median |implied - realised| / realised over 31 underlying-days; realised stands in for a forecast
nobody has produced yet); `feed_jump_threshold_fraction` MEASURED BUT INCONCLUSIVE -- at 0.005 half of every
symbol's first eight bars read as a jump, but the floor is also the permanent lower bound, so raising it to the
contract figure (about 0.18) would blind the detector on indices (p99 0.056%). That needs the part changed.

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
| ~~`bear_entry_prior_extension_floor`~~ DONE 2026-09-13 | 0.01 fraction of price | bear-entry-timer | "below the 1.5% largest intraday move measured on the captured thirty" |
| ~~`tail_prior_normal_move_fraction`~~ DONE 2026-09-13 | 0.01 fraction of price | tail-copy-selector, tail-move-remaining-estimator, tail-mover-qualifier | "under the 1.5% largest intraday move measured on the captured thirty today" |
| ~~`tail_prior_trail_fraction`~~ DONE 2026-09-13 | 0.01 fraction of price | tail-trailing-exit-planner | "as for profit_lock_prior_retracement" |
| ~~`forecast_prior_absolute_return`~~ DONE 2026-09-13 | 0.001 fraction of price | entropy-magnitude-forecaster | "the median five-minute move on the liquid captured symbols today" |
| ~~`liquidity_tradeable_cost_fraction`~~ DONE 2026-09-13 | 0.005 fraction of price | liquidity-grader | "a third of the 1.5% stop the exit plans set" — **live 2026-09-13: liquidity-grader grades 68 of 91 symbols untradeable and 4 thin** |
| ~~`liquidity_thin_cost_fraction`~~ DONE 2026-09-13 | 0.02 fraction of price | liquidity-grader | "more than the stop" — **live 2026-09-13: same grader, 68 of 91 untradeable** |
| ~~`bear_setup_weight_prior_loss_fraction`~~ DONE 2026-09-13 | 0.02 fraction of price | bear-setup-weight-learner | "the stop the plans set plus slippage" |
| ~~`bear_setup_weight_prior_win_fraction`~~ DONE 2026-09-13 | 0.02 fraction of price | bear-setup-weight-learner | "so the prior tail ratio is one" |

## Counts and times sized to crypto print rates (13)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| ~~`bull_feature_short_window`~~ DONE 2026-09-13 | 64 observations | bull-feature-builder | "202-883 trades a second ... on Binance aggregates" |
| ~~`bear_feature_short_window`~~ DONE 2026-09-13 | 64 observations | bear-feature-builder | "202-883 trades a second ... on Binance aggregates" |
| ~~`bull_feature_minimum_observations`~~ DONE 2026-09-13 | 64 observations | bull-feature-builder | "set to the short window" |
| ~~`bear_feature_minimum_observations`~~ DONE 2026-09-13 | 64 observations | bear-feature-builder | "set to the short window" |
| ~~`regime_window_length`~~ DONE 2026-09-13 | 1024 trades | regime-classifier | "measured on 60000 real trades from four symbols" — **live 2026-09-13: regime-classifier unclassified 20,901, symbols_forgotten_silent 748** |
| ~~`regime_minimum_observations`~~ DONE 2026-09-13 | 1024 trades | regime-classifier | "set equal to the window" — **live 2026-09-13: regime-classifier unclassified 20,901** |
| ~~`liquidity_turnover_window`~~ DONE 2026-09-13 | 60 observations | liquidity-grader | "about a minute on the captured symbols" |
| ~~`order_participation_cap`~~ DONE 2026-09-13 | 0.1 fraction of traded volume | fill-volume-capper, participation-capped-order-splitter | "the thinnest captured symbol trades about 30 prints a minute" |
| ~~`outage_silence_seconds`~~ DONE 2026-09-13 | 60.0 seconds since a symbol's last print | venue-outage-rider | "the top-30 symbols captured all print far inside a minute" |
| ~~`tail_window_length`~~ DONE 2026-09-13 | 50 prints | tail-move-remaining-estimator, tail-mover-qualifier | "a few minutes on the captured symbols" |
| `spread_reversion_horizon` MEASURED, INCONCLUSIVE 2026-09-13 | 60.0 seconds | bear-position-invalidation-watcher, bull-position-invalidation-watcher, label-builder, spread-reversion-detector | "0.07 s on a symbol printing 200 a second" |
| ~~`limit_walk_cadence`~~ DONE 2026-09-13 | 2.0 seconds | limit-price-walker | "a few prints on the captured symbols" |
| `order_latency_prior` MEASURED, INCONCLUSIVE 2026-09-13 | 0.25 seconds | order-latency-simulator | "REST order placement to both venues ... 80 to 200 ms" |

## Crypto mechanisms: funding, venue maintenance, signed requests (6)

| setting | value | read by | what its note rests on |
|---|---|---|---|
| ~~`bear_maximum_carry_fraction_of_horizon`~~ NEEDS AN INDIAN MECHANISM 2026-09-13 | 0.01 fraction of price | bear-setup-filter | "the most funding a short may be projected to pay" |
| ~~`bear_invalidation_carry_fraction_of_expected_move`~~ NEEDS AN INDIAN MECHANISM 2026-09-13 | 0.5 fraction | bear-position-invalidation-watcher | "its funding carry" |
| ~~`live_balance_tolerance_fraction`~~ DONE 2026-09-13 | 0.02 fraction of the allocation | live-balance-divergence-watch | "about a day of funding and fees" |
| ~~`event_risk_scheduled_window`~~ NEEDS AN INDIAN MECHANISM 2026-09-13 | 900.0 seconds | event-risk-limiter | "repricing around a funding settlement or a listing" |
| ~~`event_risk_announcement_window`~~ NEEDS AN INDIAN MECHANISM 2026-09-13 | 3600.0 seconds | event-risk-limiter | "both venues announce maintenance at least an hour ahead" |
| ~~`clock_drift_warning`~~ DONE 2026-09-13 | 1.0 seconds | clock-skew-monitor | "both venues reject a signed request" |

## Not counted here

- Plumbing (inbox sizes, timeouts, checkpoints, heartbeats) and learning priors whose notes cite no market
  measurement: 428 settings judged market-independent by their own reasoning, not re-read one by one.
- The four the guard already counts (`feed_jump_threshold_increments`, `feed_jump_threshold_fraction`,
  `volatility_gap_minimum_fraction`, `reference_price_maximum_age_seconds`).
