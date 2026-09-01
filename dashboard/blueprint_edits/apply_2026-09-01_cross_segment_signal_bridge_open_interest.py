#!/usr/bin/env python3
"""Redirect cross-segment-signal-bridge off whale-transfer/funding-forecast,
and retire whale-transfer-reader now that nothing else consumes its type.

docs/proposals/cross-segment-signal-bridge-open-interest.md: WHALE_FLOW and
FUNDING_SKEW retired (no Indian equivalent); OPEN_INTEREST_SURGE replaces
FUNDING_SKEW's role using real broker-open-interest data. The segment
vocabulary moves from the old 3 crypto segments (futures/spot/options) to
the real 6 (index-options/stock-options/index-futures/stock-futures/
commodities/cash-equity).

whale-transfer-reader's only consumer was cross-segment-signal-bridge; once
this edit lands, nothing reads whale-transfer at all -- the cascade this
project always traces before removing a part (docs/proposals/options-
scanner-first-slice.md's own precedent). The whale-transfer data TYPE stays
declared, undeleted, matching how onchain-flow was left in place when
onchain-flow-aggregator retired.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

BRIDGE_ID = "cross-segment-signal-bridge"
feats[BRIDGE_ID]["consumes"] = [
    "broker-instrument-listing", "broker-open-interest", "symbol-price-frame", "position",
]

WHALE_READER_ID = "whale-transfer-reader"
whale_consumers = [
    f["id"] for f in d["features"]
    if f["id"] != WHALE_READER_ID and "whale-transfer" in f.get("consumes", [])
]
assert not whale_consumers, f"whale-transfer still has real consumers: {whale_consumers}"
d["features"] = [f for f in d["features"] if f["id"] != WHALE_READER_ID]

# ------------------------------------------------- recompute affected blocks' contracts
for cid in {"intelligence", "online-research"}:
    c = next(cat for cat in d["categories"] if cat["id"] == cid)
    parts = [f for f in d["features"] if f["category"] == cid]
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_cross_segment_signal_bridge_open_interest"] = {
    "what": (
        "cross-segment-signal-bridge redirected from whale-transfer/"
        "funding-forecast to broker-instrument-listing + broker-open-interest "
        "(OPEN_INTEREST_SURGE replaces FUNDING_SKEW's role), segment "
        "vocabulary moved from 3 crypto segments to the real 6. "
        "whale-transfer-reader retired -- its only consumer just stopped "
        "reading whale-transfer."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_cross_segment_signal_bridge_open_interest.py",
    "proposal": "docs/proposals/cross-segment-signal-bridge-open-interest.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{BRIDGE_ID}: consumes -> {feats[BRIDGE_ID]['consumes']}")
print(f"removed: {WHALE_READER_ID}")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
