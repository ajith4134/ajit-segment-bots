#!/usr/bin/env python3
"""The cost of borrowing is the broker's interest, not a perpetual's funding.

docs/proposals/carry-replaces-funding.md: `leverage-selector` consumed
`funding-forecast`, whose only producer is `funding-rate-forecaster`, whose only
input is `venue-premium` from `venue-premium-stream-reader` -- a three-part
crypto chain with no Indian producer at any link. An absent forecast returned a
penalty of 0.5, so every leverage this project chose on an Indian segment was
silently halved for a cost nobody charges.

Replaced, not deleted: the carry is computed from `broker-margin-requirement`,
which the selector already consumes -- borrowed = notional - margin, and the
daily rate is the broker's own. The three orphaned crypto parts and their two
types are retired.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"
PROPOSAL = "docs/proposals/carry-replaces-funding.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

# ---- the selector stops reading a type nothing Indian produces
selector = feats["leverage-selector"]
selector["consumes"] = [t for t in selector["consumes"] if t != "funding-forecast"]
selector["role"] = "choose the leverage each trade is opened at"
history = selector.setdefault("rewired", [])
record = {"on": TODAY, "why": PROPOSAL}
if record not in history:
    history.append(record)

# ---- the crypto chain that leaves behind, retired the way the others were
RETIRED = ("funding-rate-forecaster", "venue-premium-stream-reader")
for part_id in RETIRED:
    if part_id in feats:
        d["features"] = [f for f in d["features"] if f["id"] != part_id]
        del feats[part_id]

RETIRED_TYPES = ("funding-forecast", "venue-premium")
for type_id in RETIRED_TYPES:
    still_read = [f["id"] for f in d["features"] if type_id in f["consumes"]]
    still_written = [f["id"] for f in d["features"] if type_id in f["produces"]]
    if still_read or still_written:
        raise SystemExit(
            f"refusing to retire '{type_id}': still produced by {still_written} "
            f"and consumed by {still_read}. A type is retired when nothing needs it, "
            f"never to make a diff smaller."
        )
d["data_types"] = [t for t in d["data_types"] if t["id"] not in RETIRED_TYPES]

for category_id in sorted({selector["category"], "prediction", "market-data-feed"}):
    category = next((c for c in d["categories"] if c["id"] == category_id), None)
    if category is None:
        continue
    parts = [f for f in d["features"] if f["category"] == category_id]
    category["consumes"] = sorted({t for f in parts for t in f["consumes"]})
    category["produces"] = sorted({t for f in parts for t in f["produces"]})
    category["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d[f"_proposal_{TODAY}_carry_replaces_funding"] = {
    "what": (
        "leverage-selector's funding input replaced by the broker's own cost of "
        "borrowing, computed from broker-margin-requirement rather than "
        "forecast: borrowed = notional - margin required, carry = borrowed "
        "fraction x the broker's daily rate. funding-rate-forecaster and "
        "venue-premium-stream-reader retired with the types funding-forecast "
        "and venue-premium, which nothing produces or consumes any more."
    ),
    "why": (
        "The user's standing instruction is that a crypto-only part is replaced "
        "with its Indian analogue rather than left in place. `funding-forecast` "
        "had no Indian producer and never would: funding is a perpetual's "
        "mechanism. Worse than dead -- `_funding_penalty` returned 0.5 for an "
        "absent forecast, deliberately, because a perpetual always pays funding "
        "and not knowing the rate is a risk. On an Indian segment that meant "
        "every leverage was halved for a cost nobody charges. An intraday "
        "equity position squared off the same session pays no financing at all."
    ),
    "decisions": {
        "D-C1": "computed, not forecast: the borrowed amount is already known "
                "from what the broker lends, so no part had to be built to "
                "predict it and no new data type was needed",
        "D-C2": "zero is the measured answer for intraday MIS, not a "
                "placeholder -- interest is what the margin trading facility "
                "charges to carry overnight, a product intraday-square-off-"
                "placer exists to keep this segment out of. The mechanism is "
                "wired so a real MTF rate reduces leverage the day one applies",
        "D-C3": "a missing broker quote is still unlevered on a borrowing "
                "segment (UNLEVERAGED_NO_BROKER_QUOTE), so removing the 0.5 "
                "penalty does not remove the protection it was standing in for",
        "D-C4": "the two orphaned parts are retired rather than left declared, "
                "the same cascade whale-transfer-reader and social-sentiment-"
                "reader took when their last consumer stopped reading them",
    },
    "origin": "user instruction, 2026-09-05: crypto-only parts are replaced with Indian ones",
    "applied_by": ("dashboard/blueprint_edits/"
                   "apply_2026-09-05_carry_replaces_funding.py"),
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"retired: {', '.join(RETIRED)} + types {', '.join(RETIRED_TYPES)}")
print(f"total: {len(d['features'])} features, {len(d['data_types'])} data types")
