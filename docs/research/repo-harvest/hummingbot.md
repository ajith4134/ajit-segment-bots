# hummingbot/hummingbot — read 2026-08-20

Licence: Apache 2.0. Language: Python, with hot paths (order book, strategies,
order tracking) written as Cython `.pyx`/`.pxd`. One line: an open-source
market-making / arbitrage trading bot framework with 100+ CEX/DEX connectors
and a built-in paper-trade exchange. Clone commit sha:
`2bfaccc48dd49e71a5b6d9b3011808e127dd00cd`.

## Mechanisms worth stealing (ranked)

1. **Order-book-walk fill simulation.** `hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx:PaperTradeExchange.c_execute_buy/c_execute_sell`
   call `hummingbot/core/data_type/order_book.pyx:OrderBook.simulate_buy/simulate_sell`,
   which walks the live ask/bid book level by level, consuming each `OrderBookRow`
   until the requested amount is filled, and returns the exact rows consumed.
   The caller then computes `avg_price = sum(price*amount for entry) / amount`.
   This is real slippage from live book depth, not a flat bps assumption. Direct
   reference for `paper-fill-simulator`, which currently only names `cost-estimate`
   as an input — this shows a cheaper mode for market orders that needs no
   separate cost model.

2. **Avellaneda-Stoikov inventory-aware spread.**
   `hummingbot/strategy/avellaneda_market_making/avellaneda_market_making.pyx:AvellanedaMarketMakingStrategy.c_calculate_reservation_price_and_optimal_spread`.
   `reservation_price = mid_price - q * gamma * vol * time_left_fraction`;
   `optimal_spread = gamma*vol*time_left_fraction + 2*ln(1 + gamma/kappa)/gamma`.
   `q = (base_balance - target_inventory) / inventory_scale` is *relative*
   inventory skew (so portfolio size doesn't change the reservation price);
   `gamma` is risk aversion (config); `vol` is live instantaneous volatility from
   `InstantVolatilityIndicator`; `kappa`/`alpha` (order-book liquidity/depth
   decay) are measured live via `TradingIntensityIndicator`
   (`hummingbot/strategy/__utils__/trailing_indicators/trading_intensity.pyx`),
   read at `c_measure_order_book_liquidity`. This is a formula, not a rule, for
   the inventory term our `stop-target-plan`/`leverage-choice` don't have.

3. **Linear inventory-skew size ratio (simpler alternative to #2).**
   `hummingbot/strategy/pure_market_making/inventory_skew_calculator.pyx:c_calculate_bid_ask_ratios_from_base_asset_ratio`.
   Computes `target_base_asset_value = portfolio_value * target_pct`, then
   piecewise-linearly interpolates (`np.interp`) a bid/ask size multiplier in
   `[0, 2]` over a symmetric range (`base_asset_range = order_size *
   inventory_range_multiplier`) around that target — so ask size scales up and
   bid size scales down as base-asset exposure grows past target. Auditable,
   no live vol/kappa needed; a good first version before #2.

4. **Order-refresh tolerance (suppress needless requote churn).**
   `hummingbot/strategy/pure_market_making/pure_market_making.pyx:PureMarketMakingStrategy.c_is_within_tolerance`
   / `c_cancel_active_orders`. Only cancels+replaces a resting order if the new
   proposed price differs from the live order price by more than
   `order_refresh_tolerance_pct` (Decimal, default `-1` = always refresh).
   Cuts order-request/cancel churn and rate-limit burn on every tick where the
   market barely moved.

5. **Two independent, composable resting-order safety cancels.**
   `c_cancel_active_orders_on_max_age_limit` cancels any order older than
   `max_order_age` (default 1800s); `c_cancel_orders_below_min_spread` cancels
   any resting order whose distance from current price has compressed below
   `minimum_spread`. Two small checks, not one combined "is this order still
   good" god-check.

6. **Price band as a pure function circuit breaker on quoting.**
   `hummingbot/strategy/pure_market_making/moving_price_band.py:MovingPriceBand.update/check_price_floor_exceeded/check_price_ceiling_exceeded`,
   invoked from `c_apply_price_band`/`c_apply_moving_price_band`. Refuses buys
   above a ceiling / sells below a floor; the moving variant recenters
   floor/ceiling to ± `price_ceiling_pct`/`price_floor_pct` of current price
   every `price_band_refresh_time` (default 86400s). A risk-limit gate
   expressed as a pure function of price + time.

7. **Explicit, closed order-state machine.**
   `hummingbot/core/data_type/in_flight_order.py:OrderState` enum
   (`PENDING_CREATE, OPEN, PENDING_CANCEL, CANCELED, PARTIALLY_FILLED, FILLED,
   FAILED, ...`), transitioned only via immutable `OrderUpdate`/`TradeUpdate`
   NamedTuples applied in
   `hummingbot/connector/client_order_tracker.py:ClientOrderTracker.process_order_update/process_trade_update`.
   A near-literal T-5 implementation for the `fill`/`position` data types.

8. **Lost-order detection via repeated not-found, not a single miss.**
   `hummingbot/connector/client_order_tracker.py:ClientOrderTracker.process_order_not_found`.
   Counts consecutive "order not found" responses per `client_order_id`; only
   past `lost_order_count_limit` (default 3) does it mark the order `FAILED`
   and move it into a distinct `_lost_orders` bucket — never dropped silently,
   never declared dead on one API hiccup.

9. **Declarative, weighted, per-endpoint rate-limit budgets.**
   `hummingbot/connector/exchange/binance/binance_constants.py` `RATE_LIMITS`,
   e.g. `RateLimit(limit_id=REQUEST_WEIGHT, limit=6000, time_interval=60s)`,
   `RateLimit(limit_id=ORDERS, limit=100, time_interval=10s)`,
   `RateLimit(limit_id=ORDERS_24HR, limit=200000, time_interval=86400s)`,
   consumed through `hummingbot/core/api_throttler/async_throttler.py:AsyncThrottler`
   against each endpoint's declared weight. Named budgets with time windows per
   endpoint class, not one venue-wide counter.

10. **Trading-rule quantization from live exchange filters.**
    `hummingbot/connector/exchange/binance/binance_exchange.py:BinanceExchange._format_trading_rules`
    parses `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `MIN_NOTIONAL.minNotional`
    per symbol into a `TradingRule`; every proposed order price/size is rounded
    through `c_quantize_order_price`/`c_quantize_order_amount` before being
    sent. Prevents a `sized-order` from ever reaching the venue with an invalid
    price or size increment.

11. **Batch collateral locking across proposed orders in one tick.**
    `hummingbot/connector/budget_checker.py:BudgetChecker.adjust_candidates` /
    `adjust_candidate_and_lock_available_collateral`. Computes required
    collateral (including fee token) per candidate order, locks it against
    available balance so a second candidate in the same batch can't claim the
    same free balance twice, then `reset_locked_collateral()` after the batch.
    Sizing one order in isolation is wrong when N are proposed in the same
    tick — this is the missing piece behind `position-sizer`.

12. **Profitability-threshold kill switch as an independent loop.**
    `hummingbot/core/utils/kill_switch.py:ActiveKillSwitch.check_profitability_loop`.
    Polls total realized profitability every 10s; crosses a configured
    `kill_switch_rate` (±%) and calls `trading_core.shutdown()`. Crude
    (session PnL % only, no drawdown curve) but a literal worked example of a
    `drawdown-breaker` as one small loop, not baked into the strategy class.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `paper-fill-simulator` | `hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx` (`c_execute_buy`/`c_execute_sell`), `hummingbot/core/data_type/order_book.pyx` (`simulate_buy`/`simulate_sell`) | walks live order-book depth to derive a real weighted-average fill price instead of applying a precomputed `cost-estimate` |
| `stop-target-placer` | `hummingbot/strategy/avellaneda_market_making/avellaneda_market_making.pyx:c_calculate_reservation_price_and_optimal_spread` | derives spread from live volatility + live order-book liquidity decay (kappa) + relative inventory skew, not just a fixed distance from a liquidation map |
| `order-state-poller` | `hummingbot/connector/client_order_tracker.py:ClientOrderTracker.process_order_not_found`, `_process_order_update` | tolerates N consecutive not-found responses before declaring an order lost; keeps a separate `lost_orders` bucket instead of deleting state on first miss |
| `venue-rate-budgeter` | `hummingbot/connector/exchange/binance/binance_constants.py` (`RATE_LIMITS`), `hummingbot/core/api_throttler/async_throttler.py` (`AsyncThrottler`) | declares named, weighted budgets per endpoint class (order placement, order status, market data) rather than one venue-wide counter |
| `position-sizer` | `hummingbot/connector/budget_checker.py:BudgetChecker.adjust_candidates` | locks collateral across a batch of candidate orders proposed in the same tick so two orders can't both claim the same free balance |
| `drawdown-breaker` | `hummingbot/core/utils/kill_switch.py:ActiveKillSwitch.check_profitability_loop` | a literal worked (if crude) reference: polls realized PnL % on an interval and halts past a threshold |

## New part proposals

1. **`book-walk-fill-pricer`**
   block: `paper-live-trading`
   consumes: `order-request`, `order-book-snapshot`
   produces: NEW `fill-price-estimate`
   responsibility: compute the weighted-average price a market order of a given
   size would actually fill at by walking live order-book depth.
   evidence: `hummingbot/core/data_type/order_book.pyx:OrderBook.simulate_buy/simulate_sell`
   note: `paper-fill-simulator` could consume `fill-price-estimate` directly for
   market orders instead of `cost-estimate`, skipping a separate cost model.

2. **`order-not-found-debouncer`**
   block: `execution-venue-adapter`
   consumes: `order-request`, NEW `venue-order-lookup-miss`
   produces: `order-reject-reason`
   responsibility: count consecutive "order not found" responses per order and
   only classify it as lost/rejected past a threshold, instead of on first miss.
   evidence: `hummingbot/connector/client_order_tracker.py:ClientOrderTracker.process_order_not_found`

3. **`quote-refresh-tolerance-gate`**
   block: `execution-venue-adapter`
   consumes: `order-request`, `position`, NEW `quote-refresh-tolerance`
   produces: `order-request` (suppressed or passed through)
   responsibility: suppress a cancel+replace request when the proposed price is
   within tolerance of the still-resting order's price, to cut venue calls.
   evidence: `hummingbot/strategy/pure_market_making/pure_market_making.pyx:c_is_within_tolerance`

4. **`inventory-skew-spread-adjuster`**
   block: `risk-capital-allocation`
   consumes: `position`, `account-balance`, `volatility-forecast`, NEW `liquidity-decay-rate`
   produces: `stop-target-plan` (widened/shifted)
   responsibility: shift and widen the stop/target plan away from mid price in
   proportion to how far current inventory sits from its target.
   evidence: `hummingbot/strategy/avellaneda_market_making/avellaneda_market_making.pyx:c_calculate_reservation_price_and_optimal_spread`

5. **`endpoint-weight-budgeter`**
   block: `execution-venue-adapter`
   consumes: `order-request`, NEW `venue-endpoint-weight-table`
   produces: `venue-rate-budget`
   responsibility: track a separate weighted budget per venue endpoint class
   (order placement, order status, market data) rather than one global counter.
   evidence: `hummingbot/connector/exchange/binance/binance_constants.py` (`RATE_LIMITS`), `hummingbot/core/api_throttler/async_throttler.py`

6. **`batch-collateral-locker`**
   block: `risk-capital-allocation`
   consumes: `sized-order` (a batch proposed in one tick), `account-balance`
   produces: `sized-order` (resized/refused per item)
   responsibility: lock collateral against each candidate order in a batch as
   it's accepted so a later candidate in the same batch can't double-claim the
   same free balance.
   evidence: `hummingbot/connector/budget_checker.py:BudgetChecker.adjust_candidates`

## Anti-patterns seen

- `PureMarketMakingStrategy` (`hummingbot/strategy/pure_market_making/pure_market_making.pyx`,
  1330 lines) is one `cdef class` owning spread calc, inventory skew, budget
  constraint, order optimization, price bands, hanging-order tracking, and
  order-lifecycle events, all as `c_apply_*` methods on one object with 60+
  instance fields. Exactly what T-6 forbids — one part grown many
  responsibilities. Port the individual formulas, not the class shape.
- Strategy objects hold direct live references into their connector
  (`self._market_info.market`, then call `market.c_get_balance(...)`,
  `market.c_quantize_order_price(...)` synchronously) rather than exchanging
  only named data types. Violates T-4 — a part should name data it needs, not
  hold a handle to another part.
- `ActiveKillSwitch.check_profitability_loop` (`hummingbot/core/utils/kill_switch.py`)
  calls `self._trading_core.shutdown()` directly from the detector. Violates
  T-2: the thing that measures whether to halt is also the thing that acts —
  no separate `trading-halt`/risk-limit hand-off for something else to enforce.
- Module-level mutable logger singletons set via `global` inside a classmethod
  (`pmm_logger = None`, repeated per strategy/connector file) — hidden shared
  state invisible to any part boundary; harmless in a monolith, incompatible
  with T-3 (off must release everything, including anything reachable through
  a shared global).

## Not useful here

Hummingbot is spot/perpetual market-making and arbitrage across dozens of
CEX/DEX connectors: no options surface, no price-forecast/ML layer, and no
per-order paper-vs-live routing — paper trading is a separate connector class
(`PaperTradeExchange`) the whole bot points at, not a per-order `money-mode`
decision. Its 100+ individual exchange connector implementations
(`hummingbot/connector/exchange/*`, `hummingbot/connector/derivative/*`) were
skipped except `binance`/`binance_perpetual` as representative samples — they
repeat the same base-class contract and aren't worth reading individually.
The Gateway/DEX code and `amm_arb` strategy (`hummingbot/connector/gateway/`,
`hummingbot/strategy/amm_arb/`) were also skipped as out of scope for a
CEX-based intraday bot.
