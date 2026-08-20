# tensortrade-org/tensortrade — read 2026-08-20

Apache 2.0. Python. An open-source framework for building, training and evaluating
**reinforcement-learning agents for algorithmic trading** — a `gymnasium.Env` wrapping
a composable order-management system (exchange, wallet, portfolio, order, broker) plus
pluggable action/reward/observer components. Clone commit: `d58afba23deb1fded39793203b7997c3990bb032`.

Directories skipped: `docs/`, `examples/`, `tensortrade/agents/` (RL agent wrappers, not
OMS), `tensortrade/feed/` (the data-feed DSL — general-purpose stream algebra, not
trading-specific), `tensortrade/stochastic/` (synthetic price generators for training),
`tests/` (read only where it disambiguated behaviour). Focus was `tensortrade/oms/` and
`tensortrade/env/`.

## Mechanisms worth stealing (ranked)

1. **Wallet fund-locking against an order path** — a wallet holds `balance` (free) and
   `_locked: Dict[path_id, Quantity]` separately. `Wallet.lock(quantity, order, reason)`
   moves size out of `balance` into `_locked[order.path_id]` *before* the order is sent
   anywhere; `Wallet.unlock` reverses it. This is what stops two orders issued in the
   same tick from both spending the same capital. `tensortrade/oms/wallets/wallet.py:Wallet.lock`,
   `Wallet.unlock`. Directly relevant: our blueprint sizes orders (`position-sizer`) and
   accounts fills (`fill-reconciler`) but has no explicit "capital is reserved the instant
   an order is sent, before the fill comes back" concept.

2. **Conservation-of-funds assertion on every transfer** — `Wallet.transfer` computes
   both sides of the ledger equation independently (`lhs = (lsb1-lsb2)-(q+c)`,
   `rhs = (ltb2-ltb1)-cv`) and raises `Exception("Invalid Transfer: ...")` if they don't
   match to within the instrument's quantization, printing the full numeric breakdown.
   `tensortrade/oms/wallets/wallet.py:Wallet.transfer`, lines 344-365. This is a cheap,
   always-on correctness canary on the exact place money actually moves — worth having
   independent of any dashboard.

3. **Order state machine with explicit terminal/non-terminal states** —
   `OrderStatus = {PENDING, OPEN, CANCELLED, PARTIALLY_FILLED, FILLED}`; transitions live
   entirely in `Order.execute/fill/complete/cancel`, and `is_active`/`is_complete` are
   derived properties, never stored flags. `tensortrade/oms/orders/order.py:Order`
   (class docstring literally enumerates "1. Confirming its own validity... 5. Managing
   its own state changes"). Close in spirit to T-5 (closed state vocabulary) though the
   part does far more than hold state — see Anti-patterns.

4. **Contingent order chaining via `OrderSpec`** — an order can carry a queued
   specification for its *successor* order. `risk_managed_order()` builds an entry order,
   then attaches `OrderSpec(side=opposite, criteria=Stop("down", pct) ^ Stop("up", pct))`
   via `order.add_order_spec(...)`. When the entry order completes, `Order.complete()`
   pops the spec and calls `order_spec.create_order(self)`, producing the exit order with
   no external caller involved. `tensortrade/oms/orders/create.py:risk_managed_order`,
   `tensortrade/oms/orders/order.py:Order.complete` (lines 254-275),
   `tensortrade/oms/orders/order_spec.py`. This guarantees a stop/take-profit gets placed
   the instant the entry fills, not on the next scheduler tick.

5. **Composable boolean order-execution criteria** — `Criteria` overloads `&`, `|`, `^`,
   `~` so conditions combine like booleans: `Stop("down", 0.02) ^ Stop("up", 0.04)` reads
   as "exit on whichever comes first". `Limit`, `Stop` (direction + percent-from-entry),
   and `Timed` (max duration in steps) all ship as building blocks.
   `tensortrade/oms/orders/criteria.py:Criteria.__and__/__or__/__xor__/__invert__`,
   `Stop.check` (lines 189-196). `Order.is_executable` just calls
   `self.criteria(self, self.exchange_pair.exchange)` — the whole gating logic is one
   evaluable expression, not a chain of if-statements.

6. **Minimum-commission floor tied to instrument precision** — commission is
   `options.commission * filled`, but if that rounds below what the instrument can
   represent (`10 ** -instrument.precision`), the code force-sets it to that floor and
   logs a warning rather than silently losing the fee.
   `tensortrade/oms/services/execution/simulated.py:execute_buy_order`, lines 49-58 (and
   the mirrored `execute_sell_order`).

7. **Slippage as a separate, swappable post-fill adjustment** — the exchange's `_service`
   (e.g. `simulated.py:execute_order`) produces a `Trade` at the quoted price; a distinct
   `SlippageModel.adjust_trade(trade)` mutates `trade.price`/`trade.size` afterward.
   `RandomUniformSlippageModel` defaults to `max_slippage_percent = 3.0`, draws
   `np.random.uniform(0, 0.03)`, and for limit orders additionally shrinks the filled
   *size* proportionally to how far price moved against the limit rather than just
   moving price. `tensortrade/oms/services/slippage/random_slippage_model.py:RandomUniformSlippageModel.adjust_trade`.
   The fill mechanism and the cost-realism mechanism are two separate parts composed
   together, not one function doing both.

8. **Append-only ledger of every wallet movement, not just fills** — `Ledger.commit` is
   called from *four* places in `Wallet` (`lock`, `unlock`, `deposit`, `withdraw`), each
   recording a `Transaction(poid, step, source, target, memo, amount, free_after,
   locked_after, locked_poid_after)`. Source/target are formatted strings like
   `"{exchange}:{instrument}/free"` → `"{exchange}:{instrument}/locked"`, so the whole
   ledger reads like a double-entry account log, not just a trade blotter.
   `tensortrade/oms/wallets/ledger.py:Ledger.commit`,
   `tensortrade/oms/wallets/wallet.py:Wallet.lock` (lines 127-131) shows the call.

9. **Broker as an order book that self-drives fill-chaining** — `Broker.update()` scans
   `unexecuted` orders each step, executes the ones whose criteria now pass, and
   separately expires anything past `order.end`. As `OrderListener.on_fill`, it detects
   `order.is_complete` and automatically re-submits the order's queued successor spec.
   `tensortrade/oms/orders/broker.py:Broker.update` (lines 73-97), `Broker.on_fill`
   (lines 99-123).

10. **Net worth computed from a data-feed pattern-match, not hand-enumerated balances** —
    `Portfolio._find_keys` regex-matches feed column names ending in `:/free`,
    `:/locked`, `:/total`, or `worth`, and `Portfolio.on_next` reads whichever of those
    exist in the observer feed's `"internal"` node that step. `tensortrade/oms/wallets/portfolio.py:Portfolio._find_keys`,
    `Portfolio.on_next` (lines 256-320). Net worth is derived from declared data shape,
    not a part enumerating every wallet by name.

## Map onto existing parts

| existing part id | repo file (reference impl) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `paper-fill-simulator` | `tensortrade/oms/services/execution/simulated.py:execute_buy_order/execute_sell_order` | Concrete commission-floor logic tied to instrument precision, and limit-vs-market fill semantics (`order.type == MARKET` scales fill by `order.price/max(current_price, order.price)`). |
| `stop-target-placer` | `tensortrade/oms/orders/create.py:risk_managed_order` | Stop distance is expressed as `Criteria` objects composed with XOR, and the exit order is attached *to the entry order itself* so it fires on fill, not on the next scan of open positions. |
| `position-sizer` | `tensortrade/oms/orders/create.py:proportion_order`; `tensortrade/env/default/actions.py:SimpleOrders.get_orders` | Concrete minimum-order gates: `size < 10**-precision`, `size < min_order_pct * net_worth`, `size < min_order_abs` — three independent floors before a sized order is even created, any one of which silently produces no order. |
| `trade-lifecycle-recorder` | `tensortrade/oms/wallets/ledger.py:Ledger.commit` | Journals lock/unlock/deposit/withdraw as double-entry-style transfers between named `source`/`target` accounts, not just the final fill. |
| `fill-reconciler` | `tensortrade/oms/wallets/wallet.py:Wallet.lock/unlock/deposit/withdraw` | Reconciliation is structural: a wallet cannot spend what's locked and cannot double-lock (`DoubleLockedQuantity`), enforced at the data-structure level rather than by a downstream check. |

## New part proposals

**`fund-lock-ledger`** — block: `portfolio-state`.
consumes: `sized-order`, `account-balance`.
produces: NEW `locked-allocation` — capital earmarked against one order's path id,
still owned by the account but unavailable to a second order until the first fills,
partially fills, or is cancelled.
Responsibility: reserve capital the instant an order is sent, before any fill comes
back, so two sized orders issued in the same tick can never spend the same balance.
Evidence: `tensortrade/oms/wallets/wallet.py:Wallet.lock`, `Wallet.unlock`.

**`exit-order-chainer`** — block: `risk-capital-allocation`.
consumes: `stop-target-plan`, `fill`.
produces: `order-request`.
Responsibility: the moment an entry fill lands, emit the paired stop-loss/take-profit
order-request directly, without waiting for the arbiter or scanner to notice the new
position on their own schedule.
Evidence: `tensortrade/oms/orders/create.py:risk_managed_order` (the `OrderSpec` attach),
`tensortrade/oms/orders/order.py:Order.complete` (pops and fires the spec).

**`fund-conservation-auditor`** — block: `observability`.
consumes: `fill`, `journal-entry`.
produces: `alert`.
Responsibility: independently recompute both sides of a fill's balance-sheet equation
(what left one wallet vs. what landed in the other, minus commission) and raise if they
don't match — a canary that catches a broken fill-simulator or fill-reconciler the same
step it happens, not on next audit.
Evidence: `tensortrade/oms/wallets/wallet.py:Wallet.transfer`, lines 344-365 (the
`lhs`/`rhs` equality check and the raised exception with the full numeric breakdown).

**`exit-criteria-compiler`** — block: `risk-capital-allocation`.
consumes: `stop-target-plan`.
produces: NEW `exit-condition` — a boolean-composable trigger (stop-loss OR take-profit
OR max-duration) evaluated against `market-data` every tick, distinct from
`watch-condition` (which is entry-side, compiled from a `proven-instruction`).
Responsibility: turn a stop/target plan into one evaluable expression instead of several
independent if-checks scattered across the exit path.
Evidence: `tensortrade/oms/orders/criteria.py:Criteria.__xor__/__and__/__or__`, `Stop.check`,
`Timed.check`.

## Anti-patterns seen

- **A data object (`Order`) directly drives another component's method.**
  `Order.execute()` calls `self.exchange_pair.exchange.execute_order(self, self.portfolio)`
  — the order holds live references to both the `Exchange` and `Portfolio` objects and
  invokes behaviour on them directly, rather than the order being a typed message that
  something else routes. `tensortrade/oms/orders/order.py:Order.execute`, line 234;
  `Order.__init__` stores `self.portfolio = portfolio` at line 106. This is exactly the
  T-4 violation the harvest brief called out in advance: a component holding a direct
  object reference to another component instead of exchanging only typed data.

- **Synchronous listener callbacks wire components together outside the data path.**
  `Order` extends `Observable` and keeps a mutable `self.listeners` list; `Broker`
  attaches itself as a listener (`order.attach(self)`,
  `tensortrade/oms/orders/broker.py:Broker.update`, line 89) and `Portfolio.order_listener`
  is injected into every order at construction
  (`tensortrade/oms/orders/order.py:Order.execute`, lines 228-229). Two parts now call
  directly into each other's methods on every state change — a second, invisible graph
  running underneath the declared data flow, which is precisely what T-2 forbids a
  feature from doing to another feature.

- **Hidden shared mutable global state.** `Wallet.ledger = Ledger()` is a *class*
  attribute (`tensortrade/oms/wallets/wallet.py`, line 48) — every `Wallet` instance
  shares one `Ledger` object unless a caller overrides it explicitly. `Wallet.reset()`
  does not reset it; only `Portfolio.reset()` remembers to call `self.ledger.reset()`
  separately (`tensortrade/oms/wallets/portfolio.py:Portfolio.reset`, line 328). State
  ownership is implicit and easy to get wrong — the opposite of T-3's "off means
  genuinely off, and it's this part's own resource."

- **God-object parts that admit to doing multiple jobs.** `Order`'s own docstring lists
  five distinct responsibilities — confirming validity, tracking trades, moving
  quantities, generating the next order in its path, and managing its own state
  (`tensortrade/oms/orders/order.py:Order`, lines 44-50). `Portfolio` similarly combines
  wallet registry, ledger accessor, net-worth calculator, performance tracker, and
  observer-feed subscriber in one class (`tensortrade/oms/wallets/portfolio.py`). Under
  T-6 each of those is a separate part with a one-sentence role, not one class doing all
  of them.

## Not useful here

The whole system is built around `tensortrade/env/generic/environment.py:TradingEnv`, a
`gymnasium.Env` subclass — `step()`/`reset()` are shaped for a single-threaded RL
training loop stepping through a fixed, replayable historical `DataFeed`, not an
async/live tick stream driving three concurrent segments (spot/futures/options) each with
independent risk budgets. There is no concept of "paper vs. live money mode," no
segment isolation, and no options instruments anywhere in `oms/instruments` (no implied
vol, no greeks, no strikes/expiries). Most damagingly for "reference implementation of a
live venue adapter": `tensortrade/oms/services/execution/ccxt.py`,
`tensortrade/oms/services/execution/interactive_brokers.py`, and `tensortrade/oms/services/execution/robinhood.py` are all **empty files (0 bytes)** in this
clone despite existing in the directory tree and being named after real venues — only
`tensortrade/oms/services/execution/simulated.py` actually executes anything, so there is no working live-exchange adapter
to learn from here, only the simulated-fill mechanics. `tensortrade/stochastic/` (Brownian
motion, GBM, etc.) generates synthetic price series for training and has no bearing on a
live-data paper bot. The whole locking/`OrderSpec`-chaining machinery is single-process,
in-memory, non-persistent — useful as a design reference for the *mechanism*, not as
code to run unattended for a 24/7 bot.
