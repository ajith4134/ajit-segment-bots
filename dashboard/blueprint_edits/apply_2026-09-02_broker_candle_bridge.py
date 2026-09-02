#!/usr/bin/env python3
"""Declare broker-candle-bridge.

docs/proposals/broker-candle-bridge.md: republishes Upstox's own OHLC bars
as candle -- the crypto-era type kline-window-builder reads, unblocking
kronos-forecaster/bull-conviction-model/bear-conviction-model for the
options segment. Same gap shape as broker-market-data-bridge and
broker-order-book-bridge, closed the same way.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-02"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-candle-bridge.md"
cid = "market-data-feed"

f = {
    "id": "broker-candle-bridge",
    "name": "Broker candle bridge",
    "role": "republish a broker's own OHLC bars as candle",
    "category": cid,
    "consumes": ["broker-instrument-listing", "broker-candle"],
    "produces": ["candle", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if f["id"] in feats:
    feats[f["id"]].update(f)
else:
    d["features"].append(f)
    feats[f["id"]] = f

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-02_broker_candle_bridge"] = {
    "what": (
        "broker-candle-bridge -- republishes Upstox's OHLC bars as candle, "
        "unblocking kline-window-builder and the real conviction-model "
        "chain (kronos-forecaster, bull/bear-conviction-model) for the "
        "options segment. is_closed inferred from wall-clock elapsed since "
        "the bar's open (Upstox states no closed flag); quote_volume is a "
        "documented close*volume approximation (Upstox states no turnover "
        "figure); trades is None, not a fabricated 0 (Upstox states no "
        "count -- same shape NormalisedCandle.trades already carries for "
        "Bybit)."
    ),
    "origin": "designed by Claude, user asked to cut the live spine over to Indian data 2026-09-02",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-02_broker_candle_bridge.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
