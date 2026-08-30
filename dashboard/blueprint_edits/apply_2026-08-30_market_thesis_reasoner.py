#!/usr/bin/env python3
"""Add market-thesis-reasoner, step 3 (the last) of the LLM-reasoning-gets-a-vote proposal.

Proposed by Claude 2026-08-29, approved by the user 2026-08-30.
Rationale: docs/proposals/llm-reasoning-gets-a-vote.md.

Build order step 3, the broadest scope, built last: a real vote that
originates its own thesis for a settings-named list of bellwether symbols,
rather than reviewing a candidate another bot already found. No new data
type: it reads and writes types the blueprint already declares.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/llm-reasoning-gets-a-vote.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

PART = {
    "id": "market-thesis-reasoner",
    "name": "Market thesis reasoner",
    "role": "vote a bellwether symbol's own measured recent direction where the regime backs it",
    "category": "ai-brain",
    "consumes": ["market-regime", "verified-snapshot", "validated-llm-output"],
    "produces": ["directional-opinion", "llm-request", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "compute-bound",
    # A real vote in the arbiter's blend, the broadest-scope of the three --
    # a stale thesis could stand for a direction the price has already reversed.
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "corrupts",
}
if PART["id"] in features:
    features[PART["id"]].update(PART)
else:
    registry["features"].append(PART)
    features[PART["id"]] = PART

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{PART['id']}: present, {len(registry['features'])} features in the blueprint")
