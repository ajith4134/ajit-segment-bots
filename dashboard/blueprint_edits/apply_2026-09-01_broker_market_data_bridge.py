#!/usr/bin/env python3
"""Declare broker-market-data-bridge.

docs/proposals/broker-market-data-bridge.md: republishes Upstox's LTP as
market-data, the crypto-era type paper-fill-simulator and ~30 other
detector/execution-layer parts already read -- the missing producer for
Indian instruments, same gap broker-underlying-price-frame-bridge closed
for symbol-price-frame.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-market-data-bridge.md"
cid = "market-data-feed"

f = {
    "id": "broker-market-data-bridge",
    "name": "Broker market data bridge",
    "role": "republish a broker's own LTP updates as market-data",
    "category": cid,
    "consumes": ["broker-instrument-listing", "broker-market-data"],
    "produces": ["market-data", "part-health"],
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

d["_proposal_2026-09-01_broker_market_data_bridge"] = {
    "what": (
        "broker-market-data-bridge -- republishes Upstox's LTP as "
        "market-data, unblocking paper-fill-simulator and ~30 other "
        "detector/execution-layer parts for the options segment."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_broker_market_data_bridge.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
