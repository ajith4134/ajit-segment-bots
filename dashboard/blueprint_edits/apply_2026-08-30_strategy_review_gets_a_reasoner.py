#!/usr/bin/env python3
"""Add strategy-review-reasoner, step 1 of the LLM-reasoning-gets-a-vote proposal.

Proposed by Claude 2026-08-29, approved by the user 2026-08-30.
Rationale: docs/proposals/llm-reasoning-gets-a-vote.md.

Build order step 1: the new data type plus its one reasoner, advisory only, no
vote -- proves the LLM-reasons-about-the-system-itself shape works before
setup-second-opinion-reasoner or market-thesis-reasoner ever cast one.

Also appends "strategy-review" to opinion-arbiter's consumes, since the arbiter
is the one part that reads it (folded as a per-bot trust discount, never a
vote -- see parts/ai_brain/opinion_arbiter.py).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/llm-reasoning-gets-a-vote.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

TYPE = {
    "id": "strategy-review",
    "name": "strategy review",
    "description": (
        "A per-bot narrative judgment from an LLM reasoning about which of the "
        "system's own bots or detectors is working, and why -- fed to "
        "opinion-arbiter as a trust discount, never a vote."
    ),
}
if TYPE["id"] not in types:
    registry["data_types"].append(TYPE)
    types[TYPE["id"]] = TYPE

PART = {
    "id": "strategy-review-reasoner",
    "name": "Strategy review reasoner",
    "role": "judge, with reasons, which of this system's own bots or detectors is working",
    "category": "ai-brain",
    "consumes": ["competence-map", "bot-scorecard", "closed-trade", "validated-llm-output"],
    "produces": ["strategy-review", "llm-request", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "compute-bound",
    # A stale review just means the arbiter keeps trusting a bot's last-known
    # standing a little longer -- no trade is corrupted, it only lags.
    "rate_risk": "latency-only",
    "skipped_tick_effect": "delays",
}
if PART["id"] in features:
    features[PART["id"]].update(PART)
else:
    registry["features"].append(PART)
    features[PART["id"]] = PART

CONSUMER_ID = "opinion-arbiter"
NEW_EDGE = "strategy-review"
if CONSUMER_ID not in features:
    raise SystemExit(f"{CONSUMER_ID} is not in the registry -- nothing to wire this into")
if NEW_EDGE not in types:
    raise SystemExit(f"{NEW_EDGE} is not a declared data type -- would be a dangling edge")
consumer = features[CONSUMER_ID]
if NEW_EDGE in consumer["produces"]:
    raise SystemExit(f"{CONSUMER_ID} already produces {NEW_EDGE} -- a self-edge is not a wire")
if NEW_EDGE not in consumer["consumes"]:
    consumer["consumes"].append(NEW_EDGE)
    consumer.setdefault("edited", []).append(
        {"on": "2026-08-30", "added": {"consumes": [NEW_EDGE]}, "origin": "proposed"}
    )

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(
    f"{TYPE['id']}: present, {PART['id']}: present, "
    f"{CONSUMER_ID} consumes {NEW_EDGE}: {NEW_EDGE in consumer['consumes']}, "
    f"{len(registry['features'])} features in the blueprint"
)
