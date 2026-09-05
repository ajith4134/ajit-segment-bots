#!/usr/bin/env python3
"""An intraday segment is flat before the session ends.

docs/proposals/intraday-square-off-placer.md: Phase A's third bot trades cash
equity intraday on the broker's leverage, and an MIS position left open is
squared off by the broker itself at whatever the book offers, or converted to
delivery with a margin call behind it. Either way the exit is one this system
did not choose, did not price, and cannot learn from.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"
PROPOSAL = "docs/proposals/intraday-square-off-placer.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PLACER = {
    "id": "intraday-square-off-placer",
    "name": "Intraday square-off placer",
    "role": "close everything an intraday segment holds before the session ends",
    "category": "risk-capital-allocation",
    "consumes": ["position", "market-session-state", "money-mode"],
    "produces": ["order-request", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "compute-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "corrupts",
}
if PLACER["id"] in feats:
    feats[PLACER["id"]].update(PLACER)
else:
    d["features"].append(PLACER)
    feats[PLACER["id"]] = PLACER

category = next(c for c in d["categories"] if c["id"] == PLACER["category"])
parts = [f for f in d["features"] if f["category"] == PLACER["category"]]
category["consumes"] = sorted({t for f in parts for t in f["consumes"]})
category["produces"] = sorted({t for f in parts for t in f["produces"]})
category["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d[f"_proposal_{TODAY}_intraday_square_off_placer"] = {
    "what": (
        "intraday-square-off-placer -- closes everything an intraday segment "
        "holds, a settings-named number of minutes before the session closes, "
        "so the broker never squares the position off instead."
    ),
    "why": (
        "Phase A's third bot (the temporary goal of 2026-09-05) trades cash "
        "equity intraday on the broker's leverage. An MIS position left open is "
        "squared off by the broker from around 15:15 IST at whatever the book "
        "offers, or converted to delivery with a margin call behind it -- an "
        "exit this system did not choose, did not price and cannot learn from, "
        "which would reach closed-trade as a fill nobody here decided."
    ),
    "decisions": {
        "D-S1": "not pre-expiry-position-closer with a different trigger: that "
                "part closes a contract about to stop existing, this one closes "
                "everything because the SEGMENT may not hold overnight. "
                "Different questions, and they must stay different in the "
                "journal because why a trade ended is what every learner trains on",
        "D-S2": "whether a segment is intraday is the segment's own statement "
                "(`positions_are_squared_off_daily`), never inferred from its "
                "name -- a segment added later would inherit a guessed answer "
                "in silence. Silence means may-hold, because the two options "
                "segments say nothing and must not be squared off daily",
        "D-S3": "the window is set against the BROKER's deadline, not the "
                "exchange's close: 25 minutes, opening at 15:05, so the exit is "
                "finished before the broker's own square-off begins at ~15:15. "
                "Wider than the expiry closer's 15 for exactly that reason",
        "D-S4": "the exits are placed by runtime/position_exit_placer.py, the "
                "same bound the flattener learned on 2026-08-30 -- a third part "
                "writing that logic again would write that bug again",
    },
    "origin": "designed by Claude, user asked for bot 3 on 2026-09-05",
    "applied_by": ("dashboard/blueprint_edits/"
                   "apply_2026-09-05_intraday_square_off_placer.py"),
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"risk-capital-allocation: "
      f"{len([f for f in d['features'] if f['category'] == 'risk-capital-allocation'])} parts")
print(f"total: {len(d['features'])} features, {len(d['data_types'])} data types")
