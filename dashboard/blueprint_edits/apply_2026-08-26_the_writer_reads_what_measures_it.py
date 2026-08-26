#!/usr/bin/env python3
"""instruction-writer reads novelty, sample size and the regime tag from their producers.

Proposed by Claude 2026-08-26 while unblocking the instruction chain.
Rationale: docs/proposals/the-writer-read-its-conditions-through-a-part-that-could-not-rank.md
Idempotent.

Three of instruction-writer's conditions were fed by `hypothesis-priority` alone:
novelty, the trades required, and (through the hypothesis itself) the regime.
`hypothesis-ranker` cannot rank a hypothesis whose expected edge is unknown, and
a hypothesis that has never traded has no known edge -- so every priority it
published carried None in all three fields. Measured on the live spine:
`hypotheses_ranked` 0 against `not_enough_inputs` 602,112, while 602,112
priorities were published.

The three parts that actually measure those facts already publish them and
nothing consumed them for this purpose: `hypothesis-deduplicator` publishes
`novelty-score`, `power-estimator` publishes `required-sample-size`, and
`hypothesis-regime-tagger` publishes `hypothesis-regime-tag`.

`hypothesis-ranker` additionally gains `candidate-formula`, so it can read a
mined formula's held-out excess as its pre-trade expected edge and rank at all.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-writer-read-its-conditions-through-a-part-that-could-not-rank.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

# Each row: the part that gains an input, and the data type it gains.
NEW_EDGES = (
    ("instruction-writer", "novelty-score"),
    ("instruction-writer", "required-sample-size"),
    ("instruction-writer", "hypothesis-regime-tag"),
    ("hypothesis-ranker", "candidate-formula"),
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
