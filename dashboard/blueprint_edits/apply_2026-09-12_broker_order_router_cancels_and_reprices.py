#!/usr/bin/env python3
"""broker-order-router also cancels and reprices.

docs/proposals/the-broker-order-router.md said it places orders and does neither
of these, because Upstox has separate endpoints for both and neither was on the
adapter. Both are on it now, read from Upstox's own v3 docs on 2026-09-12:

    cancel  DELETE /v3/order/cancel?order_id=...   query parameter, no body
    modify  PUT    /v3/order/modify                JSON body

so the part takes the two consumes ccxt-order-router already had.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "broker-order-router"
ADDED = ["cancel-decision", "order-reprice"]

d = json.loads(REG.read_text())
feature = next((f for f in d["features"] if f["id"] == PART), None)
if feature is None:
    raise SystemExit(f"{PART} is not declared; apply its own edit first")

missing = [name for name in ADDED if name not in feature["consumes"]]
if not missing:
    print(f"already applied: {PART} already consumes {', '.join(ADDED)}")
    raise SystemExit(0)

feature["consumes"] = list(feature["consumes"]) + missing
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART} now consumes {', '.join(feature['consumes'])}")
