#!/usr/bin/env python3
"""Every conviction floor reads `liquidity-grade`, so a plan is charged the cost
of the symbol it was written on.

Idempotent: re-running it changes nothing.
"""

from __future__ import annotations

import json
import pathlib

REG = pathlib.Path(__file__).resolve().parents[2] / "docs/features.json"
TODAY = "2026-09-08"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/the-floor-charges-the-symbols-own-cost.md"

GATES = (
    "bull-opinion-composer",
    "bear-opinion-composer",
    "tail-opinion-composer",
    "opinion-arbiter",
)

for part_id in GATES:
    feature = feats[part_id]
    feature["consumes"] = sorted(set(feature["consumes"]) | {"liquidity-grade"})

for cid in sorted({feats[part_id]["category"] for part_id in GATES}):
    category = next(cat for cat in d["categories"] if cat["id"] == cid)
    parts = [feature for feature in d["features"] if feature["category"] == cid]
    category["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
    category["produces"] = sorted({x for feature in parts for x in feature["produces"]})
    category["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-08_the_floor_charges_the_symbols_own_cost"] = {
    "what": (
        "The four gates that compute a break-even -- both opinion composers, "
        "the tailgater's composer and the arbiter -- now consume "
        "`liquidity-grade` and charge a plan the round trip measured for that "
        "plan's own symbol, falling back to `per_side_trading_cost_fraction` "
        "only where nothing recent has graded it. Until now every gate charged "
        "that one rate, derived for an NSE option premium, against risk "
        "fractions often measured on an underlying stock: the cost and the risk "
        "were fractions of two different instruments' prices, so their ratio "
        "meant nothing and the floor it produced was uncrossable."
    ),
    "evidence": (
        "Live spine, 2026-09-08: 9,085 trade intents formed, every one of them "
        "`stand-aside` for `conviction-below-threshold`, with "
        "`bull-opinion-composer.last_floor` pinned at 1.0. "
        "measurements/2026-09-08-conviction-floor-name-mismatch/ carries the "
        "arithmetic and the per-instrument floors."
    ),
    "origin": "the user, 2026-09-08 -- \"fix all the design questions and "
              "errors of no new trades opening in 3 segments\"",
    "applied_by": "dashboard/blueprint_edits/"
                  "apply_2026-09-08_the_floor_charges_the_symbols_own_cost.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
for part_id in GATES:
    print(f"{part_id}: consumes now {feats[part_id]['consumes']}")
