#!/usr/bin/env python3
"""bull/bear-conviction-model score their own champion and challenger, so
champion-challenger-gate can actually promote one.

Found in this session's own learning-loop audit: champion-choice and
retrain-request were on both conviction models' consumes from the start, but
nothing ever called apply_champion_choice / apply_retrain_request. Tracing why
led to a bigger gap: champion-challenger-gate compares
model-version.validation_score, which model-registry has always hardcoded to
0.0 for every model, so nothing could ever have been promoted through that
path regardless of model.

An online model that trains continuously has no discrete trained artefact for
model-registry's ledger to hold and no separate held-out data for a
refutation battery or a trial ledger to test -- so it is scored directly: this
adds the new `online-model-score` type, produced by both conviction models
(prequential score per slot) and consumed by champion-challenger-gate, which
treats bull-conviction-model/bear-conviction-model as promoting on score
improvement alone (see the corresponding code change to
ChampionChallengerGate.decide).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

TYPE = {
    "id": "online-model-score",
    "name": "online model score",
    "description": (
        "A continuously-trained model's own prequential score for one of its slots "
        "(champion or challenger) -- each example judged by that slot's error on it "
        "before training on it, which is what makes the running mean a held-out "
        "measure without a separate validation split. Read by champion-challenger-gate "
        "in place of model-version.validation_score for a model with no discrete "
        "trained artefact for that ledger to hold."
    ),
}
if TYPE["id"] not in types:
    registry["data_types"].append(TYPE)
    types[TYPE["id"]] = TYPE


def add_produces(part_id, data_type):
    feature = features[part_id]
    if data_type in feature["consumes"]:
        raise SystemExit(f"{part_id} already consumes {data_type} -- a self-edge is not a wire")
    if data_type not in feature["produces"]:
        feature["produces"].append(data_type)
        feature.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"produces": [data_type]}, "origin": "proposed"}
        )


def add_consumes(part_id, data_type):
    feature = features[part_id]
    if data_type in feature["produces"]:
        raise SystemExit(f"{part_id} already produces {data_type} -- a self-edge is not a wire")
    if data_type not in feature["consumes"]:
        feature["consumes"].append(data_type)
        feature.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"consumes": [data_type]}, "origin": "proposed"}
        )


for required in ("bull-conviction-model", "bear-conviction-model", "champion-challenger-gate"):
    if required not in features:
        raise SystemExit(f"{required} is not in the registry -- nothing to wire this into")

add_produces("bull-conviction-model", "online-model-score")
add_produces("bear-conviction-model", "online-model-score")
add_consumes("champion-challenger-gate", "online-model-score")

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(
    "online-model-score: present, "
    f"bull-conviction-model produces it: {'online-model-score' in features['bull-conviction-model']['produces']}, "
    f"bear-conviction-model produces it: {'online-model-score' in features['bear-conviction-model']['produces']}, "
    f"champion-challenger-gate consumes it: {'online-model-score' in features['champion-challenger-gate']['consumes']}"
)
