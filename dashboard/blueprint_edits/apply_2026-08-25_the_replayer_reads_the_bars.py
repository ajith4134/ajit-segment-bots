#!/usr/bin/env python3
"""instruction-replayer consumes historical-window, so a replay has bars.

Proposed by Claude 2026-08-25 during the payload-shape sweep.
Rationale: docs/proposals/a-split-names-where-it-cuts-not-the-bars.md
Idempotent.

The part's own docstring named this edit as the fix: a split says where it cuts
and never carries the bars, so every split arrived without them and nothing was
ever replayed.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/a-split-names-where-it-cuts-not-the-bars.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

REPLAYER = "instruction-replayer"
WINDOW = "historical-window"
PRODUCER = "historical-bar-store"

replayer = features.get(REPLAYER)
if replayer is None:
    raise SystemExit(f"{REPLAYER} is not in the registry; this edit is out of date")

producer = features.get(PRODUCER)
if producer is None or WINDOW not in producer.get("produces", ()):
    raise SystemExit(
        f"{PRODUCER} no longer produces {WINDOW}; amend {PROPOSAL} rather than letting "
        f"this edit create a dangling edge."
    )

changed = []
if WINDOW not in replayer["consumes"]:
    replayer["consumes"] = list(replayer["consumes"]) + [WINDOW]
    changed.append(f"{REPLAYER}: consumes {WINDOW}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
