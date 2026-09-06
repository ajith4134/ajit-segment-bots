#!/usr/bin/env python3
"""Rewire implied-vol-reader onto the broker's own option chain.

docs/proposals/implied-vol-reader-reads-the-broker-option-chain.md: the part has
read nothing ever (reads 0, quotes_seen 0, surfaces_published 0 on the live
spine 2026-09-06). Its start_part is a deliberate stub whose own docstring cites
RL-050 -- the crypto build order, under which options were a segment this system
had not built. That ordering is retired: docs/goal.md's Phase A is index options
and stock options, both of them, fully, first.

Everything the reader needs is already on the bus -- implied volatility on
broker-option-greeks, strike/expiry/CE-PE/underlying on
broker-subscribed-instrument-listing, a two-sided market on market-quote (from
broker-quote-bridge, built earlier the same day), and the underlying's spot on
symbol-price-frame. Nothing new is fetched or subscribed.

A conversion, not an addition: the reader's core knows nothing about a venue and
every rule in it is what an Indian chain needs. Only its input was crypto-era.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-06"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/implied-vol-reader-reads-the-broker-option-chain.md"

part = feats["implied-vol-reader"]
# Set explicitly and in the order the part's own PART_DECLARATION literal states
# -- PartDeclaration equality compares the consumes tuple in order, and the
# part's blueprint test checks exactly that.
part["consumes"] = [
    "broker-option-greeks",
    "broker-subscribed-instrument-listing",
    "market-quote",
    "symbol-price-frame",
]
rewiring = {"on": TODAY, "why": PROPOSAL}
if rewiring not in part.setdefault("rewired", []):
    part["rewired"].append(rewiring)

cid = part["category"]
c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-06_implied_vol_reader_reads_the_broker_chain"] = {
    "what": (
        "implied-vol-reader stops draining `market-data` -- which never carried an "
        "options quote -- and reads the broker's own chain instead: implied "
        "volatility from broker-option-greeks, strike/expiry/CE-PE/underlying from "
        "broker-subscribed-instrument-listing, a two-sided market from market-quote, "
        "and the underlying's spot from symbol-price-frame. All four already carry. "
        "The part had reads 0 / quotes_seen 0 / surfaces_published 0 for its whole "
        "life because start_part was a stub citing RL-050, the crypto build order "
        "under which options were unbuilt; Phase A is now both options segments "
        "first. instrument-selector and volatility-gap-detector both consume "
        "implied-vol-surface, and the detector was refusing on a missing surface on "
        "every one of 2,672 tests. The conversion also fixes an append-only quote "
        "list that was harmless while the part received nothing and would have grown "
        "without bound on a live chain -- a quote is a level and a contract's newer "
        "quote supersedes its older one."
    ),
    "origin": (
        "found by Claude walking execution-venue-adapter and paper-live-trading, "
        "features 3 and 4 of the 29 under the audit temporary goal of 2026-09-05 "
        "(docs/feature-audit.md); item 3 of that goal is convert or replace, never "
        "leave in place"
    ),
    "applied_by": (
        "dashboard/blueprint_edits/apply_2026-09-06_implied_vol_reader_reads_the_broker_chain.py"
    ),
    "proposal": [PROPOSAL],
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("implied-vol-reader consumes:", part["consumes"])
print(
    f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
    f"{len(d['data_types'])} data types"
)
