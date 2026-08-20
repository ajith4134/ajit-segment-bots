# freqtrade/freqtrade — read 2026-08-20

GPLv3. Python (pandas/numpy/sqlalchemy/ccxt, pluggable ML for FreqAI). Free
crypto algo-trading bot: live/dry-run execution, backtesting, hyperopt, and
FreqAI (ML price/classification prediction layer). Clone commit:
`f1e0e5844c85b71b43f60be2fbe50d449c471a6`.

## Mechanisms worth stealing (ranked)

1. **Unlimited-stake position sizing from tied-up + free capital**
   `Wallets.get_available_stake_amount` computes
   `min(total_stake_amount - tied_up_stakes, free_balance)`, and
   `Wallets._calculate_unlimited_stake_amount` (freqtrade/wallets.py:338,349)
   divides `(available + tied_up) / max_open_trades`, capped at available. This
   is a clean formula for "divide capital evenly across open-trade slots
   without starving itself as trades open" — directly reusable by
   `position-sizer`.
2. **available_capital vs. wallet-inferred starting balance**
   `Wallets.get_starting_balance` (freqtrade/wallets.py:301) either reads a
   configured `available_capital`, or derives it as
   `(free_balance - total_closed_profit + open_stakes) * tradable_balance_ratio`
   — i.e. it backs out the starting balance from current state so a partially
   externally-funded account doesn't miscount profit as capital.
3. **Backoff-based DDoS/temporary-error retry with exception taxonomy**
   `freqtrade/exceptions.py` defines a strict hierarchy
   (`ExchangeError → TemporaryError → DDosProtection`,
   `ExchangeError → InvalidOrderException → RetryableOrderError` /
   `InsufficientFundsError`). `calculate_backoff` (freqtrade/exchange/common.py:114)
   is `(max_retries - remaining_retries)**2 + 1` seconds; `retrier` /
   `retrier_async` (freqtrade/exchange/common.py:124,177) recurse with that
   backoff only on `TemporaryError`/`RetryableOrderError`, and give up and
   re-raise otherwise. This is a ready template for `order-reject-classifier`
   + `order-resubmitter`: classify first, then only retry the classes that are
   actually transient.
4. **Max-drawdown circuit breaker over a rolling trade window**
   `MaxDrawdown.global_stop` → `_max_drawdown`
   (freqtrade/plugins/protections/max_drawdown_protection.py:46) looks back
   `lookback_period` minutes, requires at least `trade_limit` closed trades in
   that window, computes relative drawdown via
   `calculate_max_drawdown(...).relative_account_drawdown` (equity mode) or a
   ratio-based mode, and locks trading with a `ProtectionReturn(lock=True,
   until=..., reason=...)` if drawdown exceeds `max_allowed_drawdown`. Directly
   the shape of `drawdown-breaker`.
5. **Stoploss-frequency guard (repeated-stop circuit breaker)**
   `StoplossGuard._stoploss_guard` (freqtrade/plugins/protections/stoploss_guard.py:44)
   counts stop-loss/trailing-stop/liquidation exits in the lookback window with
   `close_profit < required_profit`; if the count reaches `trade_limit`
   (default 10), it locks trading — optionally per-pair, per-side. Distinct
   failure mode from raw drawdown: catches "many small stops in a row" before
   the equity curve shows it.
6. **Spread and volume liquidity filters as composable pairlist stages**
   `SpreadFilter._validate_pair` (freqtrade/plugins/pairlist/SpreadFilter.py:63)
   rejects a pair when `1 - bid/ask > max_spread_ratio` (default 0.5%).
   `VolumePairList.filter_pairlist` (freqtrade/plugins/pairlist/VolumePairList.py:174)
   computes rolling quote volume over `lookback_period` candles of
   `lookback_timeframe` and drops pairs under `min_value`. Together this is a
   concrete `liquidity-grader` reference: spread threshold + rolling-volume
   floor, refreshed on a TTL cache (`refresh_period`, default 1800s), not every
   tick.
7. **Anti-lookahead backtest signal shifting**
   `Backtesting._get_ohlcv_as_lists` (freqtrade/optimize/backtesting.py:550-567)
   shifts every entry/exit signal column by one candle (`.shift(1)`) and drops
   the first row after shifting, so a signal computed on candle N can only act
   on candle N+1 — the same candle it would actually have been visible on
   live. This is exactly the job `lookahead-auditor` needs to enforce, not just
   check after the fact.
8. **Monotonic stop-loss ratchet with leverage and short-side handling**
   `Trade.adjust_stop_loss` (freqtrade/persistence/trade_model.py:839) computes
   `current_price * (1 ± abs(stoploss/leverage))` (sign flips for shorts), then
   only accepts the new stop if it is higher (long) or lower (short) than the
   existing one — "stop losses only walk up, never down" — unless
   `allow_refresh` is explicitly set. Precision-rounds toward the safe side
   (`ROUND_UP`/`ROUND_DOWN` by direction). Reference for `profit-lock`.
9. **FreqAI dissimilarity-index (DI) and outlier-rejection pipeline**
   `IFreqaiModel.define_data_pipeline` (freqtrade/freqai/freqai_interface.py:558)
   builds an sklearn-style `Pipeline` from config flags: variance threshold →
   min-max scaler → optional PCA (`n_components=0.999`) → optional SVM outlier
   extractor (`nu=0.01` default) → optional `DissimilarityIndex(di_threshold=...)`
   → optional DBSCAN → optional Gaussian noise injection. At predict time
   (freqtrade/freqai/base_models/BaseRegressionModel.py:114-129) each new
   feature row is run back through the fitted pipeline; `dk.DI_values` holds
   the per-row DI (distance from the training-set feature space) and
   `dk.do_predict` is a boolean mask marking predictions that fall outside the
   trained space. This is a real, working template for "know when the model is
   extrapolating" — directly relevant to `kronos-forecaster` / `model-drift-monitor`
   trust-gating.
10. **Retrain trigger runs on a separate thread, decoupled from prediction**
    `IFreqaiModel` starts a background thread
    (freqtrade/freqai/freqai_interface.py:222 area, "designed to constantly
    scan pairs for retraining on a separate thread (intracandle)") that checks
    `train_period_days` / `live_retrain_hours` sliding windows and sets
    `self.retrain` without blocking the candle-by-candle prediction path — the
    prediction consumer never waits on a training run.

## Map onto existing parts

| existing part id | repo file (reference impl) | what the repo does that our description doesn't yet say |
|---|---|---|
| `position-sizer` | `freqtrade/wallets.py:Wallets.get_trade_stake_amount`, `_calculate_unlimited_stake_amount`, `_check_available_stake_amount` | Concrete formula for splitting available capital across N open-trade slots without re-reading a stale balance, plus an explicit "amend last stake" fallback when remaining capital is too thin to fill one more slot. |
| `drawdown-breaker` | `freqtrade/plugins/protections/max_drawdown_protection.py:MaxDrawdown._max_drawdown` | Two selectable drawdown calculation modes (equity-based vs. legacy cumulative-ratio) and a minimum-trade-count gate before drawdown is even evaluated, so a single early loss can't trip the breaker. |
| `halt-enforcer` | `freqtrade/plugins/protections/stoploss_guard.py:StoplossGuard._stoploss_guard` | A distinct, count-of-recent-stops trigger (not equity-based), with per-pair vs. global and per-side scoping — a second, independent brake alongside drawdown. |
| `liquidity-grader` | `freqtrade/plugins/pairlist/SpreadFilter.py:SpreadFilter._validate_pair`, `freqtrade/plugins/pairlist/VolumePairList.py:VolumePairList.filter_pairlist` | Concrete spread-ratio and rolling-quote-volume thresholds, refreshed on a TTL cache rather than every tick — a cost control our part description doesn't mention. |
| `order-reject-classifier` / `order-resubmitter` | `freqtrade/exceptions.py`, `freqtrade/exchange/common.py:retrier`, `calculate_backoff` | A ready exception taxonomy (temporary/DDoS/insufficient-funds/retryable-not-found) and a quadratic backoff formula, with retry only for the classes proven transient. |
| `profit-lock` | `freqtrade/persistence/trade_model.py:Trade.adjust_stop_loss` | The monotonic-ratchet invariant ("only walk up, never down") plus leverage-adjusted stop distance and short-side sign flip, precision-rounded toward the conservative side. |
| `lookahead-auditor` | `freqtrade/optimize/backtesting.py:Backtesting._get_ohlcv_as_lists` | Enforces no-lookahead structurally by shifting signal columns one candle forward before the replay ever sees them, rather than auditing after the fact. |
| `model-drift-monitor` | `freqtrade/freqai/freqai_interface.py:IFreqaiModel.define_data_pipeline`, `base_models/BaseRegressionModel.py` predict path | A per-prediction dissimilarity-index score plus a boolean "outside training space" mask, computed at inference time — a concrete signal for "trust this forecast or not" beyond a single accuracy-decay threshold. |

## New part proposals

- **`liquidity-cache-refresher`** — block: `market-data-feed`. consumes:
  `order-book-snapshot`, `market-data`. produces: NEW `liquidity-grade-cache`
  (a liquidity-grade set refreshed on a TTL rather than every tick).
  Responsibility: recompute spread/volume liquidity grades for the whole
  universe on a fixed interval instead of per-tick, so `liquidity-grader`
  reads a cache instead of recomputing per symbol per tick. Evidence:
  `freqtrade/plugins/pairlist/VolumePairList.py:44` (`FtTTLCache`).
- **`prediction-trust-gate`** — block: `prediction`. consumes: `price-forecast`,
  NEW `feature-distance-score`. produces: NEW `forecast-out-of-distribution-flag`.
  Responsibility: flag a forecast as extrapolating outside the training
  feature space (dissimilarity-index style) so the arbiter can discount it
  independent of the forecaster's historical accuracy. Evidence:
  `freqtrade/freqai/freqai_interface.py:558-586` (DI/SVM/DBSCAN pipeline
  steps), `freqtrade/freqai/base_models/BaseRegressionModel.py:114-129`
  (`dk.DI_values`, `dk.do_predict`).
- **`stop-frequency-breaker`** — block: `risk-capital-allocation`. consumes:
  `closed-trade`. produces: `risk-limit`. Responsibility: cut the risk limit
  when the count of stop-loss exits in a rolling window crosses a threshold —
  a signal distinct from `drawdown-breaker` because a string of small stops
  can precede visible equity drawdown. Evidence:
  `freqtrade/plugins/protections/stoploss_guard.py:44-87`.

## Anti-patterns seen

- **`FreqtradeBot` is a god object.** `freqtrade/freqtradebot.py` is 2671
  lines, 59 methods on one class, covering entry, exit, stake sizing calls,
  exchange calls, protection checks, RPC notification, and persistence in one
  place — the opposite of T-1/T-6 (one responsibility per part, grow by
  adding parts). We take the formulas cited above, not this structure.
- **Protections read global mutable trade state directly.** `IProtection`
  subclasses call `Trade.get_trades_proxy(...)` straight from the ORM
  (`freqtrade/plugins/protections/stoploss_guard.py:52`,
  `max_drawdown_protection.py:52`) rather than being handed a data type — a
  part reaching into shared persistence instead of consuming a declared input
  violates T-4 (a part should name data, not reach into the circuit's guts).
- **FreqAI's retrain thread mutates the model in place from a background
  thread** (`freqtrade/freqai/freqai_interface.py`, intracandle retrain
  scanning) while prediction reads it concurrently — an implicit shared-state
  handoff rather than an explicit `finetuned-model` data type crossing a
  boundary; T-5 wants explicit states, not a background thread quietly
  swapping state underneath a consumer.

## Not useful here

FreqAI's deep-learning/backtesting infrastructure (`freqtrade/freqai/RL/`,
`torch/`, `tensorboard/`) targets research workflows with hours-long train
loops and disk-persisted per-pair models — not directly reusable for a
Kronos-based intraday forecaster, though the DI/outlier-rejection pattern
above is. `freqtrade/rpc/` (Telegram/webserver notification, ~dozens of
files) was skimmed only, not read in depth — general alerting glue, not
domain-specific. `freqtrade/optimize/hyperopt_tools.py` (parameter search over
strategy hyperparameters) was skipped entirely: irrelevant until a strategy
has tunable parameters worth searching, which this project's per-part design
doesn't currently have. No options-market code exists in freqtrade at all
(spot/futures/margin only via ccxt), so nothing here informs the options
segment's `implied-vol-surface` handling.
