# AI4Finance-Foundation/FinRL — read 2026-08-20

MIT. Python (gymnasium/stable-baselines3/elegantrl/rllib, pandas/numpy,
stockstats, ccxt, alpaca-trade-api). A library of Gym-style trading
environments plus RL-agent wrappers for training deep-RL trading policies on
historical data (equities primarily, some crypto/portfolio-allocation
variants), with a thin Alpaca paper-trading loop bolted on. The repo's own
README states it now points production/live work at a separate successor
(`FinRL-X` / `FinRL-Trading`); this repo is "the original end-to-end
educational and research framework." Clone commit:
`2334a5fe6d30629157f13c3b0319e1637e15e123`.

Skipped: `finrl/agents/elegantrl`, `finrl/agents/rllib`,
`finrl/agents/stablebaselines3` (thin wrappers around external RL libraries —
algorithm choice, not domain mechanism), `finrl/agents/portfolio_optimization`
(a specific RL architecture/EIIE model, not a trading mechanism), `docs/`,
`docker/`, `.github/`, `examples/` notebooks (thin callers of the code already
read), `finrl/meta/data_processors/processor_{eodhd,joinquant,quantconnect,
sinopac,wrds,yahoofinance}.py` (vendor-specific data downloaders, same shape
as `finrl/meta/data_processors/processor_ccxt.py` already read), `unit_tests/`
(read the list only, not
the bodies — confirms only cash-penalty env and two downloaders have any
test).

## Mechanisms worth stealing (ranked)

1. **Turbulence index as a systemic-risk kill switch.**
   `FeatureEngineer.calculate_turbulence`
   (`finrl/meta/preprocessor/preprocessors.py:270-334`) computes, per date,
   `current_temp.dot(pinv(cov_temp)).dot(current_temp.T)` — a
   Mahalanobis-distance-style statistic of today's cross-asset return vector
   against a trailing 252-day rolling covariance matrix — starting only after
   252 days of history and discarding the first 2 nonzero readings as
   warm-up outliers. `StockTradingEnv._sell_stock` /
   `_buy_stock` (`finrl/meta/env_stock_trading/env_stocktrading.py:149-174,
   215-221`) then force-liquidates every position (or blocks all new buys)
   the instant `self.turbulence >= self.turbulence_threshold`, overriding
   whatever the RL policy's action said. Directly the shape of a systemic
   `event-risk-limiter` / `trading-halt-decider` gate: one scalar, one
   threshold, unconditional override.
2. **Turbulence gate reused live, unchanged, at inference time.**
   `AlpacaPaperTrading.trade`
   (`finrl/meta/env_stock_trading/env_stock_papertrading.py:221-296`) recomputes
   the same turbulence statistic against live Alpaca data each tick
   (`AlpacaPaperTrading.get_state`, lines 297-338) and if
   `turbulence_bool == 1`, calls `self.alpaca.list_positions()` and closes
   every open position regardless of the model's output — the identical gate
   is evaluated in train, backtest and live paths from the same formula, not
   re-implemented three times.
3. **Cost-basis-driven forced stop-loss with three-part reward shaping.**
   `StockTradingEnvStopLoss.get_reward` and `.step`
   (`finrl/meta/env_stock_trading/env_stocktrading_stoploss.py:260-447`)
   tracks a running `avg_buy_price` per asset via an incremental mean
   (`avg_buy_price += (closing - avg_buy_price) / n_buys`, line 427), derives
   `closing_diff_avg_buy = closings - stoploss_penalty * avg_buy_price`, and
   force-sells any holding where that goes negative once
   `cash >= stoploss_penalty * initial_amount` (lines 355-365). The reward is
   `((total_assets - total_penalty + additional_reward) / initial_amount - 1)
   / current_step`, where `total_penalty = cash_penalty + stop_loss_penalty +
   low_profit_penalty` (lines 260-295) — a cash-reserve penalty
   (`max(0, total_assets*cash_penalty_proportion - cash)`), a penalty for
   holding through a stop-loss level, and a penalty for selling below
   `profit_loss_ratio`-implied minimum profit. Concrete reference for
   `profit-lock` / `stop-target-placer`: the stop is measured off actual
   average cost basis, not off entry price alone.
4. **Iterative fixed-point commission model for simultaneous portfolio
   turnover ("trf").** `PortfolioOptimizationEnv._get_state_and_info_from_time_index`
   step logic (`finrl/meta/env_portfolio_optimization/env_portfolio_optimization.py:321-333`)
   solves `mu = (1 - fee*w0 - (2*fee - fee**2) * sum(max(last_w[1:] - mu*w[1:], 0))) /
   (1 - fee*w0)` by fixed-point iteration to `1e-10` convergence, then scales
   the whole portfolio value by `mu` in one shot — a single multiplicative
   discount capturing the simultaneous-rebalance cost across every asset at
   once, instead of costing each trade independently. A second, simpler model
   ("wvm", lines 307-320) computes `fees = sum(abs(delta_weights[1:] *
   portfolio_value))` and rejects the rebalance outright if fees would exceed
   the cash slice. Both are concrete alternatives for `execution-cost-model`
   beyond a flat per-trade percentage.
5. **Flat per-trade percentage cost with an explicit "can't afford it" clamp.**
   `StockTradingEnv._buy_stock._do_buy`
   (`finrl/meta/env_stock_trading/env_stocktrading.py:182-212`) computes
   `available_amount = cash // (price * (1 + buy_cost_pct))` before sizing,
   so the cost is baked into the affordability check itself rather than
   applied after and risking negative cash — `buy_num_shares =
   min(available_amount, action)`.
6. **Turbulence and technical-feature normalization by fixed power-of-two
   scale, shared verbatim between train and live state vectors.**
   `AlpacaPaperTrading.get_state` (`finrl/meta/env_stock_trading/
   env_stock_papertrading.py:297-338`) builds the live observation as
   `hstack(cash*2**-12, sigmoid_sign(turbulence, thresh)*2**-5,
   turbulence_bool, price*2**-6, stocks*2**-6, stocks_cd, tech*2**-7)`, using
   `sigmoid_sign` (lines 377-382: `sigmoid(x*e) - 0.5) * thresh`) to squash
   an unbounded turbulence statistic into a bounded, sign-preserving range
   before it enters the policy's input. This is the repo's only real
   train/live parity mechanism: the same literal scale constants have to be
   copied into the live class by hand (see anti-pattern below), but the
   *idea* — squash an unbounded risk statistic through a fixed sigmoid before
   it reaches a policy or bot input — is reusable for feeding
   `market-anomaly` or turbulence-like signals into `bull-bot`/`bear-bot`
   without them seeing raw unbounded magnitudes.
7. **Reward as scaled realized asset-value delta with a terminal
   gamma-discounted tail.** `CryptoEnv.step` and `BitcoinEnv.step`
   (`finrl/meta/env_cryptocurrency_trading/env_multiple_crypto.py:65-101`,
   `finrl/meta/env_cryptocurrency_trading/env_btc_ccxt.py:86-134`) both use
   `reward = (next_total_asset - total_asset) * 2**-16` every step, and
   accumulate `gamma_return = gamma_return * gamma + reward`, added back only
   on the terminal step. Simple, auditable reward shaping: no lookahead, no
   Sharpe-style non-Markovian term inside the per-step reward — the
   difficult part (risk-adjusted scoring) is left entirely to whatever reads
   `trade-episode`/`decision-quality-score` downstream, not smuggled into the
   step reward.
8. **`data_split` enforces a hard date-range cut with per-date reindexing.**
   `finrl/meta/preprocessor/preprocessors.py:26-35` filters
   `df[(df[date_col] >= start) & (df[date_col] < end)]`, then sets
   `data.index = data[date_col].factorize()[0]` — collapsing the index to a
   dense 0..N integer sequence keyed on date. Every env's `self.day`/`.time`
   walk assumes this contiguous integer index; a clean, minimal
   `walk-forward-splitter` reference for turning a date range into an
   index a replay can step through without gaps.

## Map onto existing parts

| existing part id | repo file (reference impl) | what the repo does that our part description does not yet say |
|---|---|---|
| `event-risk-limiter` | `finrl/meta/preprocessor/preprocessors.py:FeatureEngineer.calculate_turbulence`, `finrl/meta/env_stock_trading/env_stocktrading.py:StockTradingEnv._sell_stock/_buy_stock` | A concrete systemic-risk statistic (Mahalanobis distance of returns vs. trailing 1-year covariance) with a hard threshold that unconditionally overrides the policy's action — not scoped to one named event, but to "the whole market is behaving abnormally right now." |
| `trading-halt-decider` | `finrl/meta/env_stock_trading/env_stock_papertrading.py:AlpacaPaperTrading.trade` (turbulence branch, lines 280-295) | Shows the same turbulence gate evaluated identically at live-inference time, closing every open position via the venue API rather than just refusing new entries — our part description doesn't yet specify that a halt should force-flatten existing positions, not only block new ones. |
| `execution-cost-model` | `finrl/meta/env_portfolio_optimization/env_portfolio_optimization.py:PortfolioOptimizationEnv.step` (trf/wvm branches, lines 307-333) | Two turnover-aware commission models — an iterative fixed-point solve ("trf") that prices simultaneous multi-asset rebalancing as one multiplicative factor, and a hard reject-if-unaffordable model ("wvm") — beyond a flat per-trade percentage. |
| `profit-lock` / `stop-target-placer` | `finrl/meta/env_stock_trading/env_stocktrading_stoploss.py:StockTradingEnvStopLoss.step/get_reward` | Stop level computed off a maintained per-asset average cost basis (incremental mean of actual buy fills), not off the original entry price, plus a separate reward penalty for selling below a configured minimum profit/loss ratio. |
| `position-sizer` | `finrl/meta/env_stock_trading/env_stocktrading.py:StockTradingEnv._buy_stock._do_buy` | Affordability check folds the transaction cost into the size computation itself (`cash // (price * (1+cost_pct))`) before clamping to the requested size, so a sized order can never be short of cash for its own fees. |

## New part proposals

- **`turbulence-index-gauge`** — block: `intelligence`. consumes:
  `market-data`. produces: NEW `turbulence-index` (a single systemic
  cross-symbol statistic: today's return vector distance from its trailing
  covariance, continuous, unbounded, updated once per bar). Responsibility:
  compute one market-wide statistical-distance number the rest of the system
  can threshold against, distinct from `regime-classifier`'s
  trending/mean-reverting/mixed read — this is "is everything moving together
  abnormally" rather than "which way is one symbol moving." Evidence:
  `finrl/meta/preprocessor/preprocessors.py:270-334`
  (`FeatureEngineer.calculate_turbulence`).
- **`turnover-cost-solver`** — block: `backtesting`. consumes: `sized-order`,
  `market-data`. produces: `cost-estimate`. Responsibility: when several
  positions rebalance at once (a segment-wide re-weighting rather than one
  order), solve the single multiplicative factor that prices the whole
  turnover in one pass instead of costing each order independently — `
  execution-cost-model` as declared prices one fill at a time and has no
  turnover-aware sibling. Evidence:
  `finrl/meta/env_portfolio_optimization/env_portfolio_optimization.py:321-333`.
- **`cost-basis-tracker`** — block: `portfolio-state`. consumes: `fill`.
  produces: NEW `cost-basis` (per-symbol running average buy price, updated
  incrementally on every buy fill, zeroed when the position closes).
  Responsibility: maintain the average price a position was actually built
  at, so stop and profit-quality decisions measure against real cost rather
  than only the most recent fill — `peak-excursion-tracker` tracks unrealized
  high/low points but not the cost basis those points are measured against.
  Evidence: `finrl/meta/env_stock_trading/env_stocktrading_stoploss.py:420-433`
  (`self.avg_buy_price` incremental update).

## Anti-patterns seen

- **`AlpacaPaperTrading` is a god object mixing every plane at once.**
  `finrl/meta/env_stock_trading/env_stock_papertrading.py:AlpacaPaperTrading`
  loads the trained model (`__init__`, lines 35-98), connects to the broker,
  fetches live state (`get_state`), computes the turbulence risk decision,
  and submits orders (`trade`, `submitOrder`) all as methods on one class —
  data fetch, forecast/policy inference, risk gating, and order execution
  are not separable parts, they are one 400-line object. We take the
  turbulence-gate formula, not this structure.
- **The execution/fill simulator makes its own risk-switching decision
  instead of receiving one.** `StockTradingEnv._sell_stock` / `_buy_stock`
  (`finrl/meta/env_stock_trading/env_stocktrading.py:149-174, 215-221`) branch
  directly on `self.turbulence_threshold` and `self.turbulence` to decide
  whether to override the actor's action — the part responsible for
  simulating fills is also the part deciding whether trading is currently
  allowed. Under T-2 that decision belongs on the control plane (something
  upstream produces a `risk-limit`/`trading-halt` the execution part merely
  obeys), not computed inside the fill logic itself.
- **Whole historical dataset held resident in the part for its entire
  lifetime, with no release path.** `CryptoEnv.__init__` and
  `BitcoinEnv.load_data` (`finrl/meta/env_cryptocurrency_trading/
  env_multiple_crypto.py:22-27`, `finrl/meta/env_cryptocurrency_trading/
  env_btc_ccxt.py:181-221`) load the full `price_array`/`tech_array` into
  instance attributes at construction and never release them; there is no
  "off" state for these objects at all, just process exit. If treated as an
  always-on part in a governed system this is a direct T-3 violation — off
  is not modeled, so there is nothing for a resource governor to switch.

## Not useful here

The repo's equity-market defaults do not fit an intraday crypto bot: turbulence
and technical indicators assume daily bars with a 252-trading-day annual
warm-up (`calculate_turbulence`'s `start = 252`), the Alpaca paper-trading loop
(`finrl/meta/env_stock_trading/env_stock_papertrading.py:155-220`) explicitly
waits for market open/close and sleeps until a session begins, which has no
meaning in a 24/7 crypto market, and
`finrl/meta/data_processors/processor_ccxt.py:CCXTEngineer.data_fetch` only pulls historical
OHLCV in batches via REST (`ccxt.binance().fetch_ohlcv`) — there is no live
streaming/websocket reader, no order-book depth, no options surface, no
funding-rate or liquidation data anywhere in the repo, and no crypto examples
under `examples/`. The RL training loop itself
(`finrl/meta/paper_trading/common.py:Config`, `AgentBase`, `train_agent`) is a
single-process academic training harness (fixed `horizon_len` rollout,
in-memory buffer, no checkpoint/resume beyond a raw `actor.pth` file) built
for offline experimentation, not a live control loop. Consistent with this,
the repo's own README now redirects live/production deployment work to a
separate successor project (`FinRL-X`/`FinRL-Trading`), and only two of the
seven `finrl/meta/env_*` modules have any unit-test coverage at all
(`unit_tests/environments/test_cash_penalty.py`).
