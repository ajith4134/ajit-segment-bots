#!/usr/bin/env python3
"""Add signal-outcome-labeller. Proposed by Claude 2026-08-22, agreed by the user.

Rationale: docs/proposals/signal-outcome-labelling.md. Idempotent.

Why the blueprint has to change at all: every route to a trade needs a trained
conviction model, and the model's only source of training-label was the outcome of
a trade. Measured, not inferred -- an untrained OnlineLearner returns logistic(0)
and reports itself unfitted, so the composer forms no opinion and nothing after it
runs. This part closes the cycle by scoring a detector's own claim against the
prices that arrive next, which needs no trade, no position and no capital.

It reads the live feed, never a replay (RL-071).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/signal-outcome-labelling.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

PART = {
    "id": "signal-outcome-labeller",
    "name": "Signal outcome labeller",
    "role": "score whether a detector's expected move happened inside its own horizon",
    "category": "learning-loop",
    "consumes": ["entry-candidate", "market-data"],
    "produces": ["training-label", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "compute-bound",
    # A label is only correct if it is scored at the horizon the candidate named.
    # Score it late and the move has already reversed; skip the tick it was due on
    # and the label is about a different window than the one claimed.
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "corrupts",
}

if PART["id"] in features:
    features[PART["id"]].update(PART)
else:
    registry["features"].append(PART)

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{PART['id']}: present, {len(registry['features'])} features in the blueprint")
