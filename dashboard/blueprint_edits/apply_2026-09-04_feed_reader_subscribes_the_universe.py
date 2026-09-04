#!/usr/bin/env python3
"""broker-market-feed-reader consumes symbol-universe.

docs/proposals/broker-symbol-universe-bridge.md: the feed reader chose what to
subscribe from broker-instrument-listing alone, and that is a race it loses --
broker-instrument-catalogue-reader restates 102,940 listings into an inbox
holding a few hundred messages, and the three index underlyings the segment is
entirely about were not among the 14,560 it received. symbol-universe is the
bounded, already-selected set; subscribing it first makes them certain.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-04"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PART = "broker-market-feed-reader"
NEW_INPUT = "symbol-universe"

f = feats[PART]
if NEW_INPUT not in f["consumes"]:
    f["consumes"] = sorted(set(f["consumes"]) | {NEW_INPUT})

cid = f["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-04_feed_reader_subscribes_the_universe"] = {
    "what": (
        "broker-market-feed-reader gains symbol-universe as an input and "
        "subscribes it ahead of whatever the instrument-catalogue race "
        "delivered. prioritize_index_option_chain could only ever promote "
        "listings that had arrived, and after the per-tick drain fix raised "
        "intake from 1,067 to 14,560 of 102,940 the three index underlyings "
        "still were not among them. The catalogue still fills the rest of the "
        "connection behind the universe, so this adds a guarantee about what is "
        "definitely subscribed rather than narrowing what the tape records. "
        "CapturableSymbol carries venue_instrument_id for it, because Upstox "
        "streams by instrument_key while every other part names an instrument "
        "by its trading symbol."
    ),
    "origin": "designed by Claude, user asked for it 2026-09-04",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-04_feed_reader_subscribes_the_universe.py",
    "proposal": "docs/proposals/broker-symbol-universe-bridge.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{PART} consumes: {feats[PART]['consumes']}")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories")
