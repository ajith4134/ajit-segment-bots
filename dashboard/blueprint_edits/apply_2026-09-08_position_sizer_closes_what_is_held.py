#!/usr/bin/env python3
"""position-sizer also consumes `position`, and trade-capital-bounds-gate
carries `action` on its sized/bounded orders.

Real incident, 2026-09-08: every actionable trade-intent formed on the live
spine all session was a CLOSE (100% of 4,483 sampled -- zero OPEN actions
appeared at all). position-sizer sized every intent through the same
entry/stop risk-budget path an OPEN uses, and a CLOSE carries neither by
construction -- there is nowhere new to enter, and stop-target-placer has
no reason to plan a stop for a position already open. Every close was
refused as missing_stop_price. Nothing this project already held could be
exited through this part, for the whole session.

Fixed by sizing CLOSE/REDUCE against the position itself
(`position-sizer.close_order`, reading the new `position` consumption)
rather than against a risk budget it was never going to carry, and by
teaching trade-capital-bounds-gate to skip its capital-ceiling economics
for a close (which reduces what is committed, not adds to it) via a new
`action` field carried on `sized-order` and `bounded-order`.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-08"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/position-sizer-closes-what-is-held.md"

f = feats["position-sizer"]
f["consumes"] = sorted(set(f["consumes"]) | {"position"})

cid = "risk-capital-allocation"
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-08_position_sizer_closes_what_is_held"] = {
    "what": (
        "position-sizer now consumes `position` and sizes CLOSE/REDUCE "
        "intents against the held quantity (close_order) instead of "
        "through the entry/stop risk-budget path OPEN uses -- a close "
        "carries neither by construction, and every one was refused as "
        "missing_stop_price all session. trade-capital-bounds-gate's "
        "sized-order/bounded-order now carry `action`, and the gate skips "
        "its capital-ceiling economics for a close, which was checking a "
        "closing order's notional against the position's own committed "
        "capital as though it were adding more."
    ),
    "origin": "found and fixed by Claude during a live-spine investigation "
              "the user asked for, 2026-09-08 -- \"also no new trades did "
              "not open today\"",
    "applied_by": "dashboard/blueprint_edits/"
                  "apply_2026-09-08_position_sizer_closes_what_is_held.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"position-sizer: consumes now {f['consumes']}")
print(f"{cid}: {len([x for x in d['features'] if x['category'] == cid])} parts")
