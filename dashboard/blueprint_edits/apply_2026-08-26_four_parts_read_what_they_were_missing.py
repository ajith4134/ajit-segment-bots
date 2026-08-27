#!/usr/bin/env python3
"""Four parts consume the input each was already written to need.

Proposed by Claude 2026-08-26, fixing four defects found by measuring the live spine.
Rationale: docs/proposals/four-parts-that-needed-an-input-nobody-had-given-them.md
Idempotent.

Each row below is a part whose code already reached for something the blueprint
never gave it, and each was a live defect rather than a tidy-up:

- `tail-trailing-exit-planner` -> `move-remaining`. Its `start_part` handed every
  candidate a literal `None` and `plan()` dereferenced it, so the first
  follow-candidate `tail-mover-qualifier` ever produced crash-looped the part.
  The estimate it needed was already being published by
  `tail-move-remaining-estimator` and consumed by nobody but the conviction model.

- `signal-outcome-labeller` -> `market-regime`. It defaulted every claim's regime
  to the string "any", so every training label it has ever built carries one
  regime and everything downstream that learns per regime -- the signal
  calibrator, the regime tagger, the conviction model -- was pooling regimes it
  could not tell apart.

- `liquidation-cluster-mapper` -> `symbol-universe`. Nothing ever called its
  `observe_margin_schedule`, so it published 544,798 maps in one run, every one
  `refused-no-margin-schedule` and none carrying a single cluster. The ladder
  rides on the symbol universe because `symbol-catalogue-reader` already makes
  the venue's REST calls and a maintenance margin ladder is a fact about a listed
  contract.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/four-parts-that-needed-an-input-nobody-had-given-them.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

NEW_EDGES = (
    ("tail-trailing-exit-planner", "move-remaining"),
    ("signal-outcome-labeller", "market-regime"),
    ("liquidation-cluster-mapper", "symbol-universe"),
)

changed = []
for consumer_id, data_type in NEW_EDGES:
    consumer = features.get(consumer_id)
    if consumer is None:
        raise SystemExit(f"{consumer_id} is not in the registry; this edit is out of date")

    producers = [
        feature["id"] for feature in registry["features"]
        if data_type in feature.get("produces", ())
    ]
    if not producers:
        raise SystemExit(
            f"nothing produces {data_type} any more; amend {PROPOSAL} rather than letting "
            f"this edit create a dangling edge."
        )
    if consumer_id in producers:
        raise SystemExit(
            f"{consumer_id} produces {data_type} itself; consuming it would be a self-edge"
        )

    if data_type not in consumer["consumes"]:
        consumer["consumes"] = list(consumer["consumes"]) + [data_type]
        changed.append(f"{consumer_id}: consumes {data_type} (produced by {', '.join(producers)})")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
