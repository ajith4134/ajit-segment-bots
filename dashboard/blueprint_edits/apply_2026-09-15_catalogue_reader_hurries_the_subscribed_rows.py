#!/usr/bin/env python3
"""broker-instrument-catalogue-reader consumes broker-subscription-state.

It restated the whole 118,388-row master evenly over 1,800s, so the ~2,000 rows the
feed subscribes came round no faster than the rest and every part that names an
instrument waited up to half an hour after each start. Reading the feed's own
subscription lets it restate those rows on a second, faster conveyor. See
docs/proposals/the-catalogue-hurries-the-subscribed-rows.md.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "broker-instrument-catalogue-reader"
NEEDED = "broker-subscription-state"

d = json.loads(REG.read_text())
feature = next((f for f in d["features"] if f["id"] == PART), None)
if feature is None:
    raise SystemExit(f"{PART} is not declared")
if NEEDED in feature["consumes"]:
    print(f"already applied: {PART} already consumes {NEEDED}")
    raise SystemExit(0)

feature["consumes"] = sorted(set(feature["consumes"]) | {NEEDED})
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART} now consumes {', '.join(feature['consumes'])}")
