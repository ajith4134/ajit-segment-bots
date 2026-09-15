#!/usr/bin/env python3
"""broker-symbol-universe-bridge consumes broker-option-greeks.

On an index's expiry day it adds the far strikes a zero-to-hero trade lives on, chosen
by a delta estimated from the index's own at-the-money implied volatility -- which
arrives only on broker-option-greeks. See docs/proposals/expiry-day-far-strikes.md.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "broker-symbol-universe-bridge"
NEEDED = "broker-option-greeks"

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
