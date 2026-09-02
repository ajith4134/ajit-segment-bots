#!/usr/bin/env python3
"""Declare the stock-market-news-data block, its 29 parts, its 20 data types.

Given by the user 2026-09-02, designed in
docs/superpowers/specs/2026-09-02-stock-market-news-data-design.md,
described in docs/proposals/stock-market-news-data.md.

One global block (D-N1), every item tagged with the segments it belongs to.
Three separate channels (D-N4): hard facts that the executor refuses on,
scheduled events, and a learned news signal. Reuses `sentiment-reading`
(declared since the crypto era with zero producers) and `market-event`.
Retires `exchange-announcement-reader`, `market-event-reader` and the
`venue-announcement` type, all three crypto-venue-shaped.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-02"
SPEC = "docs/superpowers/specs/2026-09-02-stock-market-news-data-design.md"
PROPOSAL = "docs/proposals/stock-market-news-data.md"

BLOCK = "stock-market-news-data"

d = json.loads(REG.read_text())


# ---------------------------------------------------------------- data types

NEW_TYPES = [
    ("raw-news-item", "raw news item",
     "One item exactly as its source delivered it: source id, url, title, body, "
     "published_at_ns as the source stamped it, observed_at_ns as this system "
     "first saw it. Both times, always -- replaying on publish time alone hands "
     "a backtest information the live bot did not have."),
    ("distinct-news-item", "distinct news item",
     "One story, with the same story from every other outlet collapsed into it. "
     "Carries the earliest publish time seen, which is the tradable one."),
    ("structured-news-item", "structured news item",
     "One news item read into its schema-checked fields by the LLM foundation: "
     "what happened, to whom, the figures stated, the claim's tense."),
    ("news-symbol-tagging", "news symbol tagging",
     "Which instruments a news item names, resolved against the broker's own "
     "instrument listing rather than a hardcoded alias table."),
    ("news-category-tagging", "news category tagging",
     "What kind of news an item is: results, corporate action, regulatory, "
     "macro, halt, analyst, rumour or block deal. Routing depends on it."),
    ("news-credibility-rating", "news credibility rating",
     "How far a source has earned trust, learned from whether its items were "
     "later confirmed by an official filing."),
    ("news-novelty-rating", "news novelty rating",
     "Whether an item is new information or the same story rewritten and "
     "already priced. Distinct from deduplication, which removes exact copies."),
    ("news-surprise-reading", "news surprise reading",
     "How far an outcome landed from what was scheduled or expected. A results "
     "print on its calendar date is not news; a beat is."),
    ("news-item", "news item",
     "The published news fact: the structured item, the instruments it names, "
     "the segments it belongs to, its category, its source's credibility. What "
     "every consumer outside the news block reads."),
    ("news-impact-forecast", "news impact forecast",
     "For one item on one symbol: direction, how far price is expected to move, "
     "over what horizon, with what confidence. Learned from realised reaction."),
    ("news-reaction-label", "news reaction label",
     "What price actually did after a news item, per horizon. The training "
     "label the sentiment model plus the impact forecaster learn from."),
    ("news-latency-reading", "news latency reading",
     "Observed time minus published time for one item. Says whether this system "
     "is reacting to news or chasing it, and discounts a late item's forecast."),
    ("instrument-restriction-report", "instrument restriction report",
     "One source's statement that an instrument is restricted: F&O ban, ASM, "
     "GSM, circuit or halt, with the window it claims."),
    ("instrument-restriction", "instrument restriction",
     "Whether an instrument may be traded right now, merged from every source "
     "that reported one, each claim expiring on its own age bound. A level."),
    ("corporate-action-report", "corporate action report",
     "A corporate action as the exchange published it: split, bonus, dividend, "
     "rights or merger, with its ratio plus its ex and record dates."),
    ("corporate-action", "corporate action",
     "The price adjustment factor a corporate action implies, with the date it "
     "takes effect. Unapplied, a 1:1 bonus is a -50% candle every detector "
     "fires on."),
    ("market-session-state", "market session state",
     "Which trading session the market is in right now: open, closed, holiday, "
     "expiry day, special session. A level."),
    ("news-source-standing", "news source standing",
     "Whether each news source is still delivering, when it last did, how stale "
     "it is. A dead feed reads exactly like a quiet news day unless measured."),
    ("news-search-request", "news search request",
     "A question asked of the open web: why did this symbol move when no known "
     "item explains it."),
    ("historical-news-window", "historical news window",
     "Taped news over a replay window, each item carrying the time this system "
     "observed it so a replay cannot read tomorrow's paper today."),
]

types_by_id = {t["id"]: t for t in d["data_types"]}
for type_id, name, description in NEW_TYPES:
    entry = {"id": type_id, "name": name, "description": description}
    if type_id in types_by_id:
        types_by_id[type_id].update(entry)
    else:
        d["data_types"].append(entry)
        types_by_id[type_id] = entry


# ------------------------------------------------------------------- helpers

feats = {f["id"]: f for f in d["features"]}


def declare(part_id, name, role, consumes, produces, category,
            resource_class, rate_risk, skipped_tick_effect):
    part = {
        "id": part_id, "name": name, "role": role, "category": category,
        "consumes": consumes, "produces": produces,
        "switchable": True, "off_releases_resources": True,
        "states": ["off", "on"], "origin": "user", "proposed": TODAY,
        "evidence": SPEC,
        "resource_class": resource_class, "rate_risk": rate_risk,
        "skipped_tick_effect": skipped_tick_effect,
    }
    if part_id in feats:
        feats[part_id].update(part)
    else:
        d["features"].append(part)
        feats[part_id] = part


def retire(part_id):
    if part_id in feats:
        d["features"] = [f for f in d["features"] if f["id"] != part_id]
        del feats[part_id]


def rewire(part_id, add=(), drop=()):
    part = feats[part_id]
    reads = [t for t in part["consumes"] if t not in drop]
    for type_id in add:
        if type_id not in reads:
            reads.append(type_id)
    part["consumes"] = sorted(reads)
    record = {"on": TODAY, "why": PROPOSAL}
    history = part.setdefault("rewired", [])
    if record not in history:
        history.append(record)


# ---------------------------------------------------------- the block itself

block = {
    "id": BLOCK,
    "name": "Stock market news data",
    "summary": (
        "Everything the Indian market says about itself, divided by segment. "
        "One global block: each source is read once, each item is tagged with "
        "the segments it belongs to (index options, stock options, index "
        "futures, stock futures, cash equity, commodities), each segment bot "
        "filters its own tag. Three channels that never share a wire -- hard "
        "facts the executor refuses on (ban, halt, corporate action, session), "
        "scheduled events, plus a learned news signal."
    ),
    "origin": "user",
    "approved": True,
    "given": TODAY,
    "evidence": SPEC,
}
existing_block = next((c for c in d["categories"] if c["id"] == BLOCK), None)
if existing_block is None:
    d["categories"].append(block)
else:
    existing_block.update(block)


# ---------------------------------------------------------- sources (11)

declare("exchange-filing-reader", "Exchange filing reader",
        "read NSE plus BSE corporate filings as they publish",
        [], ["raw-news-item", "part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")

declare("corporate-action-reader", "Corporate action reader",
        "read the exchange's own corporate action file",
        [], ["corporate-action-report", "part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")

declare("results-calendar-reader", "Results calendar reader",
        "read board meeting plus results dates before they arrive",
        [], ["market-event", "part-health"],
        BLOCK, "io-bound", "latency-only", "delays")

declare("trading-restriction-reader", "Trading restriction reader",
        "read the exchange's F&O ban, ASM, GSM, halt lists",
        [], ["instrument-restriction-report", "part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")

declare("regulator-circular-reader", "Regulator circular reader",
        "read SEBI, RBI, exchange circulars as they publish",
        [], ["raw-news-item", "part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")

declare("macro-event-calendar-reader", "Macro event calendar reader",
        "read the scheduled macro calendar that moves the index",
        [], ["market-event", "part-health"],
        BLOCK, "io-bound", "latency-only", "delays")

declare("financial-press-feed-reader", "Financial press feed reader",
        "read the financial press feeds",
        [], ["raw-news-item", "part-health"],
        BLOCK, "bandwidth-bound", "latency-only", "delays")

declare("social-chatter-reader", "Social chatter reader",
        "read public retail chatter about a symbol",
        [], ["raw-news-item", "part-health"],
        BLOCK, "bandwidth-bound", "latency-only", "delays")

declare("broker-news-reader", "Broker news reader",
        "read whatever news a broker's own API carries",
        ["broker-instrument-listing"], ["raw-news-item", "part-health"],
        BLOCK, "io-bound", "latency-only", "delays")

declare("web-news-searcher", "Web news searcher",
        "search the open web for the answer to a news question",
        ["news-search-request", "news-source-standing"],
        ["raw-news-item", "part-health"],
        BLOCK, "bandwidth-bound", "latency-only", "delays")

declare("unexplained-move-investigator", "Unexplained move investigator",
        "ask why a price move has no known cause",
        ["symbol-price-frame", "news-item"],
        ["news-search-request", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")


# ------------------------------------------------- identify plus normalise (7)

declare("news-item-deduplicator", "News item deduplicator",
        "collapse one story arriving from many outlets",
        ["raw-news-item"], ["distinct-news-item", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("news-text-structurer", "News text structurer",
        "read one item's text into its schema-checked fields",
        ["distinct-news-item", "validated-llm-output"],
        ["structured-news-item", "llm-request", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-symbol-resolver", "News symbol resolver",
        "resolve the instruments a news item names",
        ["structured-news-item", "broker-instrument-listing"],
        ["news-symbol-tagging", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("news-category-classifier", "News category classifier",
        "classify what kind of news an item is",
        ["structured-news-item"], ["news-category-tagging", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("news-credibility-scorer", "News credibility scorer",
        "score how far a source has earned trust",
        ["structured-news-item", "news-item"],
        ["news-credibility-rating", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-segment-classifier", "News segment classifier",
        "tag which segments a news item belongs to",
        ["structured-news-item", "news-symbol-tagging",
         "news-category-tagging", "news-credibility-rating"],
        ["news-item", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("news-tape-writer", "News tape writer",
        "append every news item to the tape",
        ["raw-news-item", "news-item"], ["part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")


# ------------------------------------------------------ judgement, learned (5)

declare("news-sentiment-model", "News sentiment model",
        "score which way a news item points for its symbol",
        ["news-item", "news-reaction-label"],
        ["sentiment-reading", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-surprise-scorer", "News surprise scorer",
        "score an outcome against what was scheduled",
        ["news-item", "market-event"],
        ["news-surprise-reading", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-novelty-scorer", "News novelty scorer",
        "score whether an item carries new information",
        ["news-item", "historical-news-window"],
        ["news-novelty-rating", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-impact-forecaster", "News impact forecaster",
        "forecast how far price moves on a news item",
        ["news-item", "sentiment-reading", "news-surprise-reading",
         "news-novelty-rating", "news-credibility-rating",
         "news-latency-reading", "news-reaction-label"],
        ["news-impact-forecast", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("news-reaction-labeller", "News reaction labeller",
        "measure what price actually did after a news item",
        ["news-item", "symbol-price-frame"],
        ["news-reaction-label", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")


# ---------------------------------------------------------- hard channel (3)

declare("instrument-restriction-state", "Instrument restriction state",
        "state whether an instrument may be traded right now",
        ["instrument-restriction-report"],
        ["instrument-restriction", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("corporate-action-adjuster", "Corporate action adjuster",
        "state the price adjustment a corporate action implies",
        ["corporate-action-report", "broker-instrument-listing"],
        ["corporate-action", "part-health"],
        BLOCK, "compute-bound", "changes-the-answer", "corrupts")

declare("market-session-calendar", "Market session calendar",
        "state which trading session the market is in right now",
        [], ["market-session-state", "part-health"],
        BLOCK, "io-bound", "changes-the-answer", "corrupts")


# ------------------------------------------------------ health plus history (3)

declare("news-source-health-monitor", "News source health monitor",
        "state whether each news source is still delivering",
        ["raw-news-item", "corporate-action-report",
         "instrument-restriction-report"],
        ["news-source-standing", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-latency-meter", "News latency meter",
        "measure how late this system sees a news item",
        ["raw-news-item"], ["news-latency-reading", "part-health"],
        BLOCK, "compute-bound", "latency-only", "delays")

declare("news-history-reader", "News history reader",
        "serve taped news over a replay window",
        ["news-item"], ["historical-news-window", "part-health"],
        BLOCK, "io-bound", "latency-only", "delays")


# -------------------------------------------- the one new part outside the block

declare("news-catalyst-detector", "News catalyst detector",
        "raise a candidate whose news impact clears the bar",
        ["news-impact-forecast", "news-item", "liquidity-grade"],
        ["entry-candidate", "part-health"],
        "opportunity-scanner", "compute-bound", "changes-the-answer", "corrupts")


# --------------------------------------------------------- retire the crypto era

retire("exchange-announcement-reader")   # crypto venue listings/delistings
retire("market-event-reader")            # only re-read venue-announcement
d["data_types"] = [t for t in d["data_types"] if t["id"] != "venue-announcement"]


# ------------------------------------------------------------------ the wiring

rewire("universal-symbol-sweeper",
       add=["news-impact-forecast", "market-session-state", "news-item"],
       drop=["venue-announcement"])
rewire("liquidity-grader", add=["instrument-restriction"])
rewire("expiry-day-zero-to-hero-detector", add=["market-session-state"])

rewire("bull-feature-builder", add=["sentiment-reading", "news-impact-forecast"])
rewire("bull-position-invalidation-watcher", add=["news-impact-forecast"])
rewire("bear-feature-builder", add=["sentiment-reading", "news-impact-forecast"])
rewire("bear-position-invalidation-watcher", add=["news-impact-forecast"])
rewire("tail-mover-qualifier", add=["news-impact-forecast"])

rewire("intent-timing-gate", add=["market-session-state", "market-event"])
rewire("premortem-writer", add=["market-event"])
rewire("market-thesis-reasoner", add=["news-item"])

rewire("kline-window-builder", add=["corporate-action"])

rewire("event-risk-limiter", add=["news-impact-forecast"],
       drop=["venue-announcement"])
rewire("halt-enforcer", add=["instrument-restriction"])

rewire("order-destination-router",
       add=["instrument-restriction", "market-session-state"])
rewire("paper-fill-simulator", add=["market-session-state"])

rewire("ccxt-order-router", add=["instrument-restriction", "market-session-state"])
rewire("order-reject-classifier", add=["instrument-restriction"])

rewire("fill-reconciler", add=["corporate-action"])
rewire("cost-basis-tracker", add=["corporate-action"])

rewire("learning-recorder", add=["news-item", "news-impact-forecast"])

rewire("loss-cause-classifier", add=["market-event", "news-impact-forecast"])
rewire("trade-narrative-writer", add=["news-item"])

rewire("signal-outcome-labeller", add=["news-impact-forecast"])

rewire("instruction-writer", add=["news-impact-forecast"])
rewire("loss-inverter", add=["news-item"])

rewire("symbol-profile-store", add=["news-reaction-label"])

rewire("market-anomaly-detector", add=["news-item"])

rewire("trading-halt-decider",
       add=["market-session-state", "instrument-restriction"])

rewire("instruction-replayer", add=["historical-news-window"])

rewire("alert-raiser", add=["news-source-standing"])


# ------------------------------------------- recompute every touched block's contract

touched = {BLOCK} | {f["category"] for f in d["features"]
                     if f.get("rewired") and f["rewired"][-1]["on"] == TODAY}
touched |= {"opportunity-scanner", "online-research", "intelligence"}

for category in d["categories"]:
    if category["id"] not in touched:
        continue
    parts = [f for f in d["features"] if f["category"] == category["id"]]
    category["consumes"] = sorted({t for p in parts for t in p["consumes"]})
    category["produces"] = sorted({t for p in parts for t in p["produces"]})
    category["contract_recomputed"] = {
        "on": TODAY, "from": "its parts", "origin": "user",
    }

d[f"_proposal_{TODAY}_stock_market_news_data"] = {
    "what": (
        "stock-market-news-data -- a foundational block beside market-data-feed "
        "holding the news the Indian market runs on, divided by segment. 29 "
        "parts, 20 new data types, 3 channels: hard facts the executor refuses "
        "on (F&O ban, halt, corporate action, session state), scheduled events, "
        "plus a learned news signal every segment bot weighs its own way."
    ),
    "why": (
        "The blueprint carried no news at all: market-event had no real "
        "producer, exchange-announcement-reader read crypto venue listings, and "
        "sentiment-reading was declared with zero producers and zero consumers. "
        "For Indian markets three of these are not sentiment but conditions the "
        "executor cannot trade through -- a banned name rejects every order, an "
        "unadjusted 1:1 bonus is a -50% candle every detector fires on, and a "
        "paper fill on a holiday journals a trade that could not happen."
    ),
    "decisions": {
        "D-N1": "one global block, segment-tagged output, not six copies",
        "D-N2": "official, press, social, broker, plus an autonomous web searcher",
        "D-N3": "the block judges as well as delivers (RL-060); the LLM reads a "
                "headline once here rather than six times in six bots",
        "D-N4": "three channels never share a wire; a halt is never a sentiment score",
    },
    "reused": ["sentiment-reading (declared, zero producers since the crypto era)",
               "market-event (already consumed by regime-break-detector plus "
               "event-risk-limiter)"],
    "retired": ["exchange-announcement-reader", "market-event-reader",
                "type venue-announcement"],
    "given": TODAY,
    "origin": "user",
    "evidence": SPEC,
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2) + "\n")
print(f"{len(d['categories'])} categories, {len(d['features'])} features, "
      f"{len(d['data_types'])} data types")
