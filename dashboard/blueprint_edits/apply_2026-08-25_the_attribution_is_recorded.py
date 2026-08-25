#!/usr/bin/env python3
"""learning-recorder journals the attribution, so a conclusion survives its process.

Proposed by Claude 2026-08-25 during phase 5, after the operator asked for the
trade attribution on the board and it turned out there was nothing on disk to put
there. Rationale: docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md
Idempotent.

`pnl-attribution` is produced by one part and consumed by five, and no recorder
consumes it -- so it lives on the bus, only while the producing process lives. The
attribution computed on a real closed trade that day, residual share 0.32, simply
vanished. `pnl-attributor`'s own docstring says the rate is "journalled (RL-029)";
journalling was intended and the contract never wired it.

`learning-recorder`'s stated role is to journal what was learned, researched,
forecast, scored. It already consumes six kinds of conclusion. An attribution is
what was scored, and it is the one this whole block exists to produce.

One data type, one part. No new part, no new type, no change to how a conclusion
is recorded -- only whether this one is.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

RECORDER = "learning-recorder"
CONCLUSION = "pnl-attribution"
PRODUCER = "pnl-attributor"

recorder = features.get(RECORDER)
if recorder is None:
    raise SystemExit(f"{RECORDER} is not in the registry; this edit is out of date")

producer = features.get(PRODUCER)
if producer is None or CONCLUSION not in producer.get("produces", ()):
    raise SystemExit(
        f"{PRODUCER} no longer produces {CONCLUSION}, so recording it would be a "
        f"consumer of something nobody makes. Amend {PROPOSAL} rather than letting "
        f"this edit create a dangling edge."
    )

if "journal-entry" not in recorder.get("produces", ()):
    raise SystemExit(
        f"{RECORDER} no longer produces journal-entry, so it is not the part that "
        f"writes conclusions down any more. That is a different design; amend "
        f"{PROPOSAL}."
    )

changed = []
if CONCLUSION not in recorder["consumes"]:
    # Appended rather than sorted: a part's declaration in code carries the order
    # its author wrote, and the two are compared for equality by the wiring check.
    recorder["consumes"] = list(recorder["consumes"]) + [CONCLUSION]
    changed.append(f"{RECORDER}: consumes {CONCLUSION}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
