#!/usr/bin/env python3
"""feed-jump-detector consumes price-increment.

Its ticks floor -- a move of a few ticks is the market, never a jump -- was only ever
reachable through `set_price_increment`, and nothing called it, because the part
declared no input carrying a tick. `tick-size-resolver` already publishes the broker's
declared increment for every symbol as `price-increment`. See
docs/proposals/a-feed-jump-floor-that-ends-with-warm-up.md.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "feed-jump-detector"
NEEDED = "price-increment"

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
