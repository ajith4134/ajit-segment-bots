#!/usr/bin/env python3
"""trade-capital-bounds-gate consumes position. Proposed by Claude 2026-09-07.

Rationale: docs/proposals/the-capital-bound-is-on-the-position-not-the-order.md.
Idempotent.

Why the blueprint has to change at all: `maximum_capital_per_trade` is "the most
one trade may commit", and this gate applied it to one *order*. A position is the
sum of many orders and the bots re-decide the same contract every few seconds, so
66 open positions stood above the ceiling on the live spine, the largest at
Rs 1,277,667 against 200,000. Bounding what an order would make the position
needs to know what the position is, which only `position` carries.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

PART_ID = "trade-capital-bounds-gate"
ADDED_INPUT = "position"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

part = features.get(PART_ID)
if part is None:
    raise SystemExit(f"{PART_ID} is not in the blueprint; this edit changes an existing part")

consumes = list(part["consumes"])
if ADDED_INPUT not in consumes:
    consumes.append(ADDED_INPUT)
    part["consumes"] = consumes

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes {', '.join(part['consumes'])}")
