# Lumiwealth/lumibot — read 2026-08-20

GPL-3.0. Python. A broker-agnostic algo-trading framework: one `Strategy` class
runs unmodified against a backtesting broker or a dozen live brokers (Alpaca,
IBKR, Tradier, Schwab, ccxt-based crypto venues, etc.). Clone commit
`07b2514700f473c84258865e5ed11144db38090f`.

## Mechanisms worth stealing (ranked)

1. **Closed-set order-status translation across venues.** `VALID_STATUS` is a
   fixed list of 16 canonical statuses (`unprocessed`, `new`, `open`,
   `submitted`, `fill`, `partial_fill`, `cancelling`, `canceled`, `error`,
   `cash_settled`, `assigned`, `assignment`, `exercise`, `exercised`,
   `expired`, `unknown`). `STATUS_ALIAS_MAP` maps ~25 broker-native strings
   (Alpaca's `pending_new`, `accepted_for_bidding`, `done_for_day`; IBKR's
   `presubmitted`, `apipending`, `pendingcancel`; Tradier's `pending`,
   `rejected`) onto that closed set before anything downstream sees them.
   `lumibot/entities/order.py:114-162` (`VALID_STATUS`, `STATUS_ALIAS_MAP`),
   `Order.status` setter at `order.py:1111-1116`. Directly what T-5 asks for:
   every venue speaks a different status dialect; nothing should see raw venue
   text.

2. **SMART_LIMIT: a limit order that walks toward the market instead of
   sitting still.** Configured by preset (`FAST`=3 steps/5s, `NORMAL`=4
   steps/10s, `PATIENT`=5 steps/20s — `smart_limit.py:15-18`), it starts at
   the mid and steps toward the aggressive edge (bid for sells, ask for buys)
   by `final_price_pct` of the spread over the step count, via
   `build_price_ladder(mid, final_price, step_count)`
   (`lumibot/tools/smart_limit_utils.py:75-82`), each price rounded to the
   inferred tick with `round_to_tick` (ROUND_CEILING for buys, ROUND_FLOOR for
   sells, `smart_limit_utils.py:23-33`). Backtest fill price is
   `mid ± slippage_amount` inside the spread
   (`backtesting_broker.py:4536-4541`, `expected_fill_price`). This is a
   concrete, numeric alternative to "market order + fixed slippage" for an
   intraday bot that cares about execution cost.

3. **Tick-size inferred from the quote itself, not declared.** `infer_tick_size`
   checks whether both bid and ask are exact multiples of 0.01, 0.05, or 0.1
   and falls back to 0.01 (`smart_limit_utils.py:5-12`). Useful anywhere a
   venue doesn't hand you a price-increment field (many crypto venues don't,
   or it varies by pair).

4. **Per-side slippage as an explicit configured list, summed.**
   `TradingSlippage(amount=...)` objects are attached separately to
   `buy_trading_slippages` / `sell_trading_slippages` on the strategy;
   `_get_strategy_slippage_amount` sums whichever list applies to the order's
   side (`backtesting_broker.py:3958-3977`). Slippage is asymmetric by
   construction (buys pay up, sells pay down), which single-scalar slippage
   models usually get wrong.

5. **Fee model separates flat, percent, and per-contract, each gated by
   maker/taker.** `TradingFee(flat_fee, percent_fee, per_contract_fee, maker,
   taker)` — a limit order (maker) and a market order (taker) can carry
   different registered fee objects (`lumibot/entities/trading_fee.py:6-49`).
   Per-contract fee is quantity-multiplied, which the docstring gives a worked
   example for ($0.65 × 40 contracts = $26).

6. **Backtest data access is hard-bounded at the replay frontier.** `Data`
   stores `datetime_end` from the loaded window and refuses any lookup past
   it: `get_bars`/`get_last_price` raise with an explicit gap message rather
   than silently returning the nearest available bar
   (`lumibot/entities/data.py:900-944`, comment "to avoid lookahead" at
   line 1192; frontier enforced via `idx.searchsorted(self.datetime_end,
   side='right')` at line 398). This is a real reference implementation for a
   lookahead guard, not just a design intention.

7. **Position reconciliation is venue-truth-wins, computed as a diff, not a
   merge.** `Broker.sync_positions` pulls broker positions, updates or
   inserts local ones that match, and **removes** any local position whose
   asset isn't reported by the broker at all (unless it's a quote asset)
   (`lumibot/brokers/broker.py:1050-1096`). The local book is never allowed to
   drift ahead of what the venue actually reports.

8. **Pre-flight order rejection before the network call.** `Ccxt._submit_order`
   validates quantity > 0, order type/class validity for crypto markets,
   pair existence, and rounds to exchange precision *before* submitting —
   only then does it catch venue exceptions and call `order.set_error(e)`
   (`lumibot/brokers/ccxt.py:418-611`, e.g. line ~453 zero-quantity check,
   ~452 "does not have a quantity", pair-existence check ~ line where
   `order.set_error("No market for pair.")` appears). Cheap local checks catch
   most rejects before they cost a round trip.

9. **A venue-specific rate throttle expressed as one field.**
   `binance_all_orders_rate_limit = 5` (`ccxt.py:54`) is read by
   `_pull_broker_all_orders` to sleep between calls when hitting Binance's
   all-orders endpoint (`ccxt.py:375-390`). Trivial, but it's the shape a
   `venue-rate-budgeter` needs: one number, one place, per venue.

10. **Every trading iteration is journaled by a decorator, not by hand.**
    `trace_stats` wraps `_on_trading_iteration` (and other lifecycle methods):
    it snapshots portfolio value before, runs the method, updates portfolio
    value again, and calls `_trace_stats` — so no strategy author can forget
    to record what happened this tick
    (`lumibot/strategies/strategy_executor.py:1178-1194`). The lifecycle
    itself is a closed set (`_before_market_opens`, `_before_starting_trading`,
    `_on_trading_iteration`, `_before_market_closes`, `_after_market_closes`,
    `_on_strategy_end`, `_on_bot_crash`, `strategy_executor.py:1299-1459`) run
    identically by the same `StrategyExecutor` whether the broker underneath
    is `BacktestingBroker` or a live one — the actual mechanism behind
    paper/live parity in this codebase.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `order-state-poller` | `lumibot/entities/order.py:114-219` (`Order.OrderStatus`, `STATUS_ALIAS_MAP`) | Names the exact closed set of statuses to poll toward, and the ~25 broker-native aliases that must collapse into it — our part description says "follow until fill/cancel" but not what the state vocabulary itself should contain. |
| `order-reject-classifier` | `lumibot/brokers/ccxt.py:418-611` (`Ccxt._submit_order`) | Classifies (and refuses) most rejects locally — zero/negative quantity, invalid order type for the market, non-existent pair, sub-minimum size after precision rounding — before the order ever reaches the venue, not just after a venue reject comes back. |
| `fill-reconciler` | `lumibot/brokers/broker.py:1050-1096` (`Broker.sync_positions`) | Makes the venue authoritative: a local position not confirmed by the broker's report is deleted, not merely flagged. |
| `paper-fill-simulator` | `lumibot/backtesting/backtesting_broker.py:4521-4572` (`_try_fill_with_quote`), `lumibot/tools/smart_limit_utils.py` | Fills at mid ± an explicit, side-specific slippage amount inside the spread, with a tick-rounded, steppable price ladder — not just "last price + noise". |
| `execution-cost-model` | `lumibot/entities/trading_fee.py`, `trading_slippage.py` | Separates flat fee, percent-of-notional fee, and per-contract fee, each independently switchable between maker and taker application. |
| `lookahead-auditor` | `lumibot/entities/data.py:900-944,1192` | A concrete frontier check (`datetime_end`, `searchsorted(..., side='right')`) rather than a policy statement — any lookup past the loaded window raises instead of returning stale/future data. |
| `venue-rate-budgeter` | `lumibot/brokers/ccxt.py:54,375-390` | Shows the budget can be a single per-venue constant (`binance_all_orders_rate_limit = 5`) rather than a general token-bucket — cheap and sufficient for one hot endpoint. |
| `trade-lifecycle-recorder` | `lumibot/strategies/strategy_executor.py:1178-1194` (`trace_stats`) | Journals every iteration via a decorator wrapped around the lifecycle method itself, so recording can't be skipped by a strategy author — an enforcement mechanism, not just a call site. |

## New part proposals

- **`limit-price-walker`** — block: `execution-venue-adapter`. consumes:
  `order-request`, `market-data`. produces: `order-request` (revised, more
  aggressive price). Responsibility: step an unfilled limit order's price
  toward the market by a bounded amount on a fixed cadence, instead of
  sitting at one price until timeout or being replaced by a market order.
  Evidence: `lumibot/entities/smart_limit.py`,
  `lumibot/tools/smart_limit_utils.py:build_price_ladder`. Distinct from
  `order-resubmitter` (which only handles transient venue rejects) and from
  `stop-order-manager` (which moves protective stops, not entry limits).

- **`tick-size-resolver`** — block: `market-data-feed`. consumes:
  `order-book-snapshot`. produces: `price-increment` (NEW). Responsibility:
  infer the valid price increment for a symbol from the spacing of its live
  bid/ask when the venue doesn't declare one, so limit/stop prices round to a
  price the venue will actually accept. Evidence:
  `lumibot/tools/smart_limit_utils.py:infer_tick_size`.

- **`venue-order-status-translator`** — block: `execution-venue-adapter`.
  consumes: `raw-venue-order-status` (NEW). produces: `fill`,
  `order-reject-reason`. Responsibility: collapse one venue's native status
  vocabulary into the system's closed order-status set before anything reads
  it as a `fill`. Currently this translation is embedded inside each broker's
  order-polling code (`order.py:132-162`'s `STATUS_ALIAS_MAP` is itself
  venue-agnostic, but every broker subclass — `alpaca.py`, `ccxt.py`,
  `tradier.py` — repeats its own parsing of the raw payload into that map,
  e.g. `ccxt.py:326-352` `_parse_broker_order`). Pulling it into its own part
  per T-6 keeps `ccxt-order-router` from also owning status parsing.

## Anti-patterns seen

- **`Broker(ABC)` is a god object.** `lumibot/brokers/broker.py` is 3,370
  lines with 127 methods on one class: order-lifecycle processing
  (`_process_new_order`, `_process_filled_order`, `_process_error_order`),
  position reconciliation (`sync_positions`, `_pull_positions`), market-hours
  calendar math (`is_market_open`, `market_hours`), options-chain and Greeks
  retrieval (`get_chains`, `get_greeks`, `get_multiplier`), telemetry
  (`_telemetry_snapshot`, `_start_runtime_telemetry`), and stream/cleanup
  bookkeeping (`cleanup_streams`, `_cleanup_old_tracking_data`). Every
  concrete broker (`Ccxt(Broker)` at `ccxt.py:39`, plus `alpaca.py`,
  `interactive_brokers.py`, `tradier.py`, `schwab.py`) inherits all 127
  methods regardless of asset class — a crypto-only venue still carries
  options-chain and equities-calendar methods. T-6 (one part, one job) and by
  extension T-1 (uniform parts) are both broken: this is many parts welded
  into one class rather than a small part composed with others.

- **`Strategy` is a second god object.** `lumibot/strategies/strategy.py` is
  6,002 lines, 149 methods: user-facing trading API, cash accounting
  (`adjust_cash`, `withdraw_cash`, `deposit_cash`, `configure_cash_financing`),
  position tracking, order construction, and scheduling all live on one
  class. A part that both places orders and manages cash financing rates has
  grown a second responsibility per T-6's own example.

- **Parts read broker-internal state directly instead of by data type.**
  `strategy_executor.py`'s lifecycle methods call back into `self.strategy`
  and `self.broker` freely (e.g. `trace_stats` reaches into
  `self.strategy._update_portfolio_value()` and
  `self.strategy._apply_daily_cash_financing_if_needed()` — internal methods,
  not a passed data type). Under T-4 a part should only ever receive named
  data types, never reach through an object graph into another part's
  internals.

## Not useful here

Most of the broker surface (`interactive_brokers.py`, `interactive_brokers_rest.py`,
`schwab.py`, `tradier.py`, `tradovate.py`, `polymarket.py`, `projectx.py` —
55-141KB each) and most of `data_sources/`, `resources/`, `fundamentals/`,
and `macro/` are equities/futures/options-on-US-exchanges specific (market
calendars, corporate actions, options chains, cash financing on margin) and
don't apply to a 24/7 crypto spot/futures/options bot; skipped reading these
in full. `example_strategies/` was skipped as sample code, not mechanism.
The `ccxt.py` broker (crypto) and the backtesting/order/position/fee entities
above are the load-bearing parts of this repo for this project.
