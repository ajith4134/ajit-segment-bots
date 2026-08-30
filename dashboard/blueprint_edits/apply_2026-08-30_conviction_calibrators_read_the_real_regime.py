#!/usr/bin/env python3
"""bull/bear-conviction-calibrator calibrate against the regime a conviction
actually formed in, and observe outcomes from real closed trades.

Found in this session's own learning-loop audit: the per-regime calibration
machinery (a calibrator per regime, plus an ALL_REGIMES fallback) already
existed, but every call site in both parts passed the literal ALL_REGIMES
constant regardless of what regime a conviction actually formed in -- so it
was declared and tested but never exercised live. `market-regime` (from
regime-classifier) is what `signal-outcome-labeller` already reads for the
same purpose; `training-label` gives each part its own closed-trade record,
mirroring bull-conviction-model's own consumption of it, filtered to the
matching direction since one detector fires both sides.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

NEW_EDGES = ("training-label", "market-regime")
CONSUMER_IDS = ("bull-conviction-calibrator", "bear-conviction-calibrator")

for edge in NEW_EDGES:
    if edge not in types:
        raise SystemExit(f"{edge} is not a declared data type -- would be a dangling edge")

for consumer_id in CONSUMER_IDS:
    if consumer_id not in features:
        raise SystemExit(f"{consumer_id} is not in the registry -- nothing to wire this into")
    consumer = features[consumer_id]
    added = []
    for edge in NEW_EDGES:
        if edge in consumer["produces"]:
            raise SystemExit(f"{consumer_id} already produces {edge} -- a self-edge is not a wire")
        if edge not in consumer["consumes"]:
            consumer["consumes"].append(edge)
            added.append(edge)
    if added:
        consumer.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"consumes": added}, "origin": "proposed"}
        )

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
for consumer_id in CONSUMER_IDS:
    consumes = features[consumer_id]["consumes"]
    print(f"{consumer_id} consumes: {[edge for edge in NEW_EDGES if edge in consumes]}")
