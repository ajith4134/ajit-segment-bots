#!/usr/bin/env python3
"""Add broker-market-data to instrument-selector's consumes.

docs/proposals/instrument-selector-atm-strike.md: the option LTP feed
observe_option_price now reads from, wired in start_part. Additive only.

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
NEW_TYPE = "broker-market-data"
existing = feats[PART_ID]["consumes"]
if NEW_TYPE not in existing:
    feats[PART_ID]["consumes"] = existing + [NEW_TYPE]

cid = feats[PART_ID]["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes -> {feats[PART_ID]['consumes']}")
