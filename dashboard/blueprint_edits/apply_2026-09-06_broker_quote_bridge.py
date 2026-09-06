#!/usr/bin/env python3
"""Declare broker-quote-bridge.

docs/proposals/broker-quote-bridge.md: `market-quote` has exactly one producer
in the blueprint -- `venue-quote-stream-reader`, which is crypto and off -- so
`quote-level-sampler` has never received anything, `symbol-quote-frame` has
never been produced, and both of its readers are cut off. One of them is
`instrument-selector`, for which a quote is the fallback when the last trade is
too old to size against: on the live run of 2026-08-24 it refused 525 of 9,945
intents for a price too old, and an Indian option that has not printed for
minutes while carrying a live bid and ask is the ordinary case.

No new data type and no new subscription: `broker-order-book-snapshot` already
carries Upstox's own bid/ask prices and sizes, and `market-quote` already exists
with a consumer waiting for it. This is the fourth member of the existing bridge
family, not a new feed.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-06"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

cid = "market-data-feed"
PROPOSAL = "docs/proposals/broker-quote-bridge.md"

new_feature = {
    "id": "broker-quote-bridge",
    "name": "Broker quote bridge",
    "role": "republish a broker's own top of book as market-quote",
    "category": cid,
    "consumes": ["broker-subscribed-instrument-listing", "broker-order-book-snapshot"],
    "produces": ["market-quote", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if new_feature["id"] in feats:
    feats[new_feature["id"]].update(new_feature)
else:
    d["features"].append(new_feature)
    feats[new_feature["id"]] = new_feature

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-06_broker_quote_bridge"] = {
    "what": (
        "broker-quote-bridge -- the only producer of `market-quote` in the blueprint "
        "is the crypto `venue-quote-stream-reader`, which is off, so "
        "`quote-level-sampler` has received nothing ever, `symbol-quote-frame` has "
        "never been produced, and `instrument-selector` and "
        "`spread-reversion-detector` are both cut off from it. A quote is "
        "instrument-selector's fallback when the last trade is too old to size "
        "against (its own `priced_from_a_quote` counter), and NormalisedQuote's "
        "docstring records the measurement the type was built for: 525 of 9,945 "
        "intents refused for a price too old on the live run of 2026-08-24. Indian "
        "option chains make that the ordinary case. This reads the same "
        "`broker-order-book-snapshot` that broker-order-book-bridge already reads -- "
        "Upstox's depth levels carry bid/ask price and size -- and states the best "
        "level of it as a quote. No new data type, no new subscription; the fourth "
        "member of the existing bridge family (T-6: grow by adding parts). Separate "
        "from broker-order-book-bridge because the two answer different questions -- "
        "the whole book to walk a fill against, versus the top of it as the price a "
        "symbol may be believed at -- and the governor must be able to shed either "
        "without the other."
    ),
    "origin": (
        "found by Claude walking market-data-feed, the first of the 29 foundational "
        "features under the audit temporary goal of 2026-09-05 "
        "(docs/feature-audit.md); item 5 of that goal asks which part each feature "
        "is missing"
    ),
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-06_broker_quote_bridge.py",
    "proposal": [PROPOSAL],
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{cid}: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(
    f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
    f"{len(d['data_types'])} data types"
)
