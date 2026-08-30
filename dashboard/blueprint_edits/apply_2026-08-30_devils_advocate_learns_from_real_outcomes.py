#!/usr/bin/env python3
"""devils-advocate's veto learner gets a real feedback loop.

Found live 2026-08-30 auditing whether ai-brain actually learns from losses:
devils-advocate.observe_objection_outcome had zero callers anywhere in the
codebase. Its RateEstimator never advanced past 0 observations, is_fitted
was always False, and would_reverse_the_decision could never be True in
production -- the veto capability was dead, not merely unlearned.

brain-self-reflector already judges, per closed trade, whether the counter-
argument's objection was the actual reason it failed (FAILED_AS_ARGUED) or
the trade won despite the objection existing (WON_AS_REASONED /
WON_DESPITE_THE_REASONING) -- exactly the signal devils-advocate's learner
needs. This adds the new `objection-outcome` type carrying that judgment,
narrowed to the objection kind and regime devils-advocate's hit-rate is
actually keyed by (CounterArgument itself did not carry either before this;
see the corresponding code change adding strongest_objection_kind and
regime to CounterArgument).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL_EVIDENCE = "found live 2026-08-30 auditing ai-brain's learning loops"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

TYPE = {
    "id": "objection-outcome",
    "name": "objection outcome",
    "description": (
        "Whether a counter-argument's strongest objection was actually right, judged "
        "by brain-self-reflector from how the trade ended -- fed back to devils-advocate "
        "so its learned hit-rate per (objection kind, regime) can move."
    ),
}
if TYPE["id"] not in types:
    registry["data_types"].append(TYPE)
    types[TYPE["id"]] = TYPE


def add_produces(part_id, data_type):
    feature = features[part_id]
    if data_type not in feature["produces"]:
        feature["produces"].append(data_type)
        feature.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"produces": [data_type]}, "origin": "proposed"}
        )


def add_consumes(part_id, data_type):
    feature = features[part_id]
    if data_type not in feature["consumes"]:
        feature["consumes"].append(data_type)
        feature.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"consumes": [data_type]}, "origin": "proposed"}
        )


for required in ("brain-self-reflector", "devils-advocate"):
    if required not in features:
        raise SystemExit(f"{required} is not in the registry -- nothing to wire this into")

add_produces("brain-self-reflector", "objection-outcome")
add_consumes("devils-advocate", "objection-outcome")

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(
    "objection-outcome: present, "
    f"brain-self-reflector produces it: {'objection-outcome' in features['brain-self-reflector']['produces']}, "
    f"devils-advocate consumes it: {'objection-outcome' in features['devils-advocate']['consumes']}"
)
