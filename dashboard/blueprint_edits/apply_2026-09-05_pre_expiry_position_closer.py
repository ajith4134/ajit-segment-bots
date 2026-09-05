#!/usr/bin/env python3
"""Nothing is still held when a contract stops existing.

docs/proposals/pre-expiry-position-closer.md: the options spec decided on
2026-09-01 that positions are force-closed before expiry rather than held to
exercise, and nothing implemented it. Measured 2026-09-05: no part in the tree
reasons about closing a position before its contract expires.

It matters most for Phase A's second segment bot. Indian single-stock options
are physically settled, so a bought call held through expiry becomes a delivery
obligation for strike x lot size rather than a premium that expires worthless.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"
PROPOSAL = "docs/proposals/pre-expiry-position-closer.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

CLOSER = {
    "id": "pre-expiry-position-closer",
    "name": "Pre-expiry position closer",
    "role": "close what is held in a contract that expires today, before the session closes",
    "category": "risk-capital-allocation",
    "consumes": [
        "position", "broker-subscribed-instrument-listing", "market-session-state",
        "money-mode",
    ],
    "produces": ["order-request", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "compute-bound", "rate_risk": "changes-the-answer",
    # `corrupts`, not `delays`: a tick skipped inside the closing window is a
    # position that is still open when the contract settles, and there is no
    # later tick that can undo it. Every other exit-placing part here delays.
    "skipped_tick_effect": "corrupts",
}
if CLOSER["id"] in feats:
    feats[CLOSER["id"]].update(CLOSER)
else:
    d["features"].append(CLOSER)
    feats[CLOSER["id"]] = CLOSER

category = next(c for c in d["categories"] if c["id"] == CLOSER["category"])
parts = [f for f in d["features"] if f["category"] == CLOSER["category"]]
category["consumes"] = sorted({t for f in parts for t in f["consumes"]})
category["produces"] = sorted({t for f in parts for t in f["produces"]})
category["contract_recomputed"] = {
    "on": TODAY, "from": "its parts", "origin": "proposed",
}

d[f"_proposal_{TODAY}_pre_expiry_position_closer"] = {
    "what": (
        "pre-expiry-position-closer -- closes at market anything held in a "
        "contract that expires today, a settings-named number of minutes before "
        "the session closes, rather than holding it to settlement."
    ),
    "why": (
        "The options spec section 2 decided it on 2026-09-01 ('Force-close "
        "before expiry ... rather than held to exercise') and nothing "
        "implemented it: measured 2026-09-05, no part in the tree reasons about "
        "closing a position before its contract expires, and position-flattener "
        "-- the only part that places an exit on its own initiative -- acts only "
        "on a human's close-positions. Indian single-stock options are "
        "PHYSICALLY settled, so a bought call held through expiry becomes a "
        "delivery obligation for strike x lot size, not a premium that expires; "
        "index options are the milder, cash-settled case of the same rule."
    ),
    "decisions": {
        "D-X1": "minutes before the session's close, never a buffer of whole "
                "days as the spec sketched -- a day-scale buffer would forbid "
                "expiry-day-zero-to-hero-detector from trading the expiry-day "
                "move it exists for",
        "D-X2": "the exits are placed by runtime/position_exit_placer.py, "
                "extracted from position-flattener rather than reimplemented: "
                "its bound (allowance = held now - already asked and "
                "unanswered) is the residue of the 2026-08-30 incident that put "
                "1,048,525 USDT of exit notional against a 190,900 USDT book, "
                "and a second part writing that logic again would write that "
                "bug again",
        "D-X3": "the expiry is read from broker-subscribed-instrument-listing "
                "under BOTH instrument_key and trading_symbol, because a "
                "position's symbol is one or the other depending on which part "
                "opened it; a position whose contract cannot be resolved is "
                "counted in positions_with_no_listing, never assumed safe",
        "D-X4": "skipped_tick_effect is `corrupts` -- a tick skipped inside the "
                "closing window is a position still open when the contract "
                "settles, and no later tick can undo it",
    },
    "origin": "designed by Claude, user asked for the expiry close-out part 2026-09-05",
    "applied_by": ("dashboard/blueprint_edits/"
                   "apply_2026-09-05_pre_expiry_position_closer.py"),
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"risk-capital-allocation: "
      f"{len([f for f in d['features'] if f['category'] == 'risk-capital-allocation'])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
      f"{len(d['data_types'])} data types")
