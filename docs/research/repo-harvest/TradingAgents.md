# TauricResearch/TradingAgents — read 2026-08-20

Apache 2.0. Python (>=3.10), LangGraph + LangChain, multi-provider LLM (OpenAI,
Anthropic, Google, Azure, Bedrock). "Multi-Agents LLM Financial Trading
Framework" — a LangGraph pipeline of analyst → bull/bear researcher →
research-manager → trader → risk-debate → portfolio-manager agents that
debate a single ticker on a single date and emit a 5-tier rating (Buy /
Overweight / Hold / Underweight / Sell). No live order routing, no fill
simulation, no numeric position sizing — every "decision" is LLM prose with
optional structured fields. Clone commit `a33fd4c0f134485a43553a2c23a63cb14adbd88f`.

## Mechanisms worth stealing (ranked)

1. **Deterministic anti-hallucination snapshot fed alongside LLM analysis.**
   `build_verified_market_snapshot()` in
   `tradingagents/dataflows/market_data_validator.py` computes the latest
   OHLCV row, a fixed indicator set (`close_10_ema, close_50_sma,
   close_200_sma, rsi, boll, boll_ub, boll_lb, macd, macds, macdh, atr`) and
   the last ≤30 closes with **zero LLM involvement**, then instructs the
   analyst prompt to treat it as ground truth and "flag the discrepancy
   rather than inventing a reconciled number." Exists specifically because
   the LLM analyst was confabulating exact numbers (repo issue #830). Any
   bot part that hands numeric evidence to an LLM (`bull-bot`, `bear-bot`,
   `decision-quality-critic`) should pair its prompt with a deterministic,
   non-LLM snapshot the model is told is authoritative.

2. **Look-ahead guard baked into the data loader, not the backtester.**
   `load_ohlcv()` in `tradingagents/dataflows/stockstats_utils.py:148` filters
   `data[data["Date"] <= curr_date_dt]` on every call — including live runs —
   and `_assert_ohlcv_not_stale()` (line 94) raises `NoMarketDataError` if the
   latest row is more than `MAX_OHLCV_STALE_DAYS=10` before `curr_date`.
   `filter_financials_by_date()` (line 224) does the same for fundamentals
   columns. This makes look-ahead prevention a property of every data read,
   not a separate audit step run only during backtests.

3. **Deferred two-phase reflection so outcomes are scored without blocking
   the pipeline.** `TradingAgentsGraph._resolve_pending_entries()` and
   `_fetch_returns()` in `tradingagents/graph/trading_graph.py:251-334`:
   Phase A (`TradingMemoryLog.store_decision`) writes a decision with an
   `pending` outcome tag and returns immediately. Phase B runs at the *start*
   of the *next* run for the same ticker: it fetches realised + benchmark
   returns via yfinance, computes `alpha = raw - bench_ret`
   (`_resolve_benchmark` picks SPY / ^N225 / ^NSEI / etc. by ticker suffix,
   `tradingagents/graph/trading_graph.py:230`), and only then calls
   `Reflector.reflect_on_final_decision()` (`tradingagents/graph/reflection.py:31`)
   to produce a 2-4 sentence lesson, stored verbatim and re-injected as
   context on future runs. Entries whose price data isn't available yet
   (too recent) are simply skipped and retried next run — no blocking wait.

4. **Append-only markdown decision log with atomic rewrite.**
   `TradingMemoryLog` (`tradingagents/agents/utils/memory.py`) appends
   pending entries with `open(path, "a")`, but every *update* (resolving an
   outcome) goes through `_apply_rotation` + a temp-file `write_text` +
   `Path.replace()` (line 160-162, and `batch_update_with_outcomes` line
   214-216) so a crash mid-write never corrupts the log. A hard, LLM-output-safe
   separator (`"\n\n<!-- ENTRY_END -->\n\n"`, an HTML comment that can't appear
   in generated prose) delimits entries instead of a fragile regex boundary.
   Optional `memory_log_max_entries` rotation drops the oldest *resolved*
   entries first, keeping every pending one.

5. **Vendor-error taxonomy that lets the router react to behavior, not
   vendor identity.** `tradingagents/dataflows/errors.py`: three classes —
   `NoMarketDataError`, `VendorRateLimitError`, `VendorNotConfiguredError` —
   all inherit `VendorError`. New vendors raise one of these three and the
   router needs no new `except` clause. Directly reusable shape for
   `order-reject-classifier` (classify by behavior: balance / rate-limit /
   price-band / size-step / outage) instead of a vendor-specific if-ladder.

6. **Checkpoint/resume keyed by a graph-shape signature, not just
   ticker+date.** `tradingagents/graph/checkpointer.py` + `trading_graph.py:348`
   (`_run_signature`): the LangGraph SqliteSaver thread ID is
   `sha256(f"{ticker}:{date}:{analysts=...,debate=...,risk=...,asset=...}")`.
   Changing the analyst selection, debate depth, or asset mode invalidates
   the checkpoint automatically instead of silently resuming into a
   differently-shaped run (repo issue #1089). One SQLite DB per ticker so
   concurrent tickers don't contend on writes.

7. **Structured-output-with-fallback wrapper used uniformly across every
   decision-making agent.** `tradingagents/agents/utils/structured.py`:
   `bind_structured()` tries `llm.with_structured_output(schema)` and returns
   `None` on `NotImplementedError`/`AttributeError`; `invoke_structured_or_freetext()`
   then tries the structured call, and on *any* exception (including a
   thinking model answering in plain prose so the parser gets nothing back,
   handled explicitly at line 76-80) falls back to one plain `llm.invoke()`.
   Every schema (`TraderProposal`, `PortfolioDecision`, `ResearchPlan`,
   `SentimentReport` in `tradingagents/agents/schemas.py`) also carries a
   `render_*()` function that turns the parsed object back into the exact
   markdown shape downstream consumers already expect — schema and legacy
   text format never fork.

8. **Symbol-normalization table centralizes vendor quirks instead of
   scattering them at call sites.** `tradingagents/dataflows/symbol_utils.py`
   maps broker-style symbols (`XAUUSD`, `SPX500`, `BTCUSD`) to vendor-native
   ones (`GC=F`, `^GSPC`, `BTC-USD`) via one alias table + a forex/crypto
   suffix heuristic (`_FOREX_CURRENCIES`, `_CRYPTO_BASES`, `_CRYPTO_QUOTES`).
   New instruments are "add a table row," never "edit a call site" — the
   same shape the blueprint wants for exchange/venue symbol handling.

9. **Path-map completeness enforced at the routing edge, not by convention.**
   `tradingagents/graph/setup.py:32-42`: `DEBATE_PATH_MAP` and
   `RISK_ANALYSIS_PATH_MAP` list *every* string a conditional router can
   return, and both debate-loop nodes / all three risk nodes are wired
   through the *same* shared dict object (repo issue #1088) — so a
   speaker-label typo or i18n rename can no longer produce a return value
   with no matching graph edge, which used to crash LangGraph mid-run.

## Map onto existing parts

| existing part id | repo file (reference impl) | what the repo does that our part description doesn't yet say |
|---|---|---|
| `regime-break-detector` | `tradingagents/dataflows/market_data_validator.py:build_verified_market_snapshot` | Concrete "ground truth beats LLM claim" contract: a deterministic snapshot is computed and the consuming prompt is told explicitly to prefer it over any other tool output and to flag disagreement rather than reconcile — a pattern for any part that must stop an LLM from asserting a number it wasn't given. |
| `historical-bar-store` / `lookahead-auditor` | `tradingagents/dataflows/stockstats_utils.py:load_ohlcv`, `_assert_ohlcv_not_stale` | Look-ahead filtering (`Date <= curr_date`) and staleness rejection (`MAX_OHLCV_STALE_DAYS`) happen inside the *live* data loader, not only inside a separate backtest auditor — every caller, live or replay, gets the same guarantee for free. |
| `exit-quality-scorer` / `forecast-scorer` | `tradingagents/graph/trading_graph.py:_fetch_returns`, `_resolve_benchmark` | Concrete alpha computation: `raw_return` and `alpha_return = raw - benchmark_return`, with the benchmark chosen per-ticker by exchange-suffix table (`.T`→`^N225`, `.NS`→`^NSEI`, etc.) — a ready pattern for scoring a directional-opinion against a market-relative baseline rather than absolute PnL alone. |
| `order-reject-classifier` | `tradingagents/dataflows/errors.py` | A 3-member exception taxonomy (`NoMarketDataError`, `VendorRateLimitError`, `VendorNotConfiguredError`) that the router catches by base class — shows how to keep the number of reject categories equal to the number of distinct *reactions*, not the number of human-describable causes. |

## New part proposals

- **`ground-truth-snapshot-builder`** — block: `prediction`.
  consumes: `market-data` (existing).
  produces: `NEW verified-snapshot` — a deterministic, non-LLM rendering of
  the latest OHLCV row, a fixed indicator set, and recent closes, with no
  forecast or inference layered in.
  responsibility: compute a ground-truth numeric snapshot an LLM-backed part
  can be told to defer to, so it stops asserting invented numbers.
  repo evidence: `tradingagents/dataflows/market_data_validator.py:62-123`.

- **`decision-outcome-resolver`** — block: `closed-trade-decoding`.
  consumes: `closed-trade` (existing), `market-data` (existing).
  produces: `NEW outcome-resolution` — realised return plus benchmark-relative
  alpha for one closed trade, or nothing yet if price data isn't available.
  responsibility: resolve a trade's raw and alpha return only once market
  data for the holding period actually exists, deferring rather than
  blocking or guessing.
  repo evidence: `tradingagents/graph/trading_graph.py:251-334` (`_fetch_returns`,
  `_resolve_pending_entries`).

- **`symbol-alias-resolver`** — block: `market-data-feed`.
  consumes: `NEW raw-symbol-input` — a symbol as the user or an upstream
  source typed it, in whatever vendor convention it arrived in.
  produces: `NEW canonical-symbol` — the venue-native symbol string plus
  which alias rule matched.
  responsibility: translate a broker/user-style symbol into the exact string
  a given venue's API expects, from one editable alias table.
  repo evidence: `tradingagents/dataflows/symbol_utils.py`.

## Anti-patterns seen

- **One giant shared mutable state dict every node reads and writes by
  string key.** `tradingagents/agents/utils/agent_states.py` — `AgentState`
  (a `MessagesState` subclass) carries `market_report`, `sentiment_report`,
  `investment_debate_state`, `risk_debate_state`, `trader_investment_plan`,
  `final_trade_decision`, etc. all in one TypedDict, and every agent node
  function (`tradingagents/agents/trader/trader.py`,
  `tradingagents/agents/managers/portfolio_manager.py`,
  `tradingagents/agents/risk_mgmt/aggressive_debator.py`, ...) reads and
  writes into it directly by key name. This is the opposite of T-4/R-01:
  parts don't declare typed consumes/produces, they share one blob and know
  every other part's field names by convention.

- **Nodes route to each other by hardcoded string identity, not by data
  type.** `tradingagents/graph/conditional_logic.py:should_continue_risk_analysis`
  branches on `state["risk_debate_state"]["latest_speaker"].startswith("Aggressive")`
  to decide whether to send control to `"Conservative Analyst"` next — a
  part's own output field names the *next part* to run. `tradingagents/graph/setup.py`
  wires the whole topology by literal node-name strings (`"Bull Researcher"`,
  `"Trader"`, `"Aggressive Analyst"`...). This is exactly the T-2 violation
  the blueprint forbids: a feature (here, a debate node) directly determines
  which other feature runs next, rather than a governor switching parts
  through a control plane keyed on data.

- **Position sizing is a free-text LLM opinion, not a computed number.**
  `TraderProposal.position_sizing` and `PortfolioDecision.executive_summary`
  in `tradingagents/agents/schemas.py:147-149,203-206` are typed as
  `str | None` with descriptions like `"e.g. '5% of portfolio'"` — there is
  no sizer that turns risk-limit, stop distance, and account balance into an
  actual sized order; the "size" is whatever prose the model wrote.

## Not useful here

Almost the entire data and execution layer does not transfer to an intraday
crypto spot/futures/options bot. Market data comes from `yfinance` daily
bars only (`tradingagents/dataflows/y_finance.py`,
`tradingagents/dataflows/stockstats_utils.py:load_ohlcv` caches "5 years to today" and refreshes on
a 900-second TTL at best — no tick/orderbook/websocket feed anywhere).
Crypto "support" (`asset_type="crypto"` in `tradingagents/agents/utils/agent_states.py` and
`tradingagents/agents/utils/agent_utils.py:build_instrument_context`) is a prompt-label change only
("treat it as a crypto asset... do not assume company fundamentals") over
the same daily-yfinance pipeline — `BTCUSD` resolves to `BTC-USD` and still
gets one bar a day. There is no order execution, no fill/slippage
simulation, no leverage, no options surface, no funding rate, and no
numeric risk gate anywhere in the repo — `requirements.txt`/`pyproject.toml`
list `backtrader` as a dependency but nothing in `tradingagents/` imports or
calls it (unused/vestigial). Skipped directories: `cli/` (Rich-based
terminal UI, no trading logic), `tests/` (agent-wiring and schema tests, no
market mechanism), `scripts/` (one-off utilities). The entire framework
produces one narrative rating per ticker per date — useful as a design
reference for LLM-debate structuring and reflection/memory bookkeeping, not
as a source for execution, sizing, or risk mechanics.
