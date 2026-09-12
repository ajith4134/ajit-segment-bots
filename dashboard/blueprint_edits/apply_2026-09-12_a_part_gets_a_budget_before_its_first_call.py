#!/usr/bin/env python3
"""part-token-budgeter consumes llm-request too.

It had issued **0** budgets, ever. Measured on the live spine 2026-09-12:
`budgets_issued` 0, `parts_with_a_measured_share` 0, with a full pool in front of
it -- 200 calls, 2,000,000 tokens, $1.

The cause is a cold start of the same shape as the prompt promotion ring. This
part learned that a part exists only from an `llm-call-record`; a record exists
only after a call; a call needs `llm-request-router` to find that part's budget.
So no part could ever be given its first allowance, and `llm_budget_starting_share`
-- a setting whose entire purpose is the share a part gets before anything is
measured about it -- could never be applied to anybody.

`llm-request` is the wire that names a part before it has called: `asked_by` was
added to that payload on the same day for exactly this, because nothing on it
named the asking part.

Nothing about the allocation changes. `share_for` already returns the starting
share for a part it has never seen; it was simply never asked about one.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "part-token-budgeter"
NEEDED = "llm-request"

d = json.loads(REG.read_text())
feature = next((f for f in d["features"] if f["id"] == PART), None)
if feature is None:
    raise SystemExit(f"{PART} is not declared")
if NEEDED in feature["consumes"]:
    print(f"already applied: {PART} already consumes {NEEDED}")
    raise SystemExit(0)

feature["consumes"] = sorted(set(feature["consumes"]) | {NEEDED})
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART} now consumes {', '.join(feature['consumes'])}")
