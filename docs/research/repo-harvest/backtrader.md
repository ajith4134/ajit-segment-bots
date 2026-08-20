# mementum/backtrader — read 2026-08-20

GNU GPLv3. Python 2/3 (py2-compat shims throughout, e.g. `from __future__ import ...` in every file). Event-driven backtesting + live-trading framework: a `Cerebro` engine iterates bars over `Strategy` objects that place orders through a `Broker` interface, simulated (`BackBroker`) or live (via a `Store`/broker pair such as `IBStore`/`IBBroker`). Clone commit: `b853d7c90b6721476eb5a5ea3135224e33db1f14`.

**Unmaintained since 2024-08** (last commit era) — read for mechanisms only, not architecture. See "Not useful here".

## Mechanisms worth stealing (ranked)

1. **Slippage as a capped price transform, not a random draw.** `_slip_up`/`_slip_down` in `backtrader/brokers/bbroker.py:BackBroker._slip_up` compute `pslip = price * (1 + slip_perc)` (or `price + slip_fixed`), then cap it to the bar's high/low (`pmax`/`pmin`) unless `slip_out=True` lets it exceed the bar range. This is deterministic and bar-bounded rather than a stochastic noise model — good for a first-pass `cost-estimate` that a random model would only add noise to.

2. **Execution-type-specific fill matching.** `BackBroker._try_exec` (`backtrader/brokers/bbroker.py:1040`) dispatches Market/Close/Limit/Stop/StopLimit/StopTrail differently: a Limit buy fills at `min(high, limit)` if `limit >= open` else at the exact limit if the low touched it (`_try_exec_limit`, line 898); a Stop fills at the trigger price or the gapped open, whichever is worse for the trader (`_try_exec_stop`, line 921). This distinguishes "gapped through the level" from "touched the level intraday" — directly relevant to crypto's frequent gap-on-news behavior.

3. **Cheat-on-close / cheat-on-open toggle for backtest realism control.** `coc`/`coo` params (`bbroker.py:179-192`) let a strategy match a Market order to the *current* bar's close (cheating) vs. the *next* bar's open (realistic). Framework makes the cheat explicit and named rather than silently baking it in — a naming pattern worth copying for our own `execution-cost-model`/`instruction-replayer` so a report can say which mode a run used.

4. **Position update returns opened/closed split with average-price and reversal handling.** `backtrader/position.py:Position.update` returns `(size, price, opened, closed)`; on a same-side increase it recomputes the average price (`(price*oldsize + size*price)/newsize`); on a reversal (long to short in one fill) it correctly splits the fill into a `closed` part (at old avg price) and an `opened` part (at new price), each with its own signed count. Distinguishes an order that reduces a position from one that reverses through zero — a common intraday crypto case (bot flips bias) that a naive position tracker gets wrong.

5. **Commission scheme is a pluggable, swappable object, not inline arithmetic.** `backtrader/comminfo.py:CommInfoBase` centralizes `getcommission`, `profitandloss`, `cashadjust`, `get_credit_interest` (funding-like carry cost: `days * price * abs(size) * (interest/365)`, `comminfo.py:274 _get_credit_interest`) and margin (`get_margin`, supports `automargin` scaling by price). The `interest`/`interest_long` mechanism is structurally identical to perpetual-futures funding accrual — a stock-borrow-fee formula reused as a funding-rate carry model.

6. **SQN (System Quality Number): `sqrt(N) * mean(pnl) / stddev(pnl)`.** `backtrader/analyzers/sqn.py:SQN.stop` — trades pulled from `trade.pnlcomm` (net of commission) on `trade.status == Closed`. Documented thresholds: 1.6-1.9 below average, 2.0-2.4 average, 2.5-2.9 good, 3.0-5.0 excellent, 5.1-6.9 superb, reliable only at N>=30 trades. A concrete, cited scalar for `backtest-scorer`/`instruction-scorecard` beyond raw PnL.

7. **Drawdown tracked as a running peak-vs-current-value ratio, updated every bar.** `backtrader/analyzers/drawdown.py:DrawDown.next` — `moneydown = maxvalue - value`; `drawdown = 100*moneydown/maxvalue`; `max.len` counts consecutive bars in drawdown (resets to 0 the instant `drawdown` hits exactly 0). Trivial formula but the *streak-length* tracking (not just magnitude) is the part worth lifting for `drawdown-breaker`.

8. **Sharpe ratio converts an annual risk-free rate to the trade timeframe via a lookup table** (`backtrader/analyzers/sharpe.py:SharpeRatio.stop`, `RATEFACTORS = {Days:252, Weeks:52, Months:12, Years:1}`), with `rate = (1+annual_rate)^(1/factor) - 1` — a correct compounding conversion rather than a naive division, and a `stddev_sample` flag for Bessel's correction on small trade counts.

9. **Volume-capped partial fills via a pluggable `filler` callable.** `backtrader/fillers.py:FixedBarPerc.__call__` returns `min(bar_volume * perc/100, remaining_order_size)` — an order can only ever consume a declared percentage of a bar's traded volume, producing realistic partial fills over multiple bars for large orders relative to liquidity. Directly maps to `execution-cost-model` needing to cap simulated fill size against real book depth rather than assuming infinite liquidity.

10. **Store-as-adapter pattern for live parity.** `backtrader/store.py:Store` is a singleton (`MetaSingleton`) holding one connection; `Store.getbroker()`/`Store.getdata()` hand out a `BrokerCls`/`DataCls` pair that share the connection. `backtrader/stores/ibstore.py` (1512 lines) wraps the Interactive Brokers API into this shape; `backtrader/brokers/ibbroker.py:IBBroker` implements the same `submit/buy/sell/cancel/getposition` surface as `BackBroker`, so a `Strategy` written against `self.broker.buy(...)` runs unmodified against paper (`BackBroker`) or live (`IBBroker`) — this is the actual mechanism behind backtest/paper/live parity, worth citing even though the concrete stores (IB/Oanda/VisualChart) are irrelevant to crypto.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `paper-fill-simulator` | `backtrader/brokers/bbroker.py:BackBroker._try_exec_limit/_try_exec_stop/_try_exec_market` | Exact per-order-type fill rule (gap vs. intraday-touch, cap at bar high/low) rather than a single generic "fill at market price" — our part description only says "fill at live price with cost applied," not how a limit/stop order's fill price is determined against a candle's O/H/L/C.
| `execution-cost-model` | `backtrader/brokers/bbroker.py:BackBroker._slip_up/_slip_down`, `backtrader/fillers.py:FixedBarPerc.__call__` | A concrete, bar-range-capped slippage formula and a volume-participation cap on fill size — our part names "spread crossed, slippage, fees" but not a formula or a liquidity cap.
| `slippage-learner` | `backtrader/comminfo.py:CommInfoBase._get_credit_interest` | Not slippage per se, but shows funding/carry cost measured as `days * price * size * rate` — a pattern for a `slippage-profile`-adjacent carry-cost measurement our blueprint doesn't currently name anywhere.
| `usdt-pnl-accountant` | `backtrader/trade.py:Trade.update` | Splits gross `pnl` from `pnlcomm` (net of commission) explicitly per trade, and tracks average entry price through partial adds/reduces/reversals — our part says "state PnL in USDT" but not the opened/closed/reversal price-averaging logic needed to compute it correctly.
| `backtest-scorer` | `backtrader/analyzers/sqn.py:SQN.stop`, `backtrader/analyzers/drawdown.py:DrawDown.next`, `backtrader/analyzers/sharpe.py:SharpeRatio.stop` | Concrete formulas (SQN, max-drawdown streak length, timeframe-adjusted Sharpe) our part description leaves unspecified.
| `position-sizer` | `backtrader/sizers/percents_sizer.py:PercentSizer._getsizing` | Trivial (`cash * pct / price`) but confirms sizing-as-a-pluggable-swappable-object shape, matching T-6 (grow by adding a new Sizer, never editing one).

## New part proposals

- **id:** `carry-cost-accountant`
  **block:** `risk-capital-allocation`
  **consumes:** `position`, `market-data`
  **produces:** NEW `carry-cost` (funding/borrow cost accrued per open position per period, days-elapsed × rate × notional)
  **responsibility:** measure the ongoing cost of holding a leveraged position between fills, separate from the entry/exit commission.
  **evidence:** `backtrader/comminfo.py:CommInfoBase.get_credit_interest/_get_credit_interest` (interest-bearing short/carry formula, structurally identical to perpetual funding accrual).

- **id:** `fill-volume-capper`
  **block:** `backtesting`
  **consumes:** `order-request`, `market-data` (bar/trade volume)
  **produces:** `cost-estimate` refinement — NEW `fillable-size` (max size a simulated fill may take from one bar/tick given a participation cap)
  **responsibility:** cap a simulated fill's size against real traded volume so a backtest never assumes infinite liquidity in one tick.
  **evidence:** `backtrader/fillers.py:FixedBarPerc.__call__`, `FixedSize.__call__`.

- **id:** `drawdown-streak-tracker`
  **block:** `observability`
  **consumes:** `account-balance`
  **produces:** NEW `drawdown-streak` (consecutive periods currently in drawdown, plus its historical max)
  **responsibility:** track how long a drawdown has persisted, not just how deep it is, so `drawdown-breaker` can act on duration as well as magnitude.
  **evidence:** `backtrader/analyzers/drawdown.py:DrawDown.next` (`r.len`/`r.max.len` streak counters).

## Anti-patterns seen

- **`Cerebro` is a 1716-line god object** (`backtrader/cerebro.py:60 class Cerebro`) that owns strategy registration, data feeds, timers, calendars, signals, stores, writers, sizers, indicators, analyzers, observers, the broker, and the run loop/optimizer — one class doing what T-1/T-6 require to be many interchangeable parts. Direct violation of "grow by adding parts, never by making a part cleverer."
- **A `Strategy` reaches directly into `self.broker` and `self.cerebro` globals** for cash, value, timers, and data lookup (`backtrader/strategy.py:472-1374`, e.g. `self.broker.getvalue()`, `self.cerebro._add_timer(...)`, `self.env.datasbyname[name]`) rather than being handed only the data it consumes — a T-4 violation (a part naming/reaching into another part) and a T-2 violation (a feature able to act through another feature's internals rather than through a data-typed interface).
- **Heavy metaclass magic** (`backtrader/metabase.py:MetaBase`, `MetaParams`) auto-injects `params`, intercepts `__new__`/`__call__` to run lifecycle hooks implicitly. This hides object construction behind non-obvious class machinery — the opposite of T-5's requirement that states/lifecycle be explicit and declared, not implicit.
- **`Store` is a hard singleton** (`backtrader/store.py:MetaSingleton`) — one process can have exactly one IB/Oanda connection system-wide, enforced by metaclass rather than by the resource governor. Control over "how many of this part exist and when" is baked into the part itself instead of living in the control plane (T-2/T-3).

## Not useful here

**backtrader is unmaintained since 2024-08** — do not adopt its dependency stack (Python 2/3 dual-compat cruft, no asyncio) or fork it as a base; its single-threaded, synchronous, pull-based bar iteration (`Cerebro._runnext`/`_runonce`) is a poor fit for an async multi-exchange ccxt feed with sub-second ticks — it assumes one bar arrives, gets fully processed, then the next bar arrives, with no concept of concurrent venues or streaming order-book depth. Its live-broker stores (`ibstore.py`, `oandastore.py`, `vcstore.py`) target Interactive Brokers, Oanda FX, and VisualChart — equities/forex venues with session calendars (`tradingcal.py`), end-of-session bars (`eosbar`), and T+ settlement assumptions that don't exist in 24/7 perpetual crypto markets; skipped reading these in depth beyond confirming the store/broker adapter shape. Its plotting (`backtrader/plot/`) and `btrun` CLI runner were not read — out of scope for mechanism-harvesting.
