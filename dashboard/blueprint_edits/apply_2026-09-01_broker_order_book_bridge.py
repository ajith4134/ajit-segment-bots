#!/usr/bin/env python3
"""Declare broker-order-book-bridge.

docs/proposals/broker-order-book-bridge.md: republishes Upstox's own depth
as order-book-snapshot, the second input paper-fill-simulator's book-walk
pricing needs.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-order-book-bridge.md"
cid = "market-data-feed"

f = {
    "id": "broker-order-book-bridge",
    "name": "Broker order book bridge",
    "role": "republish a broker's own depth updates as order-book-snapshot",
    "category": cid,
    "consumes": ["broker-instrument-listing", "broker-order-book-snapshot"],
    "produces": ["order-book-snapshot", "part-health"],
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

d["_proposal_2026-09-01_broker_order_book_bridge"] = {
    "what": (
        "broker-order-book-bridge -- republishes Upstox's own depth as "
        "order-book-snapshot, the second of two bridges paper-fill-"
        "simulator needs for the options segment."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_broker_order_book_bridge.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
