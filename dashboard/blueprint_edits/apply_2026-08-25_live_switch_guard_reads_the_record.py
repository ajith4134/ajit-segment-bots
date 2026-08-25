#!/usr/bin/env python3
"""live-switch-guard consumes the record it judges, instead of getattr defaults.

Proposed by Claude 2026-08-25 during the payload-shape sweep.
Rationale: docs/proposals/the-live-switch-guard-had-nothing-to-judge.md
Idempotent.

Three of the four graduation tests were read off `bot-maturity`, which carries
none of them, so they were judged against 0.0 net, 1.0 drawdown and 0.0 days.
The measurements exist on three other wires this part did not consume.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-live-switch-guard-had-nothing-to-judge.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

GUARD = "live-switch-guard"
INPUTS = {
    "bot-scorecard": "bot-scorekeeper",
    "closed-trade": "position-close-detector",
    "drawdown-episode": "drawdown-episode-tracker",
}

guard = features.get(GUARD)
if guard is None:
    raise SystemExit(f"{GUARD} is not in the registry; this edit is out of date")

changed = []
for data_type, expected_producer in INPUTS.items():
    producer = features.get(expected_producer)
    if producer is None or data_type not in producer.get("produces", ()):
        raise SystemExit(
            f"{expected_producer} no longer produces {data_type}, so this edit would give "
            f"{GUARD} a wire to nothing. Amend {PROPOSAL} rather than letting it create a "
            f"dangling edge."
        )
    if data_type not in guard["consumes"]:
        # Appended rather than sorted: a part's declaration in code carries the
        # order its author wrote, and the two are compared for equality.
        guard["consumes"] = list(guard["consumes"]) + [data_type]
        changed.append(f"{GUARD}: consumes {data_type}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
