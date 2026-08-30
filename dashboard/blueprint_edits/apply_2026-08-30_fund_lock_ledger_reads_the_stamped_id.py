#!/usr/bin/env python3
"""fund-lock-ledger reads stamped-order, so a release can find the lock it took.

Found live 2026-08-30 auditing why almost no trade opened after the first
handful: fund-lock-ledger locked capital under bounded-order.intent_id but
released it looking up fill.order_id -- order-idempotency-stamper's SHA-256
of that same intent_id, an unrelated-looking string. Every lock leaked
permanently; free_balance only ever fell and had gone negative inside half
an hour on the live spine. stamped-order carries both ids together, which is
what a release needs to translate fill.order_id back to the intent_id it
was actually locked under.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

CONSUMER_ID = "fund-lock-ledger"
NEW_EDGE = "stamped-order"
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
