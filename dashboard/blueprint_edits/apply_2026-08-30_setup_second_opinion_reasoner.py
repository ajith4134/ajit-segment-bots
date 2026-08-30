#!/usr/bin/env python3
"""Add setup-second-opinion-reasoner, step 2 of the LLM-reasoning-gets-a-vote proposal.

Proposed by Claude 2026-08-29, approved by the user 2026-08-30.
Rationale: docs/proposals/llm-reasoning-gets-a-vote.md.

Build order step 2: a real vote, but reviewing only a symbol another bot
already has a live, acting directional-opinion on this tick -- bounded by
construction, never proposing its own setup. No new data type: it reads and
writes types the blueprint already declares (opinion-arbiter already consumes
every bot's directional-opinion, so no edge needs adding there).

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
    "id": "setup-second-opinion-reasoner",
    "name": "Setup second opinion reasoner",
    "role": "vote on whether the case for a candidate another bot already found holds up",
    "category": "ai-brain",
    "consumes": ["directional-opinion", "verified-snapshot", "validated-llm-output"],
    "produces": ["directional-opinion", "llm-request", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "compute-bound",
    # A real vote in the arbiter's blend, the same as any acting bot: skipping
    # it changes what the ensemble weighed, and a stale confirmation could
    # stand for a case that no longer holds.
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
