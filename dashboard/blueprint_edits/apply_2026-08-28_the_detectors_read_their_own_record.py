#!/usr/bin/env python3
"""The nine scanner detectors read the record of whether they were right.

Proposed by Claude 2026-08-28, after tracing why `bull-feature-builder` had
produced zero complete feature vectors in its entire life.
Rationale: docs/proposals/nine-detectors-that-never-learn-whether-they-were-right.md
Idempotent.

Every one of these nine carries a `SignalCalibrator` and defines the method that
feeds it. Nothing calls any of them, and nothing could: none declared an input
carrying an outcome, so no `start_part` had anything to call it with. All nine
report `outcomes_learned: 0` and no probe reads that as a fault, because nothing
failed -- it never happened.

`signal-outcome-labeller` already measures the answer and publishes it as
`training-label`, keyed by the detector that made the claim. It travels to the
conviction models and to the two profilers. It has never travelled back to the
parts whose claims it is judging. This adds that edge and nothing else: no part
gains a produces, loses one, or changes state.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/nine-detectors-that-never-learn-whether-they-were-right.md"

# What is being added, and to whom. One data type, nine parts.
THE_RECORD = "training-label"

DETECTORS = (
    "funding-skew-detector",
    "liquidation-cascade-detector",
    "mean-reversion-detector",
    "momentum-burst-detector",
    "sentiment-shift-detector",
    "spread-reversion-detector",
    "universal-symbol-sweeper",
    "volatility-gap-detector",
    "whale-flow-detector",
)

registry = json.loads(REGISTRY.read_text())
features = registry["features"]
by_id = {feature["id"]: feature for feature in features}

missing = [part_id for part_id in DETECTORS if part_id not in by_id]
if missing:
    raise SystemExit(
        f"the registry has no {', '.join(missing)}; this edit names a part that "
        f"does not exist, so amend {PROPOSAL}"
    )

# The edge has to have a far end. R-01 computes edges from consumes/produces, so
# a consume nothing produces is a dangling edge rather than an error at runtime --
# it would simply never deliver, which is the defect this edit exists to fix.
producers = [
    feature["id"] for feature in features
    if THE_RECORD in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing in the registry produces {THE_RECORD}, so these nine would "
        f"consume a dangling edge; amend {PROPOSAL}"
    )

changed = []
for part_id in DETECTORS:
    feature = by_id[part_id]
    consumes = list(feature.get("consumes", ()))
    if THE_RECORD in consumes:
        continue
    # Appended rather than sorted in: the order a part lists its inputs is the
    # order its author thought about them, and rewriting all nine lists would
    # make the diff say more changed than did.
    consumes.append(THE_RECORD)
    feature["consumes"] = consumes
    changed.append(part_id)

if not changed:
    print("nothing to do; the registry already carries this edit")
    raise SystemExit(0)

REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")

print(f"{THE_RECORD} added to the consumes of {len(changed)} part(s):")
for part_id in changed:
    print(f"  {part_id}")
print(f"\nproduced by: {', '.join(sorted(producers))}")
print("\nnow run:  python3 dashboard/check_contracts.py")
