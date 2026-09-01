#!/usr/bin/env python3
"""Redirect exchange-announcement-reader from symbol-universe to
broker-instrument-listing.

docs/proposals/exchange-announcement-reader-broker-listing.md: this part
was never crypto-only in concept, only its one input was. No feed was ever
connected on this box, crypto or Indian -- this makes it Indian-ready in
the same honest "path exists, not fed" state.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PART_ID = "exchange-announcement-reader"
feats[PART_ID]["consumes"] = ["broker-instrument-listing"]

cid = feats[PART_ID]["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_exchange_announcement_reader_broker_listing"] = {
    "what": (
        "exchange-announcement-reader redirected from symbol-universe to "
        "broker-instrument-listing -- not crypto-only in concept, only its "
        "universe input was."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_exchange_announcement_reader_broker_listing.py",
    "proposal": "docs/proposals/exchange-announcement-reader-broker-listing.md",
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes -> {feats[PART_ID]['consumes']}")
