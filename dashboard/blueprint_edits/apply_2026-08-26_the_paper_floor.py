#!/usr/bin/env python3
"""`autonomy-boundary` reads the money mode, so paper trading has a floor.

Proposed by Claude 2026-08-26.
Rationale: docs/proposals/the-envelope-that-could-never-widen.md
Idempotent.

The envelope widens on demonstrated competence, competence is measured from
closed trades, and trading requires the envelope: 49,908 envelopes issued, zero
widenings, 444,545 zero risk limits, 33 order intents refused. On paper there is
no risk to earn the right to take, so the money mode is what the floor is read
from.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-envelope-that-could-never-widen.md"

PART = "autonomy-boundary"
MONEY_MODE = "money-mode"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

if PART not in features:
    raise SystemExit(f"{PART} is not in the registry; this edit is out of date")

producers = [
    feature["id"] for feature in registry["features"] if MONEY_MODE in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing produces {MONEY_MODE}; amend {PROPOSAL} rather than letting this edit "
        f"create a dangling edge (R-01)"
    )

changed = []

consumes = features[PART].setdefault("consumes", [])
if MONEY_MODE not in consumes:
    consumes.append(MONEY_MODE)
    consumes.sort()
    changed.append(f"{PART} consumes {MONEY_MODE}, produced by {', '.join(sorted(producers))}")

note = features[PART].get("note", "")
addition = (
    " Reads the money mode since 2026-08-26: on paper the issued level is floored at "
    "act-within-limits, because competence is measured from closed trades and closed "
    "trades need trading -- a loop with no entry point that left 33 order intents "
    "refused against 444,545 zero risk limits. The level the evidence supports is "
    "still earned, and what may change settings or admit parts still follows it."
)
if addition.strip() not in note:
    features[PART]["note"] = (note + addition).strip()
    changed.append(f"{PART}'s note records why the floor exists")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    for line in changed:
        print(f"  {line}")
else:
    print("  already applied; nothing to change")
