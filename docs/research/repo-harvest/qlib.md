# microsoft/qlib — read 2026-08-20

MIT licence. Python (+ a Cython/C++ high-perf index-data module). Microsoft's
AI-oriented quant investment platform: data layer, model zoo, nested
strategy/execution simulation, RL order execution, and online (live) serving.
Commit read: `79633dd9506ea689e5400dea0197717b5b3d74b7`.

Scope actually read: `qlib/rl/` (order_execution, trainer, interpreter),
`qlib/backtest/` (exchange, decision, account, position, executor),
`qlib/strategy/base.py`, `qlib/workflow/online/` (manager, strategy, update),
and `qlib/contrib/strategy/` (rule_strategy.py, cost_control.py,
signal_strategy.py) for concrete reference strategies. Everything else
skipped — see "Not useful here".

## Mechanisms worth stealing (ranked)

1. **Almgren-Chriss volume scheduling for order splitting.** `ACStrategy`
   computes `kappa_tild = lamb/eta * sigma^2`, `kappa = arccosh(kappa_tild/2 + 1)`,
   then allocates each remaining trading step a fraction
   `(sinh(kappa*(N-t)) - sinh(kappa*(N-t-1))) / sinh(kappa*N)` of the
   remaining order — more front-loaded as volatility (`sigma`, a rolling
   stdev-of-log-returns signal) rises, degrading gracefully to TWAP (equal
   split) when no volatility signal exists.
   `qlib/contrib/strategy/rule_strategy.py:ACStrategy.generate_trade_decision`
   (lines ~461-536), volatility formula in `_reset_signal` (~423-438).
   Directly reusable for splitting a sized crypto order across the next N
   ticks/minutes by realised vol instead of naive TWAP.
2. **Quadratic market-impact cost model, scaled by participation.**
   `adj_cost_ratio = impact_cost * (trade_val / total_trade_val) ** 2` — cost
   grows with the square of how much of that bar's total volume the order
   consumes, added on top of flat open/close cost rates (defaults
   `open_cost=0.0015`, `close_cost=0.0025`, `min_cost=5.0`).
   `qlib/backtest/exchange.py:Exchange._calc_trade_info_by_order` (843-952).
   Directly maps to `execution-cost-model` — a fill filled at 30% of the
   candle's volume should cost more (bp) than one filled at 3%.
3. **Cash/position-aware order clipping before fill.** A buy order is
   clipped to what cash can afford (`_get_buy_amount_by_cash_limit`: solves
   for `max_trade_amount` given `cost_ratio` and `min_cost`); a sell order is
   clipped to actual held amount (never oversell) and voided if the residual
   cash after the trade would be below `min_cost`.
   `qlib/backtest/exchange.py:Exchange._calc_trade_info_by_order`,
   `_get_buy_amount_by_cash_limit` (834-857). This is the honesty check a
   `paper-fill-simulator` needs: a paper fill must be refused/clipped the
   same way a real venue would reject for insufficient balance.
4. **Volume-capacity clipping simulates partial fills / order rejection.**
   `_clip_amount_by_volume` caps `order.deal_amount` at a configurable
   fraction of that bar's traded volume (`vol_threshold`), either cumulative
   or per-tick — an order larger than the market can absorb is silently
   partial-filled, exactly like a real illiquid book.
   `qlib/backtest/exchange.py:Exchange._clip_amount_by_volume` (786-832).
5. **Nested nested time-scale executor with nesting-aware trade ranges.**
   `NestedExecutor` lets an outer strategy issue a decision (e.g. "sell X by
   end of day") and an inner strategy decide sub-step execution (e.g. per
   30-min bar), each level with its own `TradeCalendarManager` and
   `TradeRange` clipping (`TradeRangeByTime`, `IdxTradeRange`).
   `qlib/backtest/executor.py:NestedExecutor` (310-500),
   `qlib/backtest/decision.py:TradeRangeByTime` (264-299). Useful pattern for
   separating "which trade to open" (ai-brain/opinion-arbiter cadence) from
   "how to work the order into the book" (execution-venue-adapter cadence)
   without collapsing them into one control loop.
6. **Price-advantage (PA) reward in basis points against a TWAP baseline.**
   `price_advantage(exec_price, baseline_price, direction)` returns
   `(1 - exec/base)*10000` for buys / `(exec/base - 1)*10000` for sells — a
   scale-free, direction-aware execution-quality metric. Used both as an RL
   reward (`PAPenaltyReward`, which also penalises `sum((vol_i/total)^2)` —
   quadratic penalty for stacking volume in one tick) and as a reported
   metric (`SAOEMetrics.pa`).
   `qlib/rl/order_execution/strategy.py:price_advantage` (342-362),
   `qlib/rl/order_execution/reward.py:PAPenaltyReward.reward` (17-50). This
   is a ready-made formula for `slippage-learner`'s per-fill scoring, and for
   scoring the resource governor/execution split quality itself.
7. **RL-vs-VWAP fallback reward with a hard threshold.** `PPOReward` only
   pays out at the terminal step: `ratio = vwap_realised/twap_baseline` (or
   inverse for sells); returns `-1` if `ratio < 1.0` (worse than doing
   nothing clever), `0` if `1.0<=ratio<1.1`, `+1` above. A blunt but
   effective anti-overfitting reward shape — reward regions rather than raw
   continuous PnL — worth considering for `decision-quality-critic` so a bot
   is not rewarded for barely-better-than-noise execution.
   `qlib/rl/order_execution/reward.py:PPOReward.reward` (53-99).
8. **Explicit `AccumulatedInfo` separates cost / turnover / return as three
   named running totals, not folded into one PnL number.**
   `qlib/backtest/account.py:AccumulatedInfo` (35-67) and
   `Account.update_portfolio_metrics` (250-292). Matches the blueprint's
   push toward `expectancy-breakdown` — decompose expected value into
   separately measurable factors rather than reporting only the total.
9. **Online model rolling as an explicit routine, decoupled from
   backtest-vs-live via a `Trainer` abstraction and a `simulate()` mode.**
   `OnlineManager.routine` re-trains, re-scores, and swaps which model is
   "online" on a schedule; the doc string explicitly lays out 4
   online/simulation x trainer/delay-trainer combinations so the exact same
   strategy code runs in backtest and live.
   `qlib/workflow/online/manager.py:OnlineManager` (top-of-file docstring +
   class, lines 1-160+), `qlib/workflow/online/strategy.py:OnlineStrategy`
   (18-90). This is the shape `kronos-finetuner` / `model-drift-monitor`
   need for retraining cadence without special-casing "are we live or
   replaying".
10. **Prediction/label updater does a surgical date-range splice, not a full
    recompute.** `_replace_range` drops only the overlapping date span from
    old data and concatenates fresh predictions, deduping on index.
    `qlib/workflow/online/update.py:_replace_range` (261-267),
    `PredUpdater.get_update_data` (275-281). Cheap incremental-update
    pattern worth copying for any per-symbol rolling cache
    (`symbol-profile-store`, `forecast-accuracy`).
11. **Proportional-budget rebalancer with impact caps per trade.**
    `SoftTopkStrategy.generate_target_weight_position` computes ideal
    per-stock weight (`risk_degree/topk`), sells excess/dropped names first
    (capped by `trade_impact_limit`), then allocates the freed cash + risk
    headroom proportionally to shortfalls, each still capped by the same
    impact limit. `qlib/contrib/strategy/cost_control.py:SoftTopkStrategy`
    (8-118). A concrete "never move more than X% of book" sizing pattern
    that composes with `position-sizer`.

## Map onto existing parts

| existing part id | repo file (reference impl) | what the repo does that the part description doesn't yet say |
|---|---|---|
| `execution-cost-model` | `qlib/backtest/exchange.py:Exchange._calc_trade_info_by_order` | Cost is not one flat rate: base open/close rate PLUS an impact term that scales with `(order_value / bar_total_value)^2`, and the order itself is clipped by cash and by volume capacity before a cost is even computed — cost, fill-size, and rejection are one calculation, not three. |
| `position-sizer` | `qlib/backtest/exchange.py:Exchange._get_buy_amount_by_cash_limit` | Gives the exact closed-form for "largest affordable size given price, cash, and a proportional+minimum cost", including the min-cost floor case — a concrete algorithm the part description leaves as "size ... or refuse". |
| `slippage-learner` / `slippage-profile` | `qlib/rl/order_execution/strategy.py:price_advantage` | Supplies a ready formula (signed, bp-scaled, TWAP-baselined) for what "measured difference between intended and filled price" should actually compute, rather than leaving the metric undefined. |

`instruction-replayer`, `lookahead-auditor`, `walk-forward-splitter`,
`backtest-scorer` and `ai-brain/opinion-arbiter` have no genuine reference
implementation here: qlib's backtest loop is day/bar-driven equities
rebalancing with T+1 settlement assumptions baked in (see Not useful here),
not a walk-forward/no-lookahead harness, and it has no opinion-arbitration
concept — `TopkDropoutStrategy` and friends compute one signal-to-target-
position mapping directly, with no competing bull/bear opinions to weigh.

## New part proposals

- **`participation-capped-order-splitter`** — block: `risk-capital-allocation`.
  consumes: `sized-order`, `volatility-forecast`, `order-book-snapshot`.
  produces: NEW `execution-schedule` (a sequence of `(time, amount)` slices
  for one sized order). Splits a sized order into per-tick slices using the
  Almgren-Chriss volume curve (front-load more when forecast vol is high,
  degrade to equal split otherwise), each slice capped by a participation
  ceiling against recent traded volume. Evidence:
  `qlib/contrib/strategy/rule_strategy.py:ACStrategy.generate_trade_decision`,
  `qlib/backtest/exchange.py:Exchange._clip_amount_by_volume`.
- **`fill-price-advantage-scorer`** — block: `learning-loop`. consumes:
  `fill`, `market-data`. produces: `slippage-profile`. Scores every fill
  against a TWAP baseline over the fill's own execution window, signed by
  direction, in basis points — the same computation `slippage-learner`
  needs but with a concrete, direction-correct formula instead of a raw
  price difference. Evidence: `qlib/rl/order_execution/strategy.py:price_advantage`.
- **`expectancy-component-ledger`** — block: `ledger`. consumes:
  `closed-trade`, `fill`. produces: NEW `expectancy-component-record` (cost,
  turnover, and return kept as three separate running totals per segment,
  not one PnL scalar). Mirrors `AccumulatedInfo`'s separation so
  `expectancy-breakdown` has raw, already-decomposed inputs to work from
  instead of decomposing one PnL number after the fact. Evidence:
  `qlib/backtest/account.py:AccumulatedInfo`.

## Anti-patterns seen

- **God-object `Exchange`**: one class owns quote data loading, limit-up/down
  logic, volume-limit parsing, cost calculation, cash clipping, AND order
  generation from target weights (`generate_order_for_target_amount_position`)
  — a T-6 violation (many responsibilities welded into one class) that this
  project's per-responsibility parts must not copy piecewise; extract only
  the individual formulas, not the class shape.
- **Strategy classes reach directly into `Exchange` and `Account` internals**
  (`self.trade_exchange.get_amount_of_trade_unit(...)`,
  `position.get_cash()`) rather than being handed data — the opposite of
  T-4's "a part names data types, never other parts."
- **Hidden mutable state via dataclass field defaults**: `Order.deal_amount`
  and `factor` are declared with defaults in the dataclass, then forcibly
  reset to `0.0`/`None` in `__post_init__` regardless of what was passed
  (`qlib/backtest/decision.py:Order.__post_init__`) — a constructor that
  silently discards caller input is a correctness trap, not a pattern to
  reuse.
- **Global mutable config singleton** (`from ..config import C`, referenced
  throughout `qlib/backtest/exchange.py` for `C.trade_unit`, `C.limit_threshold`,
  `C.region`) — implicit global state a part reads without it being in its
  declared `consumes:`, which this project's contract-checked parts forbid.

## Not useful here

Skipped entirely: `qlib/data/` (the whole point-in-time data-provider and
expression-engine layer — Chinese/US equities daily+minute bar storage
format, irrelevant to a ccxt-fed crypto feed), `qlib/contrib/model/` (the
model zoo — LightGBM/LSTM/GRU signal models tuned for daily-frequency
factor investing, not this project's Kronos-based forecaster), `docs/`,
`examples/`, `tests/`. Also skimmed but not cited: `qlib/rl/trainer/`,
`qlib/rl/data/` (RL training-loop plumbing, not a mechanism). Much of what
remains only partly transfers: `Exchange`'s limit-up/limit-down and
T+1-settlement logic (`trade_unit`, `limit_threshold` tied to `REG_CN`) is
CN-equities-market-structure-specific and does not apply to a 24/7 crypto
venue with no daily limits; the default cost rates (`open_cost=0.0015`,
`close_cost=0.0025`) are equities-commission-scale, not crypto
maker/taker-fee scale, and must be re-derived from the actual venue's fee
schedule rather than copied as constants. The whole nested-executor loop
assumes bar-aligned days (`trade_calendar`, `get_step_time` keyed to a
trading calendar with open/close) rather than a continuous 24/7 market —
useful as a pattern for time-scale separation, not as code to run as-is.
