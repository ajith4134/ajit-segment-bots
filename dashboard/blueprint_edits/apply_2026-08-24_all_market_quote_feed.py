#!/usr/bin/env python3
"""Add the all-market quote feed: two parts, two data types, one new reader.

Proposed by Claude 2026-08-24, agreed by the user the same day, scope "pricing
only" chosen by the user.
Rationale: docs/proposals/an-all-market-quote-is-the-price-a-quiet-symbol-has.md.
Measurements: measurements/2026-08-24-all-market-quotes/.
Idempotent.

Why the blueprint has to change at all: every price this system decides against
comes from a trade, and a symbol that has not traded has no fresh one. Measured
on the live run at 16:2x on 2026-08-24, instrument-selector refused 525 of 9 945
intents for a stale price and exactly zero for never having seen a price. The
refusals are not a coverage gap that a bigger universe would close -- they are
symbols already captured, whose last trade is simply old.

A resting bid and ask is a live fact for a symbol nobody is trading. Both venues
serve the whole universe of it on one connection: Binance `!bookTicker` at 693-768
symbols, Bybit every symbol named explicitly at 833 of 833, none silent.

The reader and the sampler are two parts rather than one because reading a socket
is bandwidth-bound and publishing a cadence is CPU-bound, the governor switches
them separately, and T-6 grows by adding parts. The sampler is not optional:
Bybit's quotes arrive at 1 190 a second against ~224 of trades, so handing them to
readers one at a time would re-create on a louder feed exactly the fan-out that
apply_2026-08-24_sampled_price_levels.py removed from the trade path.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/an-all-market-quote-is-the-price-a-quiet-symbol-has.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}
data_types = registry["data_types"]

MARKET_QUOTE = {
    "id": "market-quote",
    "name": "market quote",
    "description": (
        "One symbol's best bid and ask on one venue, with the size at each and the "
        "moment the venue stamped it. A quote exists whether or not the symbol "
        "traded, which is what separates it from a print."
    ),
    "origin": "proposed",
    "added": "2026-08-24",
}

QUOTE_FRAME = {
    "id": "symbol-quote-frame",
    "name": "symbol quote frame",
    "description": (
        "Every symbol's latest best bid and ask on one venue, each with the moment "
        "the venue stamped it, published on a fixed cadence rather than per quote."
    ),
    "origin": "proposed",
    "added": "2026-08-24",
}

QUOTE_READER = {
    "id": "venue-quote-stream-reader",
    "name": "Venue quote stream reader",
    # One responsibility stated as one (T-6): deliver the venue's quotes. Which
    # socket, which route and how many connections are how it does that.
    "role": "read every symbol's live quote from one venue",
    "category": "market-data-feed",
    "consumes": ["stream-plan", "venue-standing"],
    "produces": ["market-quote", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound",
    # Falling behind the socket means quotes arriving late, and a quote's whole
    # value is that it is current. Late is the same as wrong here.
    "rate_risk": "changes-the-answer",
    # A quote is a level, not an event: the next one carries the current truth and
    # nothing accumulates, so a skipped tick delays rather than loses.
    "skipped_tick_effect": "delays",
}

QUOTE_SAMPLER = {
    "id": "quote-level-sampler",
    "name": "Quote level sampler",
    "role": "publish every symbol's latest quote for one venue on a fixed cadence",
    "category": "market-data-feed",
    "consumes": ["market-quote"],
    "produces": ["symbol-quote-frame", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "compute-bound",
    # Every reader's staleness bound is measured against this cadence, so a frame
    # published late is a frame whose quotes are older than the reader was told.
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}

# The one part whose refusals this exists to answer. Scope is pricing only: the
# selector is where a reference price is chosen and where the refusal is counted,
# so it is the only consumer this edit adds. It keeps symbol-price-frame -- a
# traded price is the better fact when there is a fresh one, and the quote is what
# it falls back to.
GAINS_THE_QUOTE_FRAME = ["instrument-selector"]

changed = []

for data_type in (MARKET_QUOTE, QUOTE_FRAME):
    if not any(entry["id"] == data_type["id"] for entry in data_types):
        data_types.append(data_type)
        changed.append(f"data type {data_type['id']} added")

for part in (QUOTE_READER, QUOTE_SAMPLER):
    if part["id"] not in features:
        registry["features"].append(part)
        features[part["id"]] = part
        changed.append(f"part {part['id']} added")

for part_id in GAINS_THE_QUOTE_FRAME:
    feature = features.get(part_id)
    if feature is None:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")
    if "symbol-price-frame" not in feature["consumes"]:
        raise SystemExit(
            f"{part_id} no longer consumes symbol-price-frame. The quote is the fallback "
            f"for when a traded price is too old, so a part that has stopped reading the "
            f"traded price is not the part this edit was written against. Amend {PROPOSAL}."
        )
    if "symbol-quote-frame" not in feature["consumes"]:
        # Appended rather than sorted: a part's declaration in code carries the
        # order its author wrote, and the two are compared for equality.
        feature["consumes"] = list(feature["consumes"]) + ["symbol-quote-frame"]
        changed.append(f"{part_id}: consumes symbol-quote-frame")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
