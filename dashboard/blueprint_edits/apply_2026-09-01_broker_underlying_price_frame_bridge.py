#!/usr/bin/env python3
"""Declare broker-underlying-price-frame-bridge.

docs/proposals/broker-underlying-price-frame-bridge.md: closes the producer
gap spec section 4 left open -- regime-classifier and four peers already
need no change, they just had nothing publishing symbol-price-frame for the
index-options segment's underlyings.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}
cats = {c["id"]: c for c in d["categories"]}

PROPOSAL = "docs/proposals/broker-underlying-price-frame-bridge.md"
cid = "market-data-feed"

f = {
    "id": "broker-underlying-price-frame-bridge",
    "name": "Broker underlying price frame bridge",
    "role": "republish a broker's underlying-instrument prices as symbol-price-frame",
    "category": cid,
    "consumes": ["broker-instrument-listing", "broker-price-frame"],
    "produces": ["symbol-price-frame", "part-health"],
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

# ------------------------------------------------- recompute ONLY this block's contract
c = cats[cid]
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_broker_underlying_price_frame_bridge"] = {
    "what": (
        "broker-underlying-price-frame-bridge -- republishes NIFTY/BANKNIFTY/"
        "SENSEX prices as symbol-price-frame so regime-classifier and four "
        "peer detectors need no change for index options (spec section 4)."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_broker_underlying_price_frame_bridge.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
