#!/usr/bin/env python3
"""Redirect bull-feature-builder / bear-feature-builder off funding-forecast.

docs/proposals/bull-bear-feature-builders-open-interest.md: funding rate is
a crypto perpetual mechanic with no Indian equivalent; open interest and
order-flow imbalance, summed per underlying from broker-open-interest via
runtime/underlying_open_interest.py, are the honest replacement (user's
instruction: replace crypto-only features with the real Indian analogue,
not just delete). bear-feature-builder's funding_carry_over_horizon has no
replacement yet -- no Indian futures/basis data source is built (Phase B).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

NEW_CONSUMES = {
    "bull-feature-builder": [
        "bull-side-candidate", "broker-instrument-listing", "broker-open-interest",
        "order-book-snapshot", "symbol-price-frame", "symbol-profile", "symbol-universe",
    ],
    "bear-feature-builder": [
        "bear-side-candidate", "broker-instrument-listing", "broker-open-interest",
        "order-book-snapshot", "symbol-price-frame", "symbol-profile", "symbol-universe",
    ],
}

for part_id, consumes in NEW_CONSUMES.items():
    feats[part_id]["consumes"] = consumes

# ------------------------------------------------- recompute affected blocks' contracts
for cid in {feats[pid]["category"] for pid in NEW_CONSUMES}:
    c = next(cat for cat in d["categories"] if cat["id"] == cid)
    parts = [f for f in d["features"] if f["category"] == cid]
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_bull_bear_feature_builders_open_interest"] = {
    "what": (
        "bull-feature-builder and bear-feature-builder redirected from "
        "funding-forecast to broker-instrument-listing + broker-open-interest -- "
        "open-interest change and order-flow imbalance replace funding rate as "
        "the crowd-positioning feature."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_bull_bear_feature_builders_open_interest.py",
    "proposal": "docs/proposals/bull-bear-feature-builders-open-interest.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
for part_id in NEW_CONSUMES:
    print(f"{part_id}: consumes -> {feats[part_id]['consumes']}")
