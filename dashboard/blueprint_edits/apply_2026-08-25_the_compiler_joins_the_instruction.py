#!/usr/bin/env python3
"""watch-condition-compiler consumes opportunity-instruction, so it has a body to compile.

Proposed by Claude 2026-08-25 during the payload-shape sweep.
Rationale: docs/proposals/a-verdict-names-an-instruction-it-does-not-carry.md
Idempotent.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/a-verdict-names-an-instruction-it-does-not-carry.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

COMPILER = "watch-condition-compiler"
INSTRUCTION = "opportunity-instruction"

compiler = features.get(COMPILER)
if compiler is None:
    raise SystemExit(f"{COMPILER} is not in the registry; this edit is out of date")

producers = [
    feature["id"] for feature in registry["features"]
    if INSTRUCTION in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing produces {INSTRUCTION} any more; amend {PROPOSAL} rather than letting "
        f"this edit create a dangling edge."
    )

changed = []
if INSTRUCTION not in compiler["consumes"]:
    compiler["consumes"] = list(compiler["consumes"]) + [INSTRUCTION]
    changed.append(f"{COMPILER}: consumes {INSTRUCTION} (produced by {', '.join(producers)})")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
