#!/usr/bin/env python3
"""Two entry timers stop guessing at entry-quality; the tail planner learns
when it is flat.

Proposed by Claude 2026-08-29. Rationale:
docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md
Idempotent.

bull-entry-timer / bear-entry-timer called observe_entry_quality(entry) with
the whole entry-quality payload where the method wants (detector,
extension_at_entry, given_away) -- entry-quality has never carried a
detector name. Both timers now match given_away from trade-episode instead,
which already carries venue_id/symbol/detector.

tail-trailing-exit-planner's trail never advanced in production:
advance_trail/forget_position exist and are correct but were never called
from start_part, because the part had no way to know when a position closed.
It now consumes position for exactly that.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

DROPPED_EDGES = (
    ("bull-entry-timer", "entry-quality"),
    ("bear-entry-timer", "entry-quality"),
)
NEW_EDGES = (
    ("bull-entry-timer", "trade-episode"),
    ("bear-entry-timer", "trade-episode"),
    ("tail-trailing-exit-planner", "position"),
)

changed = []

for consumer_id, data_type in DROPPED_EDGES:
    consumer = features.get(consumer_id)
    if consumer is None:
        raise SystemExit(f"{consumer_id} is not in the registry; this edit is out of date")
    if data_type in consumer["consumes"]:
        consumer["consumes"] = [d for d in consumer["consumes"] if d != data_type]
        changed.append(f"{consumer_id}: no longer consumes {data_type}")

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
