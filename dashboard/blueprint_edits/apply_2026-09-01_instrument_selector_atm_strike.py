#!/usr/bin/env python3
"""Add broker-instrument-listing and broker-option-greeks to
instrument-selector's consumes.

docs/proposals/instrument-selector-atm-strike.md. Additive only -- the
existing crypto perpetual path (symbol-universe, symbol-price-frame,
implied-vol-surface, liquidity-grade, timed-intent, symbol-quote-frame)
stays intact for the futures segment; nothing is removed.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PART_ID = "instrument-selector"
NEW_TYPES = ("broker-instrument-listing", "broker-option-greeks")
existing = feats[PART_ID]["consumes"]
feats[PART_ID]["consumes"] = existing + [t for t in NEW_TYPES if t not in existing]

cid = feats[PART_ID]["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_instrument_selector_atm_strike"] = {
    "what": (
        "instrument-selector gains ATM strike selection for the "
        "index-options segment -- broker-instrument-listing and "
        "broker-option-greeks added to its consumes, additive only."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_instrument_selector_atm_strike.py",
    "proposal": "docs/proposals/instrument-selector-atm-strike.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes -> {feats[PART_ID]['consumes']}")
