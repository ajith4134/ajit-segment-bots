#!/usr/bin/env python3
"""Declare equity-opportunity-profiler and cash-equity-shortlist-ranker.

docs/proposals/equity-opportunity-profiler.md and
docs/proposals/cash-equity-shortlist-ranker.md: the operator asked (2026-09-05)
for the top 50 cash-equity names actually worth scanning today -- momentum,
volume, 52-week range, gap, VWAP deviation, ATR-normalised move -- instead of
`broker-symbol-universe-bridge` publishing all 2,444 ordinary shares unranked
and uncapped. Two new parts and two new data types: `equity-historical-profile`
(each share's own 52-week range, average volume, average true range and prior
close, fetched from Upstox's historical-candle endpoint) and
`cash-equity-shortlist` (today's ranked top-N, a level).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}

cid = "market-data-feed"

PROFILER_PROPOSAL = "docs/proposals/equity-opportunity-profiler.md"
RANKER_PROPOSAL = "docs/proposals/cash-equity-shortlist-ranker.md"

new_features = [
    {
        "id": "equity-opportunity-profiler",
        "name": "Equity opportunity profiler",
        "role": "fetch each ordinary NSE share's own historical trading profile",
        "category": cid,
        "consumes": ["broker-instrument-listing", "broker-token-standing"],
        "produces": ["equity-historical-profile", "part-health"],
        "switchable": True, "off_releases_resources": True,
        "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
        "evidence": PROFILER_PROPOSAL,
        "resource_class": "io-bound", "rate_risk": "latency-only",
        "skipped_tick_effect": "delays",
    },
    {
        "id": "cash-equity-shortlist-ranker",
        "name": "Cash equity shortlist ranker",
        "role": "rank today's ordinary NSE shares into the top-N cash-equity shortlist",
        "category": cid,
        "consumes": [
            "broker-instrument-listing", "equity-historical-profile", "liquidity-grade",
            "broker-price-frame", "candle",
        ],
        "produces": ["cash-equity-shortlist", "part-health"],
        "switchable": True, "off_releases_resources": True,
        "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
        "evidence": RANKER_PROPOSAL,
        "resource_class": "compute-bound", "rate_risk": "changes-the-answer",
        "skipped_tick_effect": "delays",
    },
]
for f in new_features:
    if f["id"] in feats:
        feats[f["id"]].update(f)
    else:
        d["features"].append(f)
        feats[f["id"]] = f

# broker-symbol-universe-bridge now caps its equity branch to today's
# shortlist instead of publishing every excluded share unranked (see
# cash-equity-shortlist-ranker.md, "The gap") -- one more consumed type on an
# existing part, not a new part (T-6). Set explicitly, in the exact order each
# part's own PART_DECLARATION literal states -- PartDeclaration equality
# (checked by each part's own blueprint test) compares the consumes tuple in
# order, and an in-place append or sort earlier in this script's history
# scrambled the rest of one of these lists, which is why this is a full
# replacement rather than an edit of what was there.
feats["broker-symbol-universe-bridge"]["consumes"] = [
    "broker-instrument-listing", "broker-price-frame", "cash-equity-shortlist",
]
feats["instrument-selector"]["consumes"] = [
    "trade-intent", "symbol-price-frame", "implied-vol-surface", "liquidity-grade",
    "timed-intent", "symbol-universe", "symbol-quote-frame",
    "broker-subscribed-instrument-listing", "broker-option-greeks", "broker-market-data",
    "cash-equity-shortlist",
]

new_types = [
    {
        "id": "equity-historical-profile",
        "name": "equity historical profile",
        "description": (
            "One ordinary NSE share's own 52-week high, 52-week low, average daily "
            "volume, average true range and prior close, fetched from Upstox's "
            "historical-candle endpoint. upper_circuit_limit/lower_circuit_limit are "
            "named on the payload and always None until a real fetch for them exists "
            "(2026-09-05: neither the instrument master nor any live feed carries "
            "them)."
        ),
        "origin": "proposed", "proposed": TODAY,
    },
    {
        "id": "cash-equity-shortlist",
        "name": "cash equity shortlist",
        "description": (
            "Today's ranked top-N ordinary NSE shares for the cash-equity-intraday "
            "segment, blended from momentum, volume surge, 52-week range proximity, "
            "gap, VWAP deviation and ATR-normalised move within a liquidity-qualified "
            "pool. A level, restated on an interval."
        ),
        "origin": "proposed", "proposed": TODAY,
    },
]
for t in new_types:
    if t["id"] in types:
        types[t["id"]].update(t)
    else:
        d["data_types"].append(t)
        types[t["id"]] = t

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-05_cash_equity_shortlist"] = {
    "what": (
        "equity-opportunity-profiler + cash-equity-shortlist-ranker -- the operator "
        "asked 2026-09-05 for the top 50 cash-equity names actually worth scanning "
        "today (momentum, volume, 52-week high/low, gap, VWAP deviation, "
        "ATR-normalised move -- never open interest, which has no cash-equity "
        "analogue) instead of broker-symbol-universe-bridge's 2,444 unranked, "
        "uncapped ordinary shares. The profiler fetches each share's own 52-week "
        "range, average volume and average true range from Upstox's historical-"
        "candle endpoint (the same call broker-history-reader makes for a different "
        "job, reused rather than restated); the ranker blends that with live "
        "momentum, volume-so-far and VWAP deviation into a percentile-ranked "
        "shortlist within a liquidity-qualified pool "
        "(runtime/cash_equity_shortlist.py, shared with the replay tool). Circuit "
        "limits and NSE delivery percentage were investigated the same day and "
        "deferred -- the first needs a broker call this build does not make yet, "
        "the second was refused by Akamai's bot-protection against archives."
        "nseindia.com even with a warmed session, a verified block rather than a "
        "missing package."
    ),
    "origin": (
        "designed by Claude, user asked 2026-09-05 for the top-50 cash-equity "
        "shortlist instead of scanning the full universe"
    ),
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-05_cash_equity_shortlist.py",
    "proposal": [PROFILER_PROPOSAL, RANKER_PROPOSAL],
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(
    f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
    f"{len(d['data_types'])} data types"
)
