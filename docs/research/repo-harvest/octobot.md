# Drakkar-Software/OctoBot — read 2026-08-20

Licence: root repo is GPL-3.0 (`LICENSE`); the vendored trading engine package
(`packages/trading`) carries LGPL-3.0 headers on every file. Language: Python, built as a
Pants monorepo (`pants.toml`) of packages, not a single flat package. One-liner: a
tentacle-plugin-based crypto trading bot with a real vendored trading engine
(`octobot_trading`) shared across backtest/live, plus a large separate plugin ecosystem.
Clone commit sha: `dc0efc8ec36c138bd619272b2b59042778668408`

The top-level `octobot/` directory (referenced in the harvest brief) is thin orchestration
— CLI, node bootstrapping, community/API glue. The actual mechanisms live in
`packages/trading/octobot_trading/` (7.2MB), `packages/evaluators/` (552KB) and
`packages/backtesting/` (2.6MB), which is where this note focuses.

## Mechanisms worth stealing (ranked)

1. **Position-sizing DSL with six amount types** —
   `packages/trading/octobot_trading/modes/script_keywords/basic_keywords/amount.py:get_amount_from_input_amount`.
   A single entry point resolves `DELTA`/`DELTA_QUOTE` (fixed base/quote amount, converted
   at current price), `PERCENT`/`AVAILABLE_PERCENT` (percent of total vs. available
   balance), `CURRENT_SYMBOL_ASSETS_PERCENT`/`TRADED_SYMBOLS_ASSETS_PERCENT` (percent of
   one symbol's or all traded symbols' holdings value), and `POSITION_PERCENT` (percent of
   an existing futures position's size, one-way mode only). Every result is then clamped
   through holdings adaptation (next item) before being returned.
2. **Requested-amount clamped to available balance, not refused** —
   `packages/trading/octobot_trading/modes/script_keywords/basic_keywords/account_balance.py:adapt_amount_to_holdings`
   calls `available_account_balance` (which itself nets out amounts already locked in
   resting stop orders via `_get_locked_amount_in_stop_orders`) and returns
   `min(requested_amount, available_balance)` — the requested size is silently reduced to
   what's actually free rather than the whole order being denied, with an explicit opt-out
   (`allow_holdings_adaptation=False` in `amount.py`) to make that same case a hard error.
3. **Linear-contract liquidation price formula** —
   `packages/trading/octobot_trading/personal_data/positions/types/linear_position.py:LinearPosition.update_isolated_liquidation_price`.
   `LONG liquidation = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)`;
   `SHORT liquidation = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)`,
   where `initial_margin_rate = 1 / leverage` (`position.py:Position.get_initial_margin_rate`).
   Simple, auditable, and exactly the number a leveraged crypto bot needs before the venue
   computes it independently.
4. **Bankruptcy price feeds fee-to-close, which feeds margin** —
   `linear_position.py:LinearPosition.get_bankruptcy_price` (`long = entry_price * (1 -
   initial_margin_rate)`, `short = entry_price * (1 + initial_margin_rate)`), consumed by
   `get_fee_to_close` (`fee = qty * bankruptcy_price * taker_fee`), which
   `update_fee_to_close` folds into `margin = initial_margin + fee_to_close`
   (`position.py:Position._update_margin`). Chaining these three numbers together means
   the margin reservation already accounts for the cost of closing the position, not just
   opening it.
5. **Liquidation is a raised error the position state machine catches, not a silent flag**
   — `packages/trading/octobot_trading/personal_data/positions/position.py:Position._check_for_liquidation`
   raises `errors.LiquidationPriceReached` when `mark_price` crosses `liquidation_price` on
   the wrong side (`short: mark_price >= liquidation_price`; `long: mark_price <=
   liquidation_price`); the caller transitions the position through
   `_create_liquidation_state` into a dedicated `PositionState` with `is_liquidated()`
   (`personal_data/positions/position_state.py:PositionState`). A liquidation is a distinct,
   named, closed-set state — not a boolean bolted onto the position.
6. **Order cancellation is a pluggable policy, not inline logic** —
   `packages/trading/octobot_trading/personal_data/orders/cancel_policies/` — separate
   `OrderCancelPolicy` subclasses for `expiration_time_order_cancel_policy.py` (pull an
   order after a time-to-live) and
   `chained_order_filling_price_order_cancel_policy.py` (pull a child order if the parent
   fill price drifted). `cancel_policy_factory.py` selects among them — the same
   one-responsibility-per-implementation shape T-1 asks for, applied to a part that's
   easy to leave as an inline if/else.
7. **Order lifecycle is one state class per phase** —
   `packages/trading/octobot_trading/personal_data/orders/states/`:
   `pending_creation_order_state.py`, `open_order_state.py`, `fill_order_state.py`,
   `close_order_state.py`, `cancel_order_state.py`, dispatched by
   `order_state_factory.py`. A closed, named set of states per T-5, rather than status
   strings compared ad hoc.
8. **Position side is derived from the sign of a single quantity field** —
   `position.py:Position._update_side`: in one-way position mode, `side = LONG` if
   `quantity > 0`, `SHORT` if `< 0`, `UNKNOWN` if `0` — one field is the only source of
   truth for direction, avoiding a `side` flag that can drift out of sync with the actual
   signed size.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that our part description does not yet say |
|---|---|---|
| `position-sizer` | `packages/trading/octobot_trading/modes/script_keywords/basic_keywords/amount.py:get_amount_from_input_amount` | six distinct sizing modes (fixed delta, quote-converted delta, percent of total/available balance, percent of one or all traded symbols' holdings, percent of an existing position) behind one entry point |
| `position-sizer` | `packages/trading/octobot_trading/modes/script_keywords/basic_keywords/account_balance.py:adapt_amount_to_holdings` | clamps the requested size down to what's actually free rather than refusing outright, with an explicit flag to make that a hard refusal instead — our part description only mentions "or a refusal" |

## New part proposals

- **`liquidation-price-tracker`** — block: `portfolio-state`. consumes: `position`,
  `leverage-choice`, `market-data`. produces: NEW `liquidation-price`. Responsibility:
  continuously compute the price at which this bot's *own* leveraged position gets force-
  closed by the venue, from entry price, leverage and the venue's maintenance-margin rate.
  Distinct from `liquidation-map` (the crowd's liquidation levels, used to avoid placing a
  stop where the crowd's stops are) — this is the bot's own number, and nothing in the
  blueprint currently produces it. Evidence:
  `packages/trading/octobot_trading/personal_data/positions/types/linear_position.py:LinearPosition.update_isolated_liquidation_price`.
- **`resting-order-cancel-policy`** — block: `execution-venue-adapter`. consumes:
  `order-request`, `market-data`. produces: NEW `cancel-decision`. Responsibility: decide
  when a still-open order should be pulled — expired past its time-to-live, or its parent
  fill price drifted too far — as its own pluggable, testable check, separate from
  `order-resubmitter` (which reacts to a venue *rejection*, not a still-live order going
  stale). Evidence: `packages/trading/octobot_trading/personal_data/orders/cancel_policies/`.

## Anti-patterns seen

- `AbstractTradingMode` (`packages/trading/octobot_trading/modes/abstract_trading_mode.py`)
  is simultaneously the strategy's decision logic *and* the thing that creates and owns its
  own producer/consumer channel wiring (`create_producers`, `create_consumers`,
  `start_producers`) — one class holds both what T-2 calls the data plane (strategy
  opinions) and control-plane responsibility (spinning up its own message channels).
- Deep, un-typed attribute chains thread a shared mutable object graph through business
  logic instead of passing data types — e.g.
  `context.exchange_manager.exchange_personal_data.portfolio_manager.portfolio_value_holder.get_assets_holdings_value(...)`
  in `amount.py`. A part reaching four objects deep into another part's internals is the
  opposite of T-4 (a part should only ever be handed the data type it needs).
- No slippage or fill-price model was found anywhere under `packages/trading` (`grep -rl
  slippage packages/trading/octobot_trading` returns nothing): backtest/paper fills read
  the historical price straight from the data importer with no simulated market impact.
  Backtests always report the "perfect fill" case, which — since it never varies whether
  the simulator is exercised carefully or not — hides exactly the failure mode Rule 8
  warns about: a result that looks measured but is actually just optimistic by
  construction.

## Not useful here

The tentacle plugin architecture (`packages/tentacles`, 23MB of installable
community-contributed strategies/evaluators/exchange configs, versioned and distributed
through `packages/tentacles_manager`) is a whole packaging and marketplace system that
doesn't apply to a project that owns all three segment bots directly rather than
distributing strategies to third parties. The web/SaaS-oriented pieces
(`octobot/community`, parts of `octobot/api` built for a hosted dashboard) and the
copy-trading package (`packages/copy`, for mirroring a *different* OctoBot instance's
trades peer-to-peer — not the same shape as this project's own `profit-tailgater`, which
copies public on-chain traders) are also out of scope. Given the time budget,
`packages/tentacles` itself, `packages/protocol`, `packages/node`, `packages/services`,
`packages/sync`, `packages/async_channel`, `packages/client`, `packages/agents`,
`packages/binary`, and `packages/backtesting` internals beyond confirming its directory
layout were not read — only `packages/trading` (positions/orders/portfolio/modes) and the
`script_keywords` sizing DSL were read in depth, per the "risk, execution, backtest,
portfolio" focus given for the larger repos, applied here by analogy since octobot's real
engine similarly turned out to live one level below the top-level package name.
