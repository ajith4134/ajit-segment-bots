#!/usr/bin/env python3
"""Rename usdt-pnl-accountant and usdt-pnl-statement to their rupee equivalents.

docs/proposals/the-settlement-currency-is-the-rupee.md: USDT is a dollar
stablecoin that settles crypto perpetuals, and no NSE trade has ever settled in
one. Every segment on this spine states `quote_currency = "INR"`.

Not only a naming problem. `reward-shaper` refuses a trade with
`no-usdt-denominated-result-for-this-trade`, so on this market it refuses every
trade, silently, with a reason that reads sensible.

One part and one data type, renamed together with all four consumers -- a
partial rename fails `check_contracts.py`, which is why this is a blueprint edit
and not a find-and-replace.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-settlement-currency-is-the-rupee.md"

OLD_PART, NEW_PART = "usdt-pnl-accountant", "inr-pnl-accountant"
OLD_TYPE, NEW_TYPE = "usdt-pnl-statement", "inr-pnl-statement"

d = json.loads(REG.read_text())

if not any(f["id"] == OLD_PART for f in d["features"]) and any(
    f["id"] == NEW_PART for f in d["features"]
):
    print("already applied: the settlement currency is already the rupee")
    raise SystemExit(0)

renamed_types = 0
for data_type in d["data_types"]:
    if data_type["id"] == OLD_TYPE:
        data_type["id"] = NEW_TYPE
        if "name" in data_type:
            data_type["name"] = data_type["name"].replace("USDT", "INR").replace("usdt", "inr")
        for field in ("description", "role", "purpose"):
            if field in data_type and isinstance(data_type[field], str):
                data_type[field] = (
                    data_type[field].replace("USDT", "INR").replace("usdt", "inr")
                )
        data_type["evidence"] = PROPOSAL
        renamed_types += 1

renamed_parts = 0
rewired = 0
for feature in d["features"]:
    if feature["id"] == OLD_PART:
        feature["id"] = NEW_PART
        for field in ("name", "role"):
            if field in feature and isinstance(feature[field], str):
                feature[field] = (
                    feature[field].replace("USDT", "INR").replace("usdt", "inr")
                )
        feature["evidence"] = PROPOSAL
        renamed_parts += 1
    for wire in ("consumes", "produces"):
        if OLD_TYPE in feature.get(wire, []):
            feature[wire] = [
                NEW_TYPE if name == OLD_TYPE else name for name in feature[wire]
            ]
            rewired += 1

REG.write_text(json.dumps(d, indent=1) + "\n")
print(
    f"renamed {renamed_parts} part and {renamed_types} data type; "
    f"rewired {rewired} consume/produce list(s)"
)
