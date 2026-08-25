#!/usr/bin/env python3
"""`candle` becomes its own data type, and the book readers read the book.

Proposed by Claude 2026-08-25.
Rationale: docs/proposals/one-wire-three-shapes.md
Idempotent.

`market-data` carried trades, candles and books at once. The minute
ccxt-venue-reader started publishing candles on it, feed-gap-detector -- which
reads a trade's sequence number -- began crashing every two seconds.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/one-wire-three-shapes.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

MARKET_DATA = "market-data"
CANDLE = "candle"
BOOK = "order-book-snapshot"

MOVED_TO_CANDLE_PRODUCES = ("ccxt-venue-reader",)
MOVED_TO_CANDLE_CONSUMES = ("kline-window-builder", "feed-jump-detector", "historical-bar-store")
GAINS_THE_BOOK = ("ground-truth-snapshot-builder", "market-anomaly-detector", "symbol-profile-store")

for part_id in MOVED_TO_CANDLE_PRODUCES + MOVED_TO_CANDLE_CONSUMES + GAINS_THE_BOOK:
    if part_id not in features:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")

if BOOK not in features["order-book-reader"].get("produces", ()):
    raise SystemExit(
        f"order-book-reader no longer produces {BOOK}; amend {PROPOSAL} rather than letting "
        f"this edit create a dangling edge."
    )

changed = []

# The type itself, declared before anything may produce or consume it (R-01).
declared = {entry["id"] for entry in registry["data_types"]}
if CANDLE not in declared:
    registry["data_types"].append({
        "id": CANDLE,
        "name": "candle",
        "description": (
            "One OHLCV bar for one symbol at one interval, with the venue's own flag for "
            "whether the bar is closed. Split from market-data on 2026-08-25: that type "
            "carried trades, candles and books at once, and the minute candles began "
            "flowing on it feed-gap-detector -- which reads a trade's sequence number -- "
            "crashed every two seconds. A candle is a summary of many trades and shares "
            "no field with one."
        ),
    })
    changed.append(f"declared data type {CANDLE}")

# market-data means one trade now, and its description should stop promising a book.
for entry in registry["data_types"]:
    if entry["id"] == MARKET_DATA and "candle" not in entry["description"]:
        entry["description"] = (
            "One trade, as a venue printed it, normalised across venues. Trades only: "
            "candles are `candle` and the book is `order-book-snapshot`, split out on "
            "2026-08-25 after one wire carrying three shapes crashed a reader of one of them."
        )
        changed.append(f"{MARKET_DATA}: description says trades only")

for part_id in MOVED_TO_CANDLE_PRODUCES:
    produces = list(features[part_id]["produces"])
    if MARKET_DATA in produces:
        produces[produces.index(MARKET_DATA)] = CANDLE
        features[part_id]["produces"] = produces
        changed.append(f"{part_id}: produces {CANDLE} instead of {MARKET_DATA}")

for part_id in MOVED_TO_CANDLE_CONSUMES:
    consumes = list(features[part_id]["consumes"])
    if MARKET_DATA in consumes:
        consumes[consumes.index(MARKET_DATA)] = CANDLE
        features[part_id]["consumes"] = consumes
        changed.append(f"{part_id}: consumes {CANDLE} instead of {MARKET_DATA}")

for part_id in GAINS_THE_BOOK:
    consumes = list(features[part_id]["consumes"])
    if BOOK not in consumes:
        features[part_id]["consumes"] = consumes + [BOOK]
        changed.append(f"{part_id}: consumes {BOOK}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
