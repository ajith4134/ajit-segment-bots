# jesse-ai/jesse — read 2026-08-20

MIT licence. Python. A crypto backtesting/paper-trading framework where user
strategies subclass one `Strategy` base class; the same strategy code runs in
backtest, paper and (for licensed users) live mode via mode-flag branching
rather than separate code paths. Clone commit sha: `682b17148b5e3949e7c850ead207c53968b0ccf2`.

Note: this open-source repo has no live_mode.py or risk.py — live-market
execution drivers (`app.live_drivers.*` in `jesse/services/api.py`) are
injected from config and are not present in this tree. Everything below is
backtest/paper-trading engine, which is what's readable here.

## Mechanisms worth stealing (ranked)

1. **Intra-candle order resolution by candle-splitting.** When more than one
   pending order's price falls inside the same 1-minute candle's high/low
   range, `_sort_execution_orders` (`jesse/modes/backtest_mode.py:1481`)
   heuristically orders them: any order exactly at the open executes first,
   then it assumes price ran one direction before the other based on whether
   the candle is red or green (`is_red = open_price > close_price`; if red,
   orders above open sorted ascending then below open sorted descending — i.e.
   price is assumed to rise first, then fall). `_simulate_price_change_effect`
   (`jesse/modes/backtest_mode.py:926`) then walks the candle order-by-order,
   splitting it at each execution price via `candle_service.split_candle`
   (`jesse/services/candle_service.py:88`) so the *next* order is checked
   against a post-split, already-narrowed candle rather than the original one.
   This is a genuine defence against "which of stop-loss/take-profit hit
   first" ambiguity, the single most common backtest-inflation bug. Directly
   applicable to `execution-cost-model`/`instruction-replayer`.

2. **Liquidation as a manufactured order, checked every candle.**
   `_check_for_liquidations` (`jesse/modes/backtest_mode.py:986`) tests whether
   the candle's high/low range crossed `position.liquidation_price`
   (`jesse/models/Position.py` property) and, if so, synthesizes a MARKET
   `reduce_only` order priced at `bankruptcy_price` and executes it
   immediately — modelling that a liquidated position exits at the exchange's
   bankruptcy price, not at a "nice" stop price. Isolated-margin liquidation
   price formula: `entry_price * (1 - 1/leverage + 0.004)` for longs (0.004 =
   maintenance-margin buffer), `entry_price * (1 + 1/leverage - 0.004)` for
   shorts (`jesse/models/Position.py:liquidation_price`). Bankruptcy price
   drops the 0.004 buffer entirely.

3. **Price-gap normalisation across candle boundaries.**
   `_get_fixed_jumped_candle` (`jesse/modes/backtest_mode.py:902`) detects when
   a candle's open doesn't match the prior candle's close (a feed gap/jump)
   and rewrites the new candle's open (and high/low if the jump crossed them)
   to the previous close — otherwise a stop/limit sitting between the two
   prices would never be seen as "crossed" even though live execution would
   have caught it on the gap.

4. **One strategy object, mode-branching, not mode-duplication.** `Strategy`
   (`jesse/strategies/Strategy.py`) is the single class instantiated in every
   mode. Mode is read via `jh.is_livetrading()` / `jh.is_backtesting()` /
   `jh.is_paper_trading()` / `jh.is_live()` helpers and only changes small
   execution details: price rounding for live order placement
   (`_submit_buy_orders`, line 653), whether fills are simulated locally vs.
   awaited from the exchange (`_simulate_market_order_execution`, line 1144),
   and sleep/retry loops for live order-ack latency (`_check`, line 1061).
   The entry/exit/position-update control flow (`_execute` → `before()` →
   `_check()` → `after()`, line 1308) is identical across modes — this is the
   concrete implementation of "paper and live share one code path" that the
   `paper-live-trading` block assumes but doesn't yet describe mechanically.

5. **Venue driver picked by mode, same interface both ways.** `API.__init__`
   (`jesse/services/api.py:9`) only calls `initiate_drivers()` outside live
   mode; `initiate_drivers` picks `Sandbox` (`jesse/exchanges/sandbox/Sandbox.py`)
   for backtest/paper and a config-named live driver class for live trading —
   both implement the same abstract `Exchange` interface
   (`jesse/exchanges/exchange.py`: `market_order`, `limit_order`, `stop_order`,
   `cancel_order`, `cancel_all_orders`, `_fetch_precisions`). `Broker`
   (`jesse/services/broker.py`) calls `self.api.market_order(...)` etc.
   uniformly regardless of which driver answers. This is a clean reference
   implementation of `order-destination-router` + venue-adapter separation.

6. **Reduce-only fill capped to remaining position, not requested qty.**
   `order_service.execute_order` (`jesse/services/order_service.py:49`): if a
   `reduce_only` order's qty exceeds the currently open position's qty (e.g. a
   stop-loss left oversized after a partial take-profit already fired), the
   filled qty is capped to exactly close the position (`order.filled_qty =
   -position_qty`) instead of over-filling or flipping the position's side.
   Prevents a whole class of paper-fill-simulator bugs where a stop
   unintentionally reverses a position.

7. **Expectancy decomposed into win-rate and win/loss magnitude, computed on
   the closed-trade ledger, not asserted.** `metrics.trades`
   (`jesse/services/metrics.py:302`): `expectancy = avg_win * win_rate -
   avg_loss * (1 - win_rate)`; `ratio_avg_win_loss = average_win /
   average_loss`; win rate is also split by long vs short
   (`win_rate_longs`, `win_rate_shorts`, lines 339-345). Directly matches the
   blueprint's `expectancy-decomposer` responsibility — this is a working
   formula to copy rather than invent.

8. **Streak tracking via a vectorised cumulative-sign trick, not a loop.**
   `metrics.trades` (`jesse/services/metrics.py:320-329`) computes running
   winning/losing streaks over the whole trade array with `np.clip` +
   `cumsum` + `np.maximum.accumulate`, avoiding an O(n) Python loop over
   trades for a report computed after every run. Worth copying verbatim for
   `bot-scorekeeper`'s streak fields if it needs them at scale.

9. **Order-modification detection re-diffs on every position update, not just
   on new bars.** `_detect_and_handle_entry_and_exit_modifications`
   (`jesse/strategies/Strategy.py:912`) compares the strategy's current
   `self.buy`/`self.stop_loss`/`self.take_profit` arrays against the
   previously-submitted copies (`self._buy` etc.) using a hand-rolled
   `_np_array_equal` (line 29, chosen over `np.array_equal` specifically to
   avoid ufunc dispatch overhead on tiny arrays checked every candle) and only
   cancels+resubmits orders that actually changed. Useful pattern for
   `stop-order-manager`: don't blindly replace stop orders every tick, diff
   first.

10. **Available margin computed live from held positions' unrealised PnL, not
    cached.** `FuturesExchange.available_margin`
    (`jesse/models/FuturesExchange.py:51`) sums `position.total_cost -
    position.pnl` across every open position on the exchange to get spent
    margin, then subtracts from wallet balance — recomputed every access
    rather than tracked as running state that could drift. Applicable to
    `exposure-limiter`/`account-balance` bookkeeping discipline.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `paper-fill-simulator` | `jesse/modes/backtest_mode.py:_simulate_price_change_effect`, `_get_executing_orders`, `_sort_execution_orders` | Names the exact heuristic for resolving fill order when multiple pending orders sit inside one candle (open-price ties first, then red/green candle direction assumption), and physically splits the candle at each fill so later orders check against a narrowed range — our description says "fill at the live price" but not how same-bar ordering ambiguity is resolved |
| `paper-fill-simulator` | `jesse/services/order_service.py:execute_order` | Caps a reduce-only fill to the remaining open position size rather than the order's requested qty, so a stale/oversized stop can't flip the position |
| `stop-target-placer` (partially) | `jesse/models/Position.py:liquidation_price`, `bankruptcy_price` | Gives the concrete isolated-margin liquidation/bankruptcy price formulas (entry × (1 ∓ 1/leverage ± 0.004)) our `liquidation-cluster-mapper`/`stop-target-placer` parts can use to keep stops off the crowd's liquidation levels with venue-realistic numbers |
| `order-destination-router` | `jesse/services/api.py:API.market_order/limit_order/stop_order`, `jesse/exchanges/sandbox/Sandbox.py` | Concrete pattern for "same call signature, different driver by mode" — paper/backtest driver (`Sandbox`) and a would-be live driver both implement one abstract interface (`jesse/exchanges/exchange.py`), so the router only ever picks *which* driver, never branches on behaviour |
| `execution-cost-model` | `jesse/services/order_service.py:execute_order` (fee lines) | Fee is computed as `fee_rate * notional` where `fee_rate` comes from `env.exchanges.<exchange>.fee` config, applied identically to every non-live fill — a minimal but working cost-estimate baseline (no slippage modelled — see Anti-patterns) |
| `expectancy-decomposer` | `jesse/services/metrics.py:trades` | Working formula: `expectancy = avg_win*win_rate - avg_loss*(1-win_rate)`, plus win-rate split by long/short and largest win/loss — more decomposed than our current one-line description |
| `exposure-limiter` | `jesse/models/FuturesExchange.py:available_margin` | Computes available margin by summing every open position's `total_cost - pnl` against wallet balance on every access, never cached — a concrete "recompute, don't trust stale state" pattern |

## New part proposals

- **`intra-bar-fill-sequencer`** — block: `backtesting`. consumes:
  `market-data` (the candle), NEW `pending-order-set` (candidate: could reuse
  `order-request` list already tracked by the block). produces: `fill`
  (ordered, one per resolved execution) or NEW `fill-sequence`. Responsibility:
  when more than one pending order's price falls inside one simulated bar,
  decide and record which one would have filled first, splitting the bar so
  later checks see the narrowed range — this is what `execution-cost-model`
  and `instruction-replayer` currently have no named part for. Evidence:
  `jesse/modes/backtest_mode.py:_sort_execution_orders`,
  `_simulate_price_change_effect`.

- **`liquidation-simulator`** — block: `backtesting` (or `execution-venue-adapter`
  for the paper case). consumes: `position`, `market-data`, `liquidation-map`.
  produces: NEW `liquidation-fill` (a `fill` variant priced at bankruptcy price,
  not market price). Responsibility: when a simulated or paper position's
  liquidation price is crossed by the candle's range, manufacture the
  closing fill at the bankruptcy price rather than letting the position
  silently go negative-margin. Evidence: `jesse/modes/backtest_mode.py:_check_for_liquidations`,
  `jesse/models/Position.py:liquidation_price/bankruptcy_price`.

- **`feed-jump-normalizer`** — block: `market-data-feed`. consumes:
  `market-data`. produces: `market-data` (corrected). Responsibility: when a
  new candle's open doesn't match the prior candle's close, rewrite the open
  (and high/low if the jump crossed them) to the prior close, so a stop/limit
  sitting in the gap is still recognised as crossed rather than skipped over.
  Distinct from `feed-gap-detector`, which flags *missing* data — this
  corrects *discontinuous but present* data. Evidence:
  `jesse/modes/backtest_mode.py:_get_fixed_jumped_candle`.

## Anti-patterns seen

- **No slippage model at all.** Simulated market orders fill exactly at the
  candle's close/current price (`order_service.execute_order`,
  `jesse/modes/backtest_mode.py`), and limit/stop orders fill exactly at their
  set price when the candle range includes it. There is no spread, no size
  impact, nothing resembling `cost-estimate`'s "spread crossed, slippage,
  fees" — only fees are modelled. Copying jesse's fill logic wholesale would
  silently make our `execution-cost-model` cheaper than reality.
- **God object risk in `Strategy`.** The single `Strategy` class
  (`jesse/strategies/Strategy.py`, 1874 lines) owns entry logic, exit logic,
  order preparation, chart rendering, ML feature export, DNA/hyperparameter
  handling, and lifecycle hooks all in one class — the opposite of T-6 (grow
  by adding parts). It works for jesse because a strategy genuinely is one
  thing to its author, but nothing here should be copied as an architecture
  pattern, only as individual mechanisms.
- **Mode read via scattered global-state calls, not passed data.**
  `jh.is_livetrading()` / `jh.is_backtesting()` / `jh.is_live()` are called
  directly, deep inside methods on `Strategy`, `Position`, `order_service`,
  `FuturesExchange` — dozens of call sites reach into global config/state
  rather than the mode being a value passed in. This is exactly what our
  `money-mode` data type and `money-mode-reader` part exist to avoid (T-4: a
  part should not reach into ambient global state to learn what mode it's
  in).

## Not useful here

Options (jesse has no implied-vol surface, no options instrument model — it's
spot/futures perpetuals/futures contracts only), no sentiment/whale-flow/social
data sources, no LLM integration, and no live execution code is present in
this open-source tree at all (live drivers are injected from private config,
so `execution-venue-adapter`'s error-classification and rate-budgeting
responsibilities have nothing to read here). The backtest fill/liquidation
mechanics above are the useful core; the rest of the repo (optimizer, Monte
Carlo mode, significance testing, ~176 indicator functions, web UI/API layer,
ML feature export) was skimmed at the directory level only and not read
line-by-line — skipped: `jesse/indicators/` (176 files, standard TA
indicators, no novel mechanism), `jesse/modes/optimize_mode/`,
`jesse/modes/monte_carlo_mode/`, `jesse/modes/significance_test_mode/`,
`jesse/controllers/`, `jesse/static/`, `jesse/mcp/`.
