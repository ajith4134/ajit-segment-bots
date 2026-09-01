#!/usr/bin/env python3
"""Add training-label to expiry-day-zero-to-hero-detector's consumes.

Missed in the prior edit (apply_2026-09-01_broker_price_quote_samplers.py):
every other calibrated detector in opportunity-scanner consumes
training-label to feed its SignalCalibrator's observe_outcome -- this one
needs the same wiring, found while implementing its real detection logic.

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

pid = "expiry-day-zero-to-hero-detector"
if "training-label" not in feats[pid]["consumes"]:
    feats[pid]["consumes"].append("training-label")

cid = "opportunity-scanner"
c = cats[cid]
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{pid} consumes: {feats[pid]['consumes']}")
