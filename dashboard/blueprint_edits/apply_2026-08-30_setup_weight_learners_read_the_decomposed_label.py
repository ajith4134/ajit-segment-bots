#!/usr/bin/env python3
"""bull/bear-setup-weight-learner read training-label, not just the blended scorecard.

Found in this session's own learning-loop audit: both learners trusted a
detector by the bot's blended win/loss (`bot-scorecard`), which conflates the
detector's setup call with whatever this bot's own entry timing, exit timing
or sizing did to the trade afterwards -- exactly the conflation `TrainingLabel`
exists to prevent (runtime/learning_types.py). `bot-scorecard` stays as the
durable prior a restart needs; `training-label`'s own `THE_SETUP_WAS_RIGHT`
component is the live, decomposed signal going forward.

`label-builder` did not carry `direction` on its closed-trade labels either
(left at the dataclass default), which would have made a label unusable here:
with no direction, a symbol's short outcome could not be told from its long
one. Fixed alongside this edit in `parts/learning_loop/label_builder.py`.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

NEW_EDGE = "training-label"
CONSUMER_IDS = ("bull-setup-weight-learner", "bear-setup-weight-learner")

if NEW_EDGE not in types:
    raise SystemExit(f"{NEW_EDGE} is not a declared data type -- would be a dangling edge")

for consumer_id in CONSUMER_IDS:
    if consumer_id not in features:
        raise SystemExit(f"{consumer_id} is not in the registry -- nothing to wire this into")
    consumer = features[consumer_id]
    if NEW_EDGE in consumer["produces"]:
        raise SystemExit(f"{consumer_id} already produces {NEW_EDGE} -- a self-edge is not a wire")
    if NEW_EDGE not in consumer["consumes"]:
        consumer["consumes"].append(NEW_EDGE)
        consumer.setdefault("edited", []).append(
            {"on": "2026-08-30", "added": {"consumes": [NEW_EDGE]}, "origin": "proposed"}
        )

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
for consumer_id in CONSUMER_IDS:
    print(f"{consumer_id} consumes {NEW_EDGE}: {NEW_EDGE in features[consumer_id]['consumes']}")
