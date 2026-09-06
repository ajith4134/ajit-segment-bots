#!/usr/bin/env python3
"""opinion-arbiter stops declaring the one input it never reads.

docs/proposals/the-arbiter-does-not-weigh-maturity.md. Closes the question
apply_2026-09-06_declared_inputs_that_were_never_read.py left for the operator,
answered 2026-09-06: drop it. `bot-maturity` appears exactly once in the part --
inside PART_DECLARATION -- so this changes no behaviour, only what R-01 draws.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-06"
PROPOSAL = "docs/proposals/the-arbiter-does-not-weigh-maturity.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}
part = feats["opinion-arbiter"]

# Stated in full, in the exact order the part's own PART_DECLARATION literal
# uses, for the reason the previous edit gives: PartDeclaration equality compares
# the tuple in order and the blueprint test checks exactly that.
part["consumes"] = [
    "directional-opinion", "market-regime", "regime-break-alert",
    "forecast-bias", "competence-map", "coverage-report", "conflict-ruling",
    "bot-weight", "counter-argument", "regime-memory", "strategy-review",
]
rewiring = {"on": TODAY, "why": PROPOSAL}
if rewiring not in part.setdefault("rewired", []):
    part["rewired"].append(rewiring)

cid = part["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-06_arbiter_does_not_weigh_maturity"] = {
    "what": (
        "opinion-arbiter declared bot-maturity and bound no reader for it, so R-01 "
        "drew a wire on every diagram no message could travel and the audit board "
        "reported NOT CARRYING against a producer that is not at fault. Dropped "
        "rather than bound: the part's own docstring argues item by item what moves "
        "conviction and maturity is not among them; the four parts that really "
        "consume it (live-switch-guard, autonomy-boundary, opinion-conflict-resolver, "
        "exploration-pair-opener) use it to gate what the system is allowed to do "
        "rather than how convinced it is; and its only producer, edge-graduation-gate, "
        "has never published, so binding it would add a code path nothing exercises "
        "one day before the first live session. Whether an arbiter should eventually "
        "weigh how proven a bot is stays open, and is not answered by leaving an "
        "unread declaration in place. This changes no behaviour -- bot-maturity "
        "appeared exactly once in the part, in the declaration itself."
    ),
    "origin": (
        "the operator's call, asked and answered 2026-09-06, on the question "
        "apply_2026-09-06_declared_inputs_that_were_never_read.py deliberately left "
        "open; found under the audit temporary goal of 2026-09-05 "
        "(docs/feature-audit.md) and RL-067, a part's real consumes equal what the "
        "blueprint declares"
    ),
    "applied_by": (
        "dashboard/blueprint_edits/apply_2026-09-06_arbiter_does_not_weigh_maturity.py"
    ),
    "proposal": [PROPOSAL],
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("opinion-arbiter consumes:", part["consumes"])
