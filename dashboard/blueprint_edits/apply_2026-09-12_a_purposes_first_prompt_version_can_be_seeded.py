#!/usr/bin/env python3
"""One new part: seed-prompt-promoter.

Nothing can promote a purpose's FIRST prompt version, so no LLM call has ever
been made by any of the sixteen parts that publish `llm-request`. Measured on
the live spine 2026-09-12: `prompt-renderer` 906 requests seen and 906 refused
for `no-active-prompt-version-for-this-purpose`, `prompt-registry`
`purposes_with_an_active_version` 0, `prompt-promotion-gate` 0 decisions.

The ring: an active version needs a promotion, a promotion needs a score, a
score needs golden cases and validated output, and validated output needs an
active version. Every arrow is correct. There is no entrance.

`prompt-promotion-gate` is deliberately NOT changed -- promoting an unscored
version is the one thing that part exists to refuse, and it reports
`promotes_without_a_score: False` as a standing claim. The entrance is a
separate, countable part instead (T-6), which can only ever act once per purpose
and never on a replacement.

Full reasoning: docs/proposals/the-first-prompt-for-a-purpose-cannot-be-scored.md

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"

PART = {
    "id": "seed-prompt-promoter",
    "name": "Seed prompt promoter",
    "role": "promote a purpose's first prompt version, which no score can reach",
    "category": "llm-foundation",
    "consumes": ["prompt-version"],
    "produces": ["prompt-promotion", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "claude",
    "proposed": "2026-09-12",
    "evidence": "docs/proposals/the-first-prompt-for-a-purpose-cannot-be-scored.md",
    "resource_class": "compute-bound",
    "rate_risk": "changes-the-answer",
    # A seed not published this tick is published on the next one, and the
    # purpose stays refused in the meantime -- which is the state it has been in
    # since the block was built. Nothing is corrupted by waiting a tick.
    "skipped_tick_effect": "delays",
}

d = json.loads(REG.read_text())
if any(f["id"] == PART["id"] for f in d["features"]):
    print(f"already applied: {PART['id']} is already declared")
    raise SystemExit(0)

anchor = next(
    (i for i, f in enumerate(d["features"]) if f["id"] == "prompt-promotion-gate"), None
)
if anchor is None:
    raise SystemExit("prompt-promotion-gate is not declared; the block moved")

# Placed beside the gate it exists to complement, so a reader of the blueprint
# meets the two promotion paths together rather than finding one of them alone.
d["features"].insert(anchor + 1, PART)
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART['id']} declared in {PART['category']}, after prompt-promotion-gate")
