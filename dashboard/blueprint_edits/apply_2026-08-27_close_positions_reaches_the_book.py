#!/usr/bin/env python3
"""position-flattener: the half of `close-positions` that actually closes a position.

Proposed by Claude 2026-08-27 on the operator's instruction to exit every open trade.
Rationale: docs/proposals/close-positions-was-an-instruction-nothing-carried-out.md
Idempotent.

`close-positions` reached `trading-halt-decider` and `halt-enforcer`, and both of
them stop the bot opening something new. Neither closes anything: halt-enforcer
produces `risk-limit` and no part in the blueprint places an exit because a human
asked for one. Exits happened only when a resting stop fired, and 10 of the 17
positions open on the day this was written had no stop resting.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/close-positions-was-an-instruction-nothing-carried-out.md"

PART = {
    "id": "position-flattener",
    "name": "Position flattener",
    "role": "close every open position at market when a human override says close-positions",
    "category": "risk-capital-allocation",
    "consumes": ["human-override", "position", "money-mode"],
    "produces": ["order-request", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "proposed": "2026-08-27",
    "resource_class": "compute-bound",
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}

registry = json.loads(REGISTRY.read_text())
features = registry["features"]
existing = {feature["id"]: feature for feature in features}

if PART["id"] in existing:
    if existing[PART["id"]] == PART:
        print("nothing to do; the registry already carries this edit")
        raise SystemExit(0)
    raise SystemExit(
        f"{PART['id']} is already in the registry with different content; "
        f"amend {PROPOSAL} rather than overwriting it"
    )

for data_type in PART["consumes"]:
    if not any(data_type in feature.get("produces", ()) for feature in features):
        raise SystemExit(
            f"nothing produces {data_type}, so this part would consume a dangling edge; "
            f"amend {PROPOSAL}"
        )

declared = {entry["id"] for entry in registry.get("data_types", ())}
for data_type in PART["consumes"] + PART["produces"]:
    if declared and data_type not in declared:
        raise SystemExit(f"{data_type} is not a declared data type; amend {PROPOSAL}")

categories = {entry["id"] for entry in registry.get("categories", ())}
if categories and PART["category"] not in categories:
    raise SystemExit(f"{PART['category']} is not a declared category; amend {PROPOSAL}")

# Placed beside halt-enforcer: the two are the same instruction's two halves, and
# a reader of the registry should find them together.
insert_at = next(
    (index + 1 for index, feature in enumerate(features) if feature["id"] == "halt-enforcer"),
    len(features),
)
features.insert(insert_at, PART)

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"added {PART['id']} to the registry after halt-enforcer")
