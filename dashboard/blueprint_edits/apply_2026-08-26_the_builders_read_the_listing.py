#!/usr/bin/env python3
"""The feature builders read the venue's own funding rate off the listing.

Proposed by Claude 2026-08-26.
Rationale: docs/proposals/the-envelope-that-could-never-widen.md is a different
change; this one has no proposal of its own because it adds no mechanism -- the
funding rate is already on `symbol-universe`, put there by the edit of 2026-08-22
("The funding rate is a listing fact"), and the parts that want it were reading a
forecast of it instead.
Idempotent.

Measured 2026-08-26: bull-feature-builder produced 0 complete vectors of 454, and
`funding_rate` was missing on every one of them -- not because the number is
unavailable but because nothing carried it here. `funding_forecast_change` stays
missing, and honestly so: it needs a premium observation, and nothing on this
system produces one yet.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

UNIVERSE = "symbol-universe"
BUILDERS = ("bull-feature-builder", "bear-feature-builder")

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

for part_id in BUILDERS:
    if part_id not in features:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")

producers = [
    feature["id"] for feature in registry["features"] if UNIVERSE in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing produces {UNIVERSE}; this edit would create a dangling edge (R-01)"
    )

changed = []
for part_id in BUILDERS:
    consumes = features[part_id].setdefault("consumes", [])
    if UNIVERSE not in consumes:
        consumes.append(UNIVERSE)
        consumes.sort()
        changed.append(f"{part_id} consumes {UNIVERSE}, produced by {', '.join(producers)}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    for line in changed:
        print(f"  {line}")
else:
    print("  already applied; nothing to change")
