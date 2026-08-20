# nautechsystems/nautilus_trader — read 2026-08-20

LGPL-3.0. Core is Rust (`crates/`); the `python/` package is now a thin PyO3
binding layer over it (v2, mid-migration from the old Cython v1 at
`MIGRATION_V2.md`). Institutional-grade algo-trading platform: backtest,
paper (`Sandbox`) and live share one engine so strategy code runs unchanged.
Clone commit: `2114cf6f761429e0adb5ca9596fcd7b895b16011`.

## Mechanisms worth stealing (ranked)

1. **Fixed-risk position sizing with commission and batching** —
   `crates/risk/src/sizing.rs:calculate_fixed_risk_position_size`. Risk money
   = `equity * risk_pct - equity*risk_pct*commission_rate*2` (commission
   charged both sides). Size = `risk_money / exchange_rate / risk_ticks /
   price_increment / multiplier`, then divided across `units` (scale-in legs),
   floor-rounded to `unit_batch_size`, and capped by `hard_limit` and the
   instrument's `max_quantity`. Directly usable for `position-sizer`.
2. **Layered pre-trade risk gate** —
   `crates/risk/src/engine/mod.rs:RiskEngine::check_orders_risk_for_account`.
   One function walks, per order: price/quantity bounds, GTD expiry-in-past,
   reduce-only-would-increase-position, min/max notional per order and per
   instrument, cumulative notional (cash) or cumulative initial margin
   (margin) across an entire order list before any of it is sent — so five
   orders that individually pass can still be denied together. Denials carry
   a typed `OrderDeniedReason` (18 variants) rather than a string.
3. **Submit/modify rate throttling as a risk-engine responsibility, not the
   venue's** — `crates/risk/src/engine/mod.rs:RiskEngine::create_submit_throttler`.
   A token-bucket `Throttler` sits in front of every submit/modify; on
   overflow it synthesizes `OrderDenied`/`OrderModifyRejected` locally instead
   of ever hitting the exchange. Useful even in paper mode to rehearse venue
   rate limits before going live.
4. **Six commission models, picked by instrument shape** —
   `crates/execution/src/models/fee.rs`. `MakerTakerFeeModel` (rate × notional
   by liquidity side), `PerContractFeeModel` (flat × contracts, expands
   multi-leg spreads by parsing `((ratio))SYMBOL` components),
   `ProbabilityPriceFeeModel` (`qty * fee * p * (1-p)` for prediction-market
   style instruments), `CappedOptionFeeModel` (`min(rate, cap) * multiplier`),
   `TieredNotionalOptionFeeModel`. All route through one `FeeModel` trait so a
   fill's cost is a one-line lookup regardless of instrument type.
5. **Eleven named liquidity/fill models with concrete depth numbers** —
   `crates/execution/src/models/fill.rs`. Not just "slippage probability":
   `TwoTierFillModel` (10 @ best, unlimited @ +1 tick), `ThreeTierFillModel`
   (50/30/20 @ best/+1/+2 ticks), `SizeAwareFillModel` (order ≤10 gets 50 @
   best; larger orders get 10 @ best + remainder at +1 tick — real market
   impact), `VolumeSensitiveFillModel` (25% of `recent_volume` at best),
   `CompetitionAwareFillModel` (liquidity scaled by a 0–1 factor). Each
   returns a synthetic `OrderBook` the matching engine fills against, so the
   same matching code handles every model.
6. **Latency model with base + per-op deltas** —
   `crates/execution/src/models/latency.rs:StaticLatencyModel`. Insert/
   update/delete each add their own delay on top of a shared base network
   latency (`effective = base + op`), and `SimulatedExchange` queues commands
   in a timestamp-ordered min-heap (`crates/backtest/src/exchange.rs:
   SimulatedExchange::send` → `generate_inflight_command`) so a paper fill
   never lands before its simulated wire time.
7. **Margin-based liquidation trigger** —
   `crates/backtest/src/exchange.rs:SimulatedExchange::process_liquidations`.
   Per settlement currency: `equity = balance + unrealized_pnl`; `threshold =
   maintenance_margin * liquidation_trigger_ratio`; if `equity <= threshold`,
   every open position in that currency is force-closed. Runs every step
   against real unrealized PnL, not a periodic check.
8. **Funding settlement as a scheduled, idempotent event** —
   `crates/backtest/src/exchange.rs:SimulatedExchange::settle_funding_rate`.
   PnL = `notional * funding_rate * (-1 if long else +1)`, applied per open
   position, batched into one account adjustment per currency, and guarded
   by a `funding_settlements: BTreeSet<(ts, instrument)>` so replay or
   restart can't double-settle the same boundary.
9. **Trailing stop as pure price arithmetic, three offset types × three
   trigger types** — `crates/execution/src/trailing.rs:
   trailing_stop_calculate`. Offset is price, basis-points-of-basis, or
   ticks; trigger basis is last trade, bid/ask, or "whichever moves the
   trigger more" (`LastOrBidAsk`). A closure (`maybe_move`) only ever moves
   the stop in the position's favour — the "ratchet" our `profit-lock` needs,
   given as ~15 lines of testable math with no engine state.
10. **Reconciliation with an explicit tolerance and a dedup discipline** —
    `crates/execution/src/reconciliation/mod.rs`. States its own invariants
    in the module doc: final quantity matches venue "within instrument
    precision", average price matches "within tolerance (default 0.01%)",
    and every synthesized fill's `trade_id`/`venue_order_id` is a
    deterministic function of the logical event so a restart-triggered
    replay reconciles to the same state instead of duplicating fills.
11. **Notional/margin risk checks are account-type-polymorphic in one pass** —
    same file as #2. Cash accounts check full notional against free balance;
    margin accounts check initial-margin against margin-free balance,
    *skipping* the check entirely for orders detected as position-reducing
    (tracked via running `cum_sell_qty`/`cum_buy_qty` against currently-open
    quantity minus already-pending opposite orders) — this reducing-order
    detection prevents false denials on legitimate exits during a margin
    squeeze.
12. **Net position and price staleness tracked centrally, not per-strategy**
    — `crates/portfolio/src/portfolio.rs:PortfolioState`. `net_positions:
    IndexMap<InstrumentId, Decimal>`, `stale_prices: AHashSet<(InstrumentId,
    PositionSide)>`, `last_xrates` for cross-currency positions — one place
    computes exposure so no two strategies can double-count.

## Map onto existing parts

| existing part | repo file | what it does that ours doesn't yet say |
|---|---|---|
| `position-sizer` | `crates/risk/src/sizing.rs:calculate_fixed_risk_position_size` | exact formula: equity×risk% minus 2× round-trip commission, divided by risk-ticks×price-increment×multiplier, then batched/hard-capped |
| `position-sizer` / `exposure-limiter` | `crates/risk/src/engine/mod.rs:RiskEngine::check_orders_risk_for_account` | per-order AND cumulative-across-order-list notional/margin checks; position-reducing orders are detected and exempted from the balance check |
| `paper-fill-simulator` | `crates/execution/src/models/fill.rs` | concrete tiered depth numbers (2-tier, 3-tier, size-aware, volume-sensitive) instead of a flat slippage probability |
| `execution-cost-model` | `crates/execution/src/models/fee.rs` | six named commission formulas keyed off instrument shape (maker/taker, per-contract with multi-leg spread parsing, capped) |
| `stop-order-manager` / `profit-lock` | `crates/execution/src/trailing.rs:trailing_stop_calculate` | full price/bps/ticks × last/bidask/last-or-bidask matrix, ratchet-only-improves logic |
| `drawdown-breaker` | `crates/backtest/src/exchange.rs:SimulatedExchange::process_liquidations` | exact trigger formula: `equity <= maintenance_margin * trigger_ratio`, evaluated per settlement currency every step |
| `fill-reconciler` | `crates/execution/src/reconciliation/mod.rs` | states a numeric tolerance (0.01% avg-price) and a deterministic-ID dedup rule for replay safety |
| `usdt-pnl-accountant` | `crates/backtest/src/exchange.rs:SimulatedExchange::settle_funding_rate` | funding PnL formula and idempotent settlement-boundary tracking |
| `cross-segment-exposure-watch` | `crates/portfolio/src/portfolio.rs:PortfolioState` | central net-position and stale-price tracking pattern (per instrument, not per strategy) |

## New part proposals

- **`margin-liquidation-watch`** — block: `risk-capital-allocation`.
  consumes: `position`, `account-balance`, NEW `maintenance-margin-schedule`.
  produces: `risk-limit`. Zero the limit (and let `drawdown-breaker`'s
  existing halt path take it from there) when
  `balance + unrealized_pnl <= maintenance_margin * trigger_ratio` for a
  leveraged segment. Evidence: exchange.rs `process_liquidations` above.
  Distinct from `drawdown-breaker` (which reads realised `closed-trade`
  history) because this reacts to unrealised mark-to-market every tick.
- **`order-latency-simulator`** — block: `paper-live-trading`. consumes:
  `order-request`. produces: `order-request` (time-shifted). Applies a
  base + per-operation (insert/modify/cancel) delay before a paper order is
  allowed to reach `paper-fill-simulator`, so paper fills can't be faster
  than a real venue round-trip would allow. Evidence:
  `crates/execution/src/models/latency.rs` +
  `crates/backtest/src/exchange.rs:SimulatedExchange::generate_inflight_command`.
- **`funding-settlement-recorder`** — block: `ledger`. consumes: `position`,
  `market-data` (funding rate). produces: `journal-entry`, adjustment to
  `usdt-pnl-statement`. Settles perp funding as a periodic, idempotent event
  keyed on settlement boundary rather than folding it into ordinary fill
  PnL. Evidence: `settle_funding_rate` above; distinct from
  `funding-rate-forecaster` (which only predicts, never books).

## Anti-patterns seen

- **`SimulatedExchange` is a god object** (`crates/backtest/src/exchange.rs`):
  one struct owns fee model, fill model, latency model, matching engines,
  account adjustment, liquidation, funding settlement, and simulation-module
  orchestration — 30+ fields, most of them booleans. T-6 says grow by adding
  parts; this grew by welding responsibilities onto one struct. Mine the
  algorithms (fee/fill/latency math, liquidation formula) but do not copy
  the container.
- **A data-plane bypass flag switches control-plane behaviour**:
  `RiskEngine.config.bypass` — `handle_submit_order` checks
  `if self.config.bypass { send straight to execution }` inline. That's a
  feature switching itself off, which is exactly what T-2 reserves for the
  resource governor.
- **A part accumulates unbounded hidden state across a run**:
  `PortfolioState` (`crates/portfolio/src/portfolio.rs`) carries a dozen
  `AHashMap`/`AHashSet` caches (`snapshot_sum_per_position`,
  `last_xrates`, `venues_missing_price`, …) that grow for the life of the
  process with no documented release path — if this part were ever turned
  off under T-3, nothing here says what gets freed.

## Not useful here

Large parts of the repo target markets and mechanics an intraday crypto
paper/live bot doesn't have: `ProbabilityPriceFeeModel` and binary-option
instruments are for prediction markets; `CappedOptionFeeModel`,
`TieredNotionalOptionFeeModel`, and the option-spread leg parser in
`PerContractFeeModel` are equities/futures options machinery; the FX
(`GBP/USD`) and futures (`ES` multiplier) fixtures throughout the risk-sizing
tests are asset classes out of scope here. Skipped or only skimmed:
`adapters/` (per-venue connector code, not mechanism), `analysis/` (Sharpe/
Sortino style stats, orthogonal to this harvest), `cache/`, `common/`,
`core/`, `data/`, `indicators/`, `live/`, `persistence/`, `serialization/`,
`system/`, `trading/` — these are the engine's plumbing (message bus,
clocks, data catalog, live adapters) rather than trading mechanism, and the
whole Rust/PyO3 kernel is a far heavier engine than a single-process bot
needs; the value here is the algorithms, not the architecture.
