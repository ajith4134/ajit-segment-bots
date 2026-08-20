#!/usr/bin/env python3
"""Apply the 2026-08-20 repo harvest and the three-bots ruling to docs/features.json.

Two changes, one pass, so the contract checker sees the end state:
  1. Parts and data types that fifteen open-source repos proved useful
     (docs/research/repo-harvest/*.md), after deduplication and judgment.
  2. RL-046..048: bull-bot, bear-bot and profit-tailgating-bot opened into blocks
     (docs/proposals/three-bots-inside-segment.md).

Idempotent: re-running changes nothing once applied.
"""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
d = json.loads(REG.read_text())
TODAY = "2026-08-20"
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}
cats = {c["id"]: c for c in d["categories"]}

def add_type(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc}; d["data_types"].append(t); types[tid] = t

def add_part(pid, name, role, cat, consumes, produces, evidence, origin="proposed"):
    produces = list(produces) + (["part-health"] if "part-health" not in produces else [])
    f = {"id": pid, "name": name, "role": role, "category": cat, "consumes": list(consumes),
         "produces": produces, "switchable": True, "off_releases_resources": True,
         "states": ["off", "on"], "origin": origin, "evidence": evidence}
    if pid in feats: feats[pid].update(f)
    else: d["features"].append(f); feats[pid] = f

def consume_also(pid, *tids):
    for t in tids:
        if t not in feats[pid]["consumes"]: feats[pid]["consumes"].append(t)

def set_role(pid, role, note):
    feats[pid]["role"] = role; feats[pid]["role_refined"] = {"on": TODAY, "from": note}

def retire(pid):
    if pid in feats:
        d["features"].remove(feats.pop(pid))

# ---------------------------------------------------------------- 1. harvest
H = "docs/research/repo-harvest/"
add_type("fill-price-estimate", "fill price estimate", "The weighted-average price an order of this size would fill at, from walking live book depth, with whether it cleared fully.")
add_type("delayed-order-request", "delayed order request", "An order request released only after a simulated venue round-trip, so a paper fill can never be faster than a real one.")
add_type("liquidation-price", "liquidation price", "The price at which this bot's own leveraged position is force-closed by the venue, from entry, leverage and maintenance margin.")
add_type("locked-allocation", "locked allocation", "Capital earmarked against one sent order, owned but unavailable to a second order until the first fills or is cancelled.")
add_type("price-increment", "price increment", "The smallest price step the venue accepts for a symbol, declared or inferred from live bid/ask spacing.")
add_type("raw-venue-order-status", "raw venue order status", "One venue's native order status payload, before translation into the system's closed status set.")
add_type("stamped-order", "stamped order", "A sized order carrying a client-generated idempotency id, so a resubmit reuses it and the venue deduplicates.")
add_type("cancel-decision", "cancel decision", "A still-open order that should be pulled: past its time-to-live, or the market has drifted too far from it.")
add_type("order-reprice", "order reprice", "A bounded step of an unfilled limit order's price toward the market, on a fixed cadence.")
add_type("cost-basis", "cost basis", "The average price a position was actually built at, updated on every fill, zeroed when flat.")
add_type("funding-settlement", "funding settlement", "One perpetual funding payment, settled as an idempotent event keyed on its settlement boundary.")
add_type("drawdown-episode", "drawdown episode", "One drawdown from peak through recovery or still open: depth, how long it has lasted, how long recovery took.")
add_type("fill-sequence", "fill sequence", "The order in which pending orders would have filled inside one simulated bar, with the bar split so later checks see the narrowed range.")
add_type("fillable-size", "fillable size", "The most a simulated fill may take from one bar, from a participation cap against real traded volume.")
add_type("feed-jump", "feed jump", "A candle whose open does not match the prior close: present but discontinuous data, so a stop in the gap still counts as crossed.")
add_type("forecast-out-of-distribution-flag", "forecast out-of-distribution flag", "A forecast made from a feature vector far from anything the model was trained on, so it should be discounted regardless of past accuracy.")
add_type("turbulence-index", "turbulence index", "One market-wide number: how far today's cross-symbol return vector sits from its trailing covariance — is everything moving together abnormally.")
add_type("verified-snapshot", "verified snapshot", "A deterministic, non-LLM rendering of the latest bars, a fixed indicator set and recent closes, that an LLM-backed part is told to defer to.")
add_type("execution-schedule", "execution schedule", "A sized order split into timed slices, front-loaded when forecast volatility is high, each slice capped by participation.")

# paper-live-trading
add_part("book-walk-fill-pricer", "Book-walk fill pricer", "compute the price an order of this size would really fill at by walking live book depth", "paper-live-trading",
         ["order-request", "order-book-snapshot"], ["fill-price-estimate"], H+"hummingbot.md OrderBook.simulate_buy; polymarket-py-sdk.md _calculate_buy_market_price")
add_part("order-latency-simulator", "Order latency simulator", "hold a paper order for a simulated venue round-trip before it may fill", "paper-live-trading",
         ["order-request", "money-mode"], ["delayed-order-request"], H+"nautilus_trader.md crates/execution/src/models/latency.rs")
add_part("paper-liquidation-simulator", "Paper liquidation simulator", "close a paper position at its bankruptcy price when the live range crosses its liquidation price", "paper-live-trading",
         ["position", "market-data", "liquidation-price", "money-mode"], ["fill"], H+"jesse.md _check_for_liquidations; nautilus_trader.md process_liquidations")
add_part("order-idempotency-stamper", "Order idempotency stamper", "stamp every sized order with a stable client id once, before it can ever be retried", "paper-live-trading",
         ["sized-order"], ["stamped-order"], H+"ccxt.md binance.py newClientOrderId")
consume_also("paper-fill-simulator", "delayed-order-request", "fill-price-estimate", "feed-jump")
consume_also("order-destination-router", "stamped-order", "execution-schedule")

# portfolio-state
add_part("liquidation-price-tracker", "Liquidation price tracker", "compute the price at which this bot's own leveraged position gets force-closed", "portfolio-state",
         ["position", "leverage-choice", "market-data"], ["liquidation-price"], H+"octobot.md LinearPosition.update_isolated_liquidation_price; jesse.md Position.liquidation_price")
add_part("fund-lock-ledger", "Fund lock ledger", "reserve capital against an order the instant it is sent, so two orders in one tick never spend the same balance", "portfolio-state",
         ["sized-order", "fill", "account-balance"], ["locked-allocation"], H+"tensortrade.md Wallet.lock; hummingbot.md BudgetChecker.adjust_candidates")
add_part("cost-basis-tracker", "Cost basis tracker", "keep the average price each position was actually built at", "portfolio-state",
         ["fill"], ["cost-basis"], H+"FinRL.md avg_buy_price incremental update")
consume_also("peak-excursion-tracker", "cost-basis"); consume_also("usdt-pnl-accountant", "cost-basis", "funding-settlement")
set_role("position-close-detector", "emit a closed trade when a position goes flat, matching closing fills against the oldest open lots so partial closes resolve correctly, with its peak excursion attached",
         H+"vectorbt.md get_exit_trades_nb FIFO lot matching")

# risk-capital-allocation
add_part("margin-liquidation-watch", "Margin liquidation watch", "zero the limit when balance plus unrealised PnL nears the maintenance margin on a leveraged segment", "risk-capital-allocation",
         ["position", "account-balance", "liquidation-price", "market-data"], ["risk-limit"], H+"nautilus_trader.md exchange.rs process_liquidations")
add_part("stop-frequency-breaker", "Stop frequency breaker", "cut the limit when stop-loss exits in a rolling window cross a threshold, before equity drawdown shows it", "risk-capital-allocation",
         ["closed-trade"], ["risk-limit"], H+"freqtrade.md plugins/protections/stoploss_guard.py")
add_part("exit-order-chainer", "Exit order chainer", "emit the paired stop plus target the moment an entry fill lands, without waiting for anything to notice the position", "risk-capital-allocation",
         ["stop-target-plan", "fill"], ["stop-adjustment"], H+"tensortrade.md risk_managed_order OrderSpec attach")
add_part("participation-capped-order-splitter", "Participation-capped order splitter", "split a sized order into timed slices capped against recent traded volume", "risk-capital-allocation",
         ["sized-order", "volatility-forecast", "order-book-snapshot"], ["execution-schedule"], H+"qlib.md ACStrategy.generate_trade_decision; Exchange._clip_amount_by_volume")
consume_also("position-sizer", "locked-allocation", "price-increment"); consume_also("drawdown-breaker", "drawdown-episode"); consume_also("event-risk-limiter", "turbulence-index")
set_role("position-sizer", "size one trade from intent, instrument, leverage, stop plus the smallest limit, iterating until fees fit, snapped to the price increment, shrunk to the largest affordable size rather than refused outright",
         H+"lean.md GetAmountToOrder; vectorbt.md buy_nb downsize")

# execution-venue-adapter
add_part("venue-order-status-translator", "Venue order status translator", "collapse one venue's native order status vocabulary into the system's closed status set", "execution-venue-adapter",
         ["raw-venue-order-status"], ["fill", "order-reject-reason"], H+"lumibot.md order.py STATUS_ALIAS_MAP; hummingbot.md order-state machine")
add_part("order-not-found-debouncer", "Order-not-found debouncer", "count consecutive order-not-found replies per order, declaring it lost only past a threshold", "execution-venue-adapter",
         ["raw-venue-order-status"], ["order-reject-reason"], H+"hummingbot.md ClientOrderTracker.process_order_not_found")
add_part("resting-order-cancel-policy", "Resting order cancel policy", "decide when a still-open order is stale enough to pull: past its time-to-live or too far from the market", "execution-venue-adapter",
         ["order-request", "market-data"], ["cancel-decision"], H+"octobot.md orders/cancel_policies/")
add_part("limit-price-walker", "Limit price walker", "step an unfilled limit order's price toward the market by a bounded amount on a fixed cadence", "execution-venue-adapter",
         ["order-request", "market-data", "order-book-snapshot"], ["order-reprice"], H+"lumibot.md smart_limit_utils.build_price_ladder")
feats["order-state-poller"]["produces"] = ["raw-venue-order-status", "part-health"]
feats["ccxt-order-router"]["produces"] = ["raw-venue-order-status", "part-health"]
consume_also("ccxt-order-router", "cancel-decision", "order-reprice")
feats["order-reject-classifier"]["consumes"] = ["raw-venue-order-status"]
set_role("venue-rate-budgeter", "count what the venue still allows this window, as a separate weighted budget per endpoint class", H+"hummingbot.md RATE_LIMITS per-endpoint weights; ccxt.md leaky bucket")

# market-data-feed
add_part("tick-size-resolver", "Tick size resolver", "resolve the valid price increment per symbol, declared by the venue or inferred from live bid/ask spacing", "market-data-feed",
         ["order-book-snapshot", "symbol-universe"], ["price-increment"], H+"lumibot.md infer_tick_size; lean.md RoundOrderPrices")
add_part("feed-jump-detector", "Feed jump detector", "flag a candle whose open does not meet the prior close, so a stop in the gap still counts as crossed", "market-data-feed",
         ["market-data"], ["feed-jump"], H+"jesse.md _get_fixed_jumped_candle")
set_role("liquidity-grader", "grade how tradeable each symbol is at the bot's size, refreshed on a fixed interval rather than every tick", H+"freqtrade.md VolumePairList FtTTLCache")

# ledger
add_part("funding-settlement-recorder", "Funding settlement recorder", "book each perpetual funding payment as its own idempotent event, separate from fill PnL", "ledger",
         ["position", "market-data"], ["funding-settlement", "journal-entry"], H+"nautilus_trader.md settle_funding_rate; backtrader.md CommInfoBase.get_credit_interest")

# observability
add_part("drawdown-episode-tracker", "Drawdown episode tracker", "track each equity drawdown from peak through recovery as its own record: depth, duration so far, recovery time", "observability",
         ["account-balance"], ["drawdown-episode"], H+"vectorbt.md generic/drawdowns.py; backtrader.md DrawDown.next streak")
add_part("fund-conservation-auditor", "Fund conservation auditor", "recompute both sides of every fill's balance equation independently, alerting the step they disagree", "observability",
         ["fill", "journal-entry"], ["alert"], H+"tensortrade.md Wallet.transfer lhs/rhs assertion")
add_part("clock-skew-monitor", "Clock skew monitor", "raise an alert when timestamp-drift rejections recur, before every private order starts failing", "observability",
         ["raw-venue-order-status"], ["alert"], H+"ccxt.md binance -1021 InvalidNonce")

# backtesting
add_part("intra-bar-fill-sequencer", "Intra-bar fill sequencer", "decide which of several pending orders filled first inside one simulated bar, splitting the bar for the rest", "backtesting",
         ["historical-window", "cost-estimate"], ["fill-sequence"], H+"jesse.md _sort_execution_orders")
add_part("fill-volume-capper", "Fill volume capper", "cap a simulated fill against the bar's real traded volume so replay never assumes infinite liquidity", "backtesting",
         ["historical-window"], ["fillable-size"], H+"backtrader.md fillers.FixedBarPerc")
consume_also("instruction-replayer", "fill-sequence", "fillable-size")

# prediction / intelligence / llm-services
add_part("forecast-distribution-gate", "Forecast distribution gate", "flag a forecast whose input features sit far from anything the model trained on", "prediction",
         ["kline-window", "finetuned-model", "price-forecast"], ["forecast-out-of-distribution-flag"], H+"freqtrade.md freqai dissimilarity index (DI) pipeline")
consume_also("forecast-ensembler", "forecast-out-of-distribution-flag")
add_part("turbulence-index-gauge", "Turbulence index gauge", "measure how abnormally every symbol is moving together, as one market-wide distance number", "intelligence",
         ["market-data"], ["turbulence-index"], H+"FinRL.md FeatureEngineer.calculate_turbulence (Mahalanobis)")
add_part("ground-truth-snapshot-builder", "Ground-truth snapshot builder", "render the latest bars plus a fixed indicator set deterministically, so an LLM part is told numbers instead of inventing them", "llm-services",
         ["market-data"], ["verified-snapshot"], H+"TradingAgents.md dataflows/market_data_validator.py")
consume_also("intent-explainer", "verified-snapshot"); consume_also("decision-quality-critic", "verified-snapshot")
set_role("slippage-learner", "measure each fill against a TWAP baseline over its own execution window, signed by direction, in basis points, by symbol, size, hour", H+"qlib.md price_advantage")

# ------------------------------------------------------- 2. three bots (RL-046..048)
P = "docs/proposals/three-bots-inside-segment.md"
types["entry-candidate"]["description"] = "A symbol, the moment it should enter trading, which watch condition fired and its reading. Never a side. Carries nothing about paper or live."
add_type("setup-weight", "setup weight", "How much one bot trusts each setup class on each time-frame, learned from its own scorecard, applied inside the bot. The scanner never sees it (RL-046).")
add_type("side-candidate", "side candidate", "An entry candidate one side's bot has kept, with the setup weight it carried.")
add_type("feature-vector", "feature vector", "One bot's own features for one candidate: the numbers its model reads.")
add_type("raw-conviction", "raw conviction", "A learned model's probability that the move goes the bot's way by more than cost within the horizon, before calibration.")
add_type("calibrated-conviction", "calibrated conviction", "A conviction mapped onto the bot's own measured hit-rate, so 0.7 means 70% for this bot.")
add_type("entry-timing", "entry timing", "Now, or wait for the pullback the playbook says comes, with a give-up time.")
add_type("exit-plan", "exit plan", "A bot's proposed target, stop and time-stop for its own candidate. Proposed; the risk gate decides.")
add_type("follow-candidate", "follow candidate", "Something already working that the tailgater may join: a continuation setup, our own winning leg, or a tracked trader's position.")
add_type("move-remaining", "move remaining", "How much of a move is likely left against how much has already gone.")
add_type("crowding-reading", "crowding reading", "How crowded the move already is: funding stretch, book one-sidedness, sentiment extreme.")

for side, name, long in (("bull", "Bull bot", "long"), ("bear", "Bear bot", "short")):
    cid = f"{side}-bot"
    if cid not in cats:
        c = {"id": cid, "name": name, "summary": f"A complete bot with its own features, model, scorecard and board cells, issuing the {long}-side opinion on every candidate the scanner raises. Same skeleton as the other side (T-1), separate at runtime (RL-048).",
             "origin": "user", "approved": True, "consumes": [], "produces": [], "flow_origin": "agreed-provisional", "scope": "per-segment", "scope_origin": "user",
             "template": "directional-bot", "rulings": ["RL-011", "RL-023", "RL-026", "RL-046", "RL-048"]}
        d["categories"].append(c); cats[cid] = c
    s = f"{side}-"
    add_part(s+"setup-filter", f"{name}: setup filter", f"keep the candidates whose setup this {long} side can trade, weighted by what it has learned", cid, ["entry-candidate", "setup-weight"], ["side-candidate"], P)
    add_part(s+"feature-builder", f"{name}: feature builder", f"compute this side's own features for a candidate: distance from support or resistance, flow imbalance, VWAP gap, funding sign, burst age, volume against the symbol's hour-norm", cid,
             ["side-candidate", "market-data", "order-book-snapshot", "symbol-profile", "funding-forecast"], ["feature-vector"], P)
    add_part(s+"outlier-rejector", f"{name}: outlier rejector", "refuse to opine when the feature vector is far from anything the model has seen", cid, ["feature-vector"], ["forecast-out-of-distribution-flag"], P+"; "+H+"freqtrade.md DI")
    add_part(s+"conviction-model", f"{name}: conviction model", f"learn the probability that a {long} move clears cost within the horizon, from the feature vector plus the price forecast", cid,
             ["feature-vector", "price-forecast", "kline-window", "forecast-out-of-distribution-flag"], ["raw-conviction"], P)
    add_part(s+"conviction-calibrator", f"{name}: conviction calibrator", "map the model's number onto this bot's measured hit-rate", cid, ["raw-conviction", "bot-scorecard"], ["calibrated-conviction"], P)
    add_part(s+"entry-timer", f"{name}: entry timer", "decide now or wait for the pullback, with a give-up time", cid, ["side-candidate", "market-data", "calibrated-conviction", "playbook-rule"], ["entry-timing"], P)
    add_part(s+"exit-plan-proposer", f"{name}: exit plan proposer", f"propose target, stop plus time-stop for a {long} candidate from ATR plus structure", cid, ["side-candidate", "market-data", "symbol-profile", "calibrated-conviction"], ["exit-plan"], P)
    add_part(s+"opinion-composer", f"{name}: opinion composer", "pack side, conviction, timing, plan plus feature snapshot into one opinion", cid, ["calibrated-conviction", "entry-timing", "exit-plan", "feature-vector"], ["directional-opinion"], P)
    add_part(s+"setup-weight-learner", f"{name}: setup weight learner", "rewrite how much this bot trusts each setup class from what it has actually been right about", cid, ["bot-scorecard", "instruction-scorecard"], ["setup-weight"], P)
    add_part(s+"position-invalidation-watcher", f"{name}: position invalidation watcher", f"say flat when the reason a {long} position was opened has gone", cid, ["position", "market-data", "feature-vector", "regime-break-alert"], ["directional-opinion"], P)

cid = "profit-tailgating-bot"
if cid not in cats:
    c = {"id": cid, "name": "Profit tailgating bot", "summary": "A complete bot that joins what is already working: continuation setups from the scanner, this segment's own winning leg of an exploration pair (RL-047), and tracked traders' positions. Never proposes a reversal; always trails, never targets.",
         "origin": "user", "approved": True, "consumes": [], "produces": [], "flow_origin": "agreed-provisional", "scope": "per-segment", "scope_origin": "user", "rulings": ["RL-011", "RL-023", "RL-026", "RL-047"]}
    d["categories"].append(c); cats[cid] = c
t = "tail-"
add_part(t+"mover-qualifier", "Tailgater: mover qualifier", "keep a continuation candidate only if the move is sustained rather than a spike already fading", cid, ["entry-candidate", "market-data", "symbol-profile", "setup-weight"], ["follow-candidate"], P)
add_part(t+"winner-selector", "Tailgater: winner selector", "pick this segment's own open leg that is in profit plus still moving", cid, ["position", "market-data", "peak-excursion"], ["follow-candidate"], P)
add_part(t+"copy-selector", "Tailgater: copy selector", "pick a tracked trader's position the copy score says to follow", cid, ["external-position", "copy-score"], ["follow-candidate"], P)
add_part(t+"move-remaining-estimator", "Tailgater: move remaining estimator", "estimate how much of the move is left against how much has already gone", cid, ["follow-candidate", "market-data", "price-forecast"], ["move-remaining"], P)
add_part(t+"crowding-detector", "Tailgater: crowding detector", "read whether everyone is already in, so the tail would be the exit liquidity", cid, ["follow-candidate", "order-book-snapshot", "funding-forecast", "sentiment-reading"], ["crowding-reading"], P)
add_part(t+"follow-conviction-model", "Tailgater: follow conviction model", "learn the probability that continuation clears cost, from remaining move plus crowding, calibrated to this bot's record", cid, ["follow-candidate", "move-remaining", "crowding-reading", "bot-scorecard"], ["calibrated-conviction"], P)
add_part(t+"trailing-exit-planner", "Tailgater: trailing exit planner", "trail a stop below the last higher low, tightening as the move ages; never a fixed target", cid, ["follow-candidate", "market-data", "symbol-profile"], ["exit-plan"], P)
add_part(t+"opinion-composer", "Tailgater: opinion composer", "pack direction, conviction plus trailing plan into one opinion", cid, ["calibrated-conviction", "exit-plan", "follow-candidate"], ["directional-opinion"], P)
add_part(t+"setup-weight-learner", "Tailgater: setup weight learner", "rewrite how much this bot trusts each continuation class from what it has been right about", cid, ["bot-scorecard"], ["setup-weight"], P)

for old in ("bull-bot", "bear-bot", "profit-tailgater", "exit-opinion-bot"): retire(old)
consume_also("stop-target-placer", "exit-plan")
cats["segment-bot"]["summary"] = "The container. Inside it: the universal opportunity scanner, the AI brain, and three complete bots — bull, bear and profit tailgating — each its own block (RL-048). The bots issue opinions; the brain turns three opinions into one intent. The scanner raises every candidate to every bot and never learns what they prefer (RL-046)."

# ------------------------------------------------------- 3. recompute block contracts
for c in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == c["id"]]
    if not parts: continue
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-08-20_harvest"] = {
    "what": "Parts fifteen open-source repos proved useful, merged after deduplication; plus RL-046..048 opening bull, bear and profit tailgating into blocks.",
    "origin": "harvest parts proposed by Claude; three-bots blocks are user rulings RL-046..048",
    "applied_by": "dashboard/blueprint_edits/apply_2026-08-20_harvest_and_three_bots.py",
    "skipped_from_harvest": {
        "inventory-skew-spread-adjuster, quote-refresh-tolerance-gate": "market-making; this system takes direction, it does not quote",
        "onchain-allowance-checker": "on-chain venues only; every venue here is a CEX through ccxt",
        "symbol-alias-resolver": "ccxt's unified symbols already do this",
        "turnover-cost-solver": "portfolio re-weighting; this system sizes one trade at a time",
        "decision-outcome-resolver": "our closed trades already carry their realised price",
        "exit-criteria-compiler": "covered by exit-plan plus stop-target-plan",
        "expectancy-component-ledger": "expectancy-decomposer already exists in hypothesis",
        "round-trip-trade-matcher, fee-aware-size-solver, fill-cost-downsizer, liquidity-cache-refresher, endpoint-weight-budgeter, fill-price-advantage-scorer": "folded into an existing part's role (role_refined), not a new part",
        "carry-cost-accountant, batch-collateral-locker, order-book-walk-pricer, liquidation-simulator, margin variants": "merged with their duplicates",
    },
}
REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(len(d["features"]), "features,", len(d["categories"]), "categories,", len(d["data_types"]), "data types")
