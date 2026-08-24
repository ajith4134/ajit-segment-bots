#!/usr/bin/env python3
"""Add price-level-sampler and symbol-price-frame, and move 37 parts onto it.

Proposed by Claude 2026-08-24, agreed by the user the same day.
Rationale: docs/proposals/sampled-price-levels-and-a-governor-that-acts.md.
Idempotent.

Why the blueprint has to change at all: the feed is not what stops this system
covering more symbols -- 193 prints a second is nothing. What stops it is that
every one of those prints is handed to 66 parts, which is 12 707 deliveries a
second measured on 2026-08-23, and that number grows with trading volume, which is
exactly what grows when the universe grows. Forty of the 66 read nothing from a
trade but the symbol, the price and the moment it printed. They are being handed
nine million trades a day to learn one number per symbol.

One part computes that number once and publishes it as one frame per venue per
tick. Fan-out stops depending on volume or on universe size: 37 parts at four
frames a second is 148 deliveries a second whatever the universe does.

Three parts that read only the price nonetheless keep the print, and counting
fields could never have found them -- what separates them is what they do with it.
paper-fill-simulator and paper-liquidation-simulator trigger on the market
touching a level, and a wick that arrives between frames must not be a wick that
never happened; a paper fill kinder than the venue is the one direction a
simulator must never err in. peak-excursion-tracker's whole output is the extreme,
and four samples a second on a symbol printing nine times a second would report
the best and worst of the samples rather than of the market -- with stop placement
learned from the difference.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/sampled-price-levels-and-a-governor-that-acts.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

DATA_TYPE = {
    "id": "symbol-price-frame",
    "name": "symbol price frame",
    "description": (
        "Every symbol's latest price on one venue, each with the moment it printed, "
        "published on a fixed cadence rather than per trade."
    ),
    "origin": "proposed",
    "added": "2026-08-24",
}

SAMPLER = {
    "id": "price-level-sampler",
    "name": "Price level sampler",
    # One responsibility, stated as one (T-6): publish the levels. Holding them is
    # how it does that, not a second thing it does -- the first wording said "hold
    # ... and publish ...", and the contract checker read that as two parts welded
    # together, which is exactly what it is there to catch.
    "role": "publish every symbol's latest price for one venue on a fixed cadence",
    "category": "market-data-feed",
    "consumes": ["market-data"],
    "produces": ["symbol-price-frame", "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound",
    # The cadence is what every reader's staleness bound is measured against, so a
    # frame published late is a frame whose levels are older than the readers were
    # told to expect. Slowing this part changes what the parts downstream believe.
    "rate_risk": "changes-the-answer",
    # A skipped tick is one frame not sent. Every level in the next frame is the
    # current one -- nothing accumulates and nothing is lost, because a level is
    # what is true now rather than an event that happened once.
    "skipped_tick_effect": "delays",
}

# Read only venue_id, symbol, price and venue_time_ns: a level, not a print.
MOVED_TO_THE_FRAME = [
    "bear-entry-timer",
    "bear-exit-plan-proposer",
    "bear-feature-builder",
    "bull-entry-timer",
    "bull-exit-plan-proposer",
    "bull-feature-builder",
    "cointegration-pair-finder",
    "copy-latency-estimator",
    "correlation-cluster-mapper",
    "counterfactual-replayer",
    "cross-segment-signal-bridge",
    "forecast-scorer",
    "funding-skew-detector",
    "instrument-selector",
    "intent-timing-gate",
    "liquidation-cascade-detector",
    "liquidation-price-tracker",
    "margin-liquidation-watch",
    "mean-reversion-detector",
    "momentum-burst-detector",
    "near-miss-recorder",
    "paper-currency-converter",
    "profit-lock",
    "regime-break-detector",
    "regime-classifier",
    "resting-order-cancel-policy",
    "sentiment-shift-detector",
    "shortfall-decomposer",
    "signal-outcome-labeller",
    "spread-reversion-detector",
    "tail-move-remaining-estimator",
    "tail-mover-qualifier",
    "tail-trailing-exit-planner",
    "turbulence-index-gauge",
    "universal-symbol-sweeper",
    "venue-outage-rider",
    "whale-flow-detector",
]

# Read only the price too, and keep the print anyway. See the docstring.
KEEPS_THE_PRINT_FOR_A_REASON_COUNTING_FIELDS_CANNOT_SEE = [
    "paper-fill-simulator",
    "paper-liquidation-simulator",
    "peak-excursion-tracker",
]

changed = []

data_types = registry["data_types"]
if not any(entry["id"] == DATA_TYPE["id"] for entry in data_types):
    data_types.append(DATA_TYPE)
    changed.append(f"data type {DATA_TYPE['id']} added")

if SAMPLER["id"] not in features:
    registry["features"].append(SAMPLER)
    features[SAMPLER["id"]] = SAMPLER
    changed.append(f"part {SAMPLER['id']} added")

for part_id in MOVED_TO_THE_FRAME:
    feature = features.get(part_id)
    if feature is None:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")
    consumes = list(feature["consumes"])
    if "market-data" in consumes:
        consumes[consumes.index("market-data")] = "symbol-price-frame"
        # A part that already read the frame for another reason must not read it twice.
        feature["consumes"] = sorted(set(consumes))
        changed.append(f"{part_id}: market-data -> symbol-price-frame")

for part_id in KEEPS_THE_PRINT_FOR_A_REASON_COUNTING_FIELDS_CANNOT_SEE:
    feature = features.get(part_id)
    if feature is None:
        raise SystemExit(f"{part_id} is not in the registry; this edit is out of date")
    if "market-data" not in feature["consumes"]:
        raise SystemExit(
            f"{part_id} no longer consumes market-data. It triggers on the market touching "
            f"a level, or its output is an extreme; a sampled feed changes what it means. "
            f"Restore it deliberately or amend {PROPOSAL}."
        )

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
