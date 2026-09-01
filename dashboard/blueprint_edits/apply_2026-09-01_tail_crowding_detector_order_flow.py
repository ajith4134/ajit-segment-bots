#!/usr/bin/env python3
"""Redirect tail-crowding-detector off funding-forecast/sentiment-reading,
trim idea-generator's dead sentiment-reading consume, retire
social-sentiment-reader now that nothing reads its type.

docs/proposals/tail-crowding-detector-order-flow.md.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

feats["tail-crowding-detector"]["consumes"] = [
    "follow-candidate", "order-book-snapshot", "broker-instrument-listing",
    "broker-open-interest",
]
feats["idea-generator"]["consumes"] = [
    c for c in feats["idea-generator"]["consumes"] if c != "sentiment-reading"
]

SENTIMENT_READER_ID = "social-sentiment-reader"
sentiment_consumers = [
    f["id"] for f in d["features"]
    if f["id"] != SENTIMENT_READER_ID and "sentiment-reading" in f.get("consumes", [])
]
assert not sentiment_consumers, f"sentiment-reading still has real consumers: {sentiment_consumers}"
d["features"] = [f for f in d["features"] if f["id"] != SENTIMENT_READER_ID]

# ------------------------------------------------- recompute affected blocks' contracts
for cid in {"profit-tailgating-bot", "intelligence", "online-research"}:
    c = next(cat for cat in d["categories"] if cat["id"] == cid)
    parts = [f for f in d["features"] if f["category"] == cid]
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_tail_crowding_detector_order_flow"] = {
    "what": (
        "tail-crowding-detector redirected from funding-forecast/"
        "sentiment-reading to broker-instrument-listing + broker-open-interest "
        "(FROM_ORDER_FLOW replaces FROM_FUNDING's role, sentiment retired with "
        "no replacement). idea-generator's dead sentiment-reading consume "
        "trimmed. social-sentiment-reader retired -- nothing reads its type."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_tail_crowding_detector_order_flow.py",
    "proposal": "docs/proposals/tail-crowding-detector-order-flow.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"tail-crowding-detector: consumes -> {feats['tail-crowding-detector']['consumes']}")
print(f"idea-generator: consumes -> {feats['idea-generator']['consumes']}")
print(f"removed: {SENTIMENT_READER_ID}")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
