#!/usr/bin/env python3
"""capital-allotment-reader reads main-account-setting, so the tighter cap actually binds.

Found live 2026-08-30, at the user's report that the dashboard's capital-per-
trade controls were not being followed: main-account.toml's own
maximum_capital_per_trade (a separate, independently-editable setting from
the segment's own maximum_capital_per_trade in settings/segments/<segment>.toml)
was read by main-account-settings-reader and published, but nothing ever
consumed it -- grep for MainAccountSetting.maximum_capital_per_trade usage
outside its own reader turned up nothing. Only the segment's own ceiling was
ever enforced by trade-capital-bounds-gate, despite main-account.toml's own
note claiming "the two are checked against each other, and the tighter one
binds." That reconciliation did not exist anywhere in code.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

CONSUMER_ID = "capital-allotment-reader"
NEW_EDGE = "main-account-setting"
if CONSUMER_ID not in features:
    raise SystemExit(f"{CONSUMER_ID} is not in the registry -- nothing to wire this into")
if NEW_EDGE not in types:
    raise SystemExit(f"{NEW_EDGE} is not a declared data type -- would be a dangling edge")
consumer = features[CONSUMER_ID]
if NEW_EDGE in consumer["produces"]:
    raise SystemExit(f"{CONSUMER_ID} already produces {NEW_EDGE} -- a self-edge is not a wire")
if NEW_EDGE not in consumer["consumes"]:
    consumer["consumes"].append(NEW_EDGE)
    consumer.setdefault("edited", []).append(
        {"on": "2026-08-30", "added": {"consumes": [NEW_EDGE]}, "origin": "proposed"}
    )

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{CONSUMER_ID} consumes {NEW_EDGE}: {NEW_EDGE in consumer['consumes']}")
