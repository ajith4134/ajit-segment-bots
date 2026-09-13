#!/usr/bin/env python3
"""The model picker learns from the enforcer's verdict, and the enforcer judges against real facts.

`llm-model-picker` learned a model's quality from `llm-call-record.succeeded` --
"the call returned" -- while its own docstring says "did the answer pass the
enforcer". `structured-output-enforcer` now publishes `llm-answer-verdict` for
every response it judges, and reads `rendered-llm-request` for the facts the
prompt was rendered with: until now it checked every response against `{}`.

docs/proposals/the-picker-learns-from-the-enforcers-verdict.md carries the
measurement (526 requests, 474 refused, reproduced exactly off the live settings).

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
VERDICT = "llm-answer-verdict"
PROPOSAL_KEY = "_proposal_2026-09-13_the_picker_learns_from_the_enforcers_verdict"

d = json.loads(REG.read_text())
changed = []

if not any(t["id"] == VERDICT for t in d["data_types"]):
    d["data_types"].append({
        "id": VERDICT,
        "name": "LLM answer verdict",
        "description": (
            "Whether one model answer survived structural and factual checking: the "
            "model, the purpose, and the enforcer's state. What llm-model-picker "
            "learns a model's quality on a purpose from."
        ),
    })
    changed.append(f"data type {VERDICT}")


def feature(part_id):
    found = next((f for f in d["features"] if f["id"] == part_id), None)
    if found is None:
        raise SystemExit(f"{part_id} is not declared")
    return found


def add(part_id, key, name):
    part = feature(part_id)
    if name not in part[key]:
        part[key] = list(part[key]) + [name]
        changed.append(f"{part_id} {key} {name}")


add("structured-output-enforcer", "consumes", "rendered-llm-request")
add("structured-output-enforcer", "produces", VERDICT)
add("llm-model-picker", "consumes", VERDICT)

if PROPOSAL_KEY not in d:
    d[PROPOSAL_KEY] = {
        "what": (
            "structured-output-enforcer reads rendered-llm-request for the facts it "
            "checks against and publishes llm-answer-verdict; llm-model-picker learns "
            "quality from that verdict rather than from whether a call returned, and "
            "explores a purpose no model has been measured on instead of refusing it."
        ),
        "evidence": (
            "Live spine 2026-09-13: llm-model-picker 526 requests, 52 chosen, 474 "
            "refused_no_model_clears_the_bar, reproduced exactly by the picker alone "
            "on the live settings; structured-output-enforcer's start_part enforced "
            "every response against {} with require_a_citation on."
        ),
        "origin": 'the operator, 2026-09-13 -- "yes do A B and C"',
        "applied_by": (
            "dashboard/blueprint_edits/"
            "apply_2026-09-13_the_picker_learns_from_the_enforcers_verdict.py"
        ),
        "proposal": "docs/proposals/the-picker-learns-from-the-enforcers-verdict.md",
    }
    changed.append("proposal record")

if not changed:
    print("already applied")
    raise SystemExit(0)

REG.write_text(json.dumps(d, indent=1) + "\n")
print("applied: " + "; ".join(changed))
