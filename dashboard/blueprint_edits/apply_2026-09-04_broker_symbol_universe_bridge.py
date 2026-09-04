#!/usr/bin/env python3
"""Declare broker-symbol-universe-bridge.

docs/proposals/broker-symbol-universe-bridge.md: republishes Upstox's own
instrument master as symbol-universe -- the crypto-era type eleven parts
consume and which no running part has produced since symbol-catalogue-reader
came off the spine in the 2026-09-02 cutover. The missing fifth bridge,
alongside broker-market-data-bridge, broker-order-book-bridge,
broker-underlying-price-frame-bridge and broker-candle-bridge.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-04"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-symbol-universe-bridge.md"
cid = "market-data-feed"

f = {
    "id": "broker-symbol-universe-bridge",
    "name": "Broker symbol universe bridge",
    "role": "republish a broker's instrument master as symbol-universe",
    "category": cid,
    "consumes": ["broker-instrument-listing", "broker-price-frame"],
    "produces": ["symbol-universe", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if f["id"] in feats:
    feats[f["id"]].update(f)
else:
    d["features"].append(f)
    feats[f["id"]] = f

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-04_broker_symbol_universe_bridge"] = {
    "what": (
        "broker-symbol-universe-bridge -- republishes Upstox's instrument "
        "master as symbol-universe, the type universal-symbol-sweeper, both "
        "feature builders, feed-coverage-auditor, options-flow-reader, "
        "instrument-selector and tick-size-resolver read and which nothing "
        "has produced since the crypto cutover. Measured 2026-09-04: the "
        "sweeper had run 3,114 sweeps over an empty list with every skip "
        "counter at 0, and the whole scanning chain below it was at zero. "
        "Publishes the tracked index underlyings plus the nearest expiry's "
        "option contracts ranked by distance from the underlying's own last "
        "price, capped per underlying -- the cap honours the operator's "
        "standing ruling on captured_symbol_count (2026-08-25, 'hold at 50 "
        "and stop walking'), whose reason is that the pair scanner fails as "
        "the square of the universe. symbol is trading_symbol, matching every "
        "other bridge, because the sweeper keys its universe (venue_id, "
        "symbol) and could not otherwise match a universe entry to a price."
    ),
    "origin": "designed by Claude, user asked for the symbol-universe bridge 2026-09-04",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-04_broker_symbol_universe_bridge.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
