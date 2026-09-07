#!/usr/bin/env python3
"""prompt-template-author consumes llm-request. Proposed by Claude 2026-09-07.

Rationale: docs/proposals/prompt-template-author-learns-which-purposes-are-asked-for.md.
Idempotent.

Why the blueprint has to change at all: this part writes a template for a purpose
named by *evidence* -- a research finding's topic, a skill's title -- while every
request that will ever be made names one of eleven fixed purposes declared as a
constant by the part making it. The two sets cannot intersect, so no template it
ever writes can answer a real request, and `prompt-renderer` refused all 132
requests it saw for `refused_no_active_version` while `llm-request-router` held
12,154 it could not route. The purposes that need templates are the purposes
being asked for, and they arrive stamped on `llm-request`.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

PART_ID = "prompt-template-author"
ADDED_INPUT = "llm-request"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

part = features.get(PART_ID)
if part is None:
    raise SystemExit(f"{PART_ID} is not in the blueprint; this edit changes an existing part")

consumes = list(part["consumes"])
if ADDED_INPUT not in consumes:
    consumes.append(ADDED_INPUT)
    part["consumes"] = consumes

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes {', '.join(part['consumes'])}")
