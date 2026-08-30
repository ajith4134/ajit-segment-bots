#!/usr/bin/env python3
"""position-recorder journals stop-adjustment, so the board's trailing column has data.

Found live 2026-08-30 at the user's report that the trailing column never
shows a locked/activated profit: `stop-adjustment` (profit-lock's trailing
stop) was never on any recorder's consumes, so the journal held no record of
a lock ever forming and the board's column could only ever render "not
built".

`stop-adjustment` is one wire carrying two shapes (exit-order-chainer's
initial exits and profit-lock's trailing lock, per
parts/paper_live_trading/stop_order_manager.py's own read_adjustment); only
profit-lock's shape is journaled here, discriminated defensively rather than
assumed.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
types = {data_type["id"]: data_type for data_type in registry["data_types"]}

CONSUMER_ID = "position-recorder"
NEW_EDGE = "stop-adjustment"
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
