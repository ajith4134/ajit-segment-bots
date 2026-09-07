#!/usr/bin/env python3
"""stop-target-placer consumes instrument-choice. Proposed by Claude 2026-09-07.

Rationale: docs/proposals/stop-target-placer-prices-the-chosen-instrument.md.
Idempotent.

Why the blueprint has to change at all: this part priced and placed a stop
against the price the bot's own exit plan reasoned in -- the underlying's spot,
not the option contract instrument-selector actually chose to trade. Sizing and
placing a stop for the chosen instrument needs to know its own price, which only
`instrument-choice` carries.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

PART_ID = "stop-target-placer"
ADDED_INPUT = "instrument-choice"

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
