#!/usr/bin/env python3
"""Ten parts read the instrument master to resolve what the feed delivers.

docs/proposals/subscribed-instrument-listing-filter.md: a listing for an
instrument the feed is not subscribed to carries no price, no greek, no open
interest and no depth, so ten of the fifteen consumers of
broker-instrument-listing can never use one. They are drained at the full
restatement rate of all 102,940 rows against 2,000 subscribed, and the conveyor
is dropping 13.6% of its deliveries (measured live 2026-09-05).

Declares broker-subscription-state (what the feed reader actually subscribed),
subscribed-instrument-listing-filter (holds the master, emits the subscribed
subset on its own conveyor) and broker-subscribed-instrument-listing, and moves
the ten onto it. Five parts stay on the master deliberately -- the proposal says
which and why.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"
PROPOSAL = "docs/proposals/subscribed-instrument-listing-filter.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}


# ------------------------------------------------------------------ the types

NEW_TYPES = [
    ("broker-subscription-state", "broker subscription state",
     "The instrument keys a broker feed reader currently has subscribed, and "
     "when it looked. A level: true until the subscription set changes. Stated "
     "by the part that holds the connection rather than re-derived from "
     "symbol-universe and the nearest-expiry rule, which is what that part uses "
     "to choose -- a second derivation would be free to disagree with the real "
     "subscription (Rule 8)."),
    ("broker-subscribed-instrument-listing", "broker subscribed instrument listing",
     "One contract as the broker's instrument master lists it, narrowed to the "
     "instruments the feed is actually subscribed to. The same payload as "
     "broker-instrument-listing and a separate wire on purpose: everything that "
     "joins a listing to an LTP, a greek, an open-interest reading or a depth "
     "snapshot can only use a subscribed instrument, and the master is fifty "
     "times larger than the subscription."),
]

types_by_id = {t["id"]: t for t in d["data_types"]}
for type_id, name, description in NEW_TYPES:
    entry = {"id": type_id, "name": name, "description": description,
             "origin": "proposed", "proposed": TODAY, "evidence": PROPOSAL}
    if type_id in types_by_id:
        types_by_id[type_id].update(entry)
    else:
        d["data_types"].append(entry)
        types_by_id[type_id] = entry


# ------------------------------------------------------------------- the part

FILTER = {
    "id": "subscribed-instrument-listing-filter",
    "name": "Subscribed instrument listing filter",
    "role": "restate the instrument master's rows for the instruments the feed is subscribed to",
    "category": "broker-adapter",
    "consumes": ["broker-instrument-listing", "broker-subscription-state"],
    "produces": ["broker-subscribed-instrument-listing", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if FILTER["id"] in feats:
    feats[FILTER["id"]].update(FILTER)
else:
    d["features"].append(FILTER)
    feats[FILTER["id"]] = FILTER


# ------------------------------------------------------------------ the wiring

def swap_input(part_id, old_type, new_type):
    """Read the narrowed type where the wide one was read, in the same place.

    In place rather than drop-then-append-then-sort: a part's own
    PART_DECLARATION must equal this list as a tuple, order included
    (`load_declaration_from_blueprint`), so reordering the blueprint would make
    ten declarations need rewriting to say exactly what they already say.
    """
    part = feats[part_id]
    part["consumes"] = [new_type if t == old_type else t for t in part["consumes"]]
    record = {"on": TODAY, "why": PROPOSAL}
    history = part.setdefault("rewired", [])
    if record not in history:
        history.append(record)


def also_produces(part_id, type_id):
    """Add an output, ahead of part-health.

    Ahead of it because every part in this codebase states part-health last, and
    a part's own PART_DECLARATION must equal this list as a tuple, order
    included -- so appending after it would make the declaration read oddly to
    say exactly the same thing.
    """
    part = feats[part_id]
    if type_id not in part["produces"]:
        produces = list(part["produces"])
        at = produces.index("part-health") if "part-health" in produces else len(produces)
        produces.insert(at, type_id)
        part["produces"] = produces
    record = {"on": TODAY, "why": PROPOSAL}
    history = part.setdefault("rewired", [])
    if record not in history:
        history.append(record)


# The feed reader states what it has subscribed. It keeps reading the master:
# subscribe_the_universe_first fills the rest of the connection from rows it has
# not subscribed yet, so narrowing its own input would be circular.
also_produces("broker-market-feed-reader", "broker-subscription-state")

# Everything that joins a listing to something the feed delivers.
MOVED_TO_THE_SUBSCRIPTION = (
    "broker-market-data-bridge",              # instrument_key -> trading_symbol
    "broker-order-book-bridge",               # the same map, for depth
    "broker-candle-bridge",                   # the same map, for bars
    "broker-underlying-price-frame-bridge",   # tracked underlyings, needs a price
    "bull-feature-builder",                   # UnderlyingOpenInterestAggregator
    "bear-feature-builder",                   # the same aggregator
    "cross-segment-signal-bridge",            # the same aggregator
    "tail-crowding-detector",                 # the same aggregator
    "instrument-selector",                    # AtmStrikeTracker: premium and delta
    "expiry-day-zero-to-hero-detector",       # refuses NO_PREMIUM / NO_DELTA
)
for part_id in MOVED_TO_THE_SUBSCRIPTION:
    swap_input(part_id,
               "broker-instrument-listing",
               "broker-subscribed-instrument-listing")

# Staying on the master, deliberately: broker-symbol-universe-bridge selects the
# universe from it, broker-market-feed-reader subscribes from it,
# corporate-action-adjuster and exchange-announcement-reader are about the
# exchange rather than about this project's subscription, broker-history-reader
# is not on the feed path at all, and broker-news-reader / news-symbol-resolver
# are announcement-shaped and off the spine.


# --------------------------------------------------------- the block contracts

TOUCHED = sorted({FILTER["category"]}
                 | {feats[p]["category"] for p in MOVED_TO_THE_SUBSCRIPTION}
                 | {feats["broker-market-feed-reader"]["category"]})
for category_id in TOUCHED:
    category = next(c for c in d["categories"] if c["id"] == category_id)
    parts = [f for f in d["features"] if f["category"] == category_id]
    category["consumes"] = sorted({t for f in parts for t in f["consumes"]})
    category["produces"] = sorted({t for f in parts for t in f["produces"]})
    category["contract_recomputed"] = {
        "on": TODAY, "from": "its parts", "origin": "proposed",
    }


d[f"_proposal_{TODAY}_subscribed_instrument_listing_filter"] = {
    "what": (
        "subscribed-instrument-listing-filter -- holds the broker's instrument "
        "master and restates, on its own conveyor, only the rows for the "
        "instruments broker-market-feed-reader states it has subscribed. Ten of "
        "the fifteen consumers of broker-instrument-listing move onto the "
        "narrowed type; five stay on the master because they select from it, "
        "subscribe from it, or ask about the exchange rather than about this "
        "project's subscription."
    ),
    "why": (
        "Measured on the live spine 2026-09-05, 35 minutes after start: "
        "broker-instrument-catalogue-reader published broker-instrument-listing "
        "571,464 times and 77,477 of those deliveries did not land -- 13.6%, the "
        "second-worst undelivered type on the spine -- while "
        "broker-market-feed-reader reported subscribed_instruments 2,000 against "
        "a 102,940-row master. A listing for an unsubscribed instrument carries "
        "no LTP, no greek, no open interest and no depth, so none of those ten "
        "parts can join it to anything. Named as the outstanding argument by "
        "a8bdbb0, which fixed the master's delivery and did not act on its width."
    ),
    "decisions": {
        "D-S1": "the bound is what the feed subscribed (2,000), not what the "
                "segment trades (symbol-universe, ~153) -- expiry-day-zero-to-"
                "hero-detector exists to find far-OTM contracts an ATM-ranked "
                "universe cap excludes, and not the master (102,940) either",
        "D-S2": "the subscription is stated by the part that owns the "
                "connection, never re-derived from symbol-universe plus the "
                "nearest-expiry rule -- a second derivation may disagree with "
                "the real subscription (Rule 8)",
        "D-S3": "a new part rather than a second audience on the catalogue "
                "reader or the feed reader (T-6); the filter is switchable and "
                "its being off costs the feed nothing (T-2/T-3), and a "
                "restatement loop must not live in the io-bound part whose "
                "skipped_tick_effect is corrupts",
        "D-S4": "the filter holds the whole master and emits the subscribed "
                "subset. Keeping only rows already subscribed when they passed "
                "would make a newly subscribed contract wait a full restatement "
                "cycle -- and expiry-day contracts are subscribed on expiry "
                "morning, exactly when the detector needs them",
    },
    "origin": "designed by Claude, user asked for the blueprint edit 2026-09-05",
    "applied_by": ("dashboard/blueprint_edits/"
                   "apply_2026-09-05_subscribed_instrument_listing_filter.py"),
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"broker-adapter: {len([f for f in d['features'] if f['category'] == 'broker-adapter'])} parts")
print(f"moved to broker-subscribed-instrument-listing: {len(MOVED_TO_THE_SUBSCRIPTION)}")
print(f"still on broker-instrument-listing: "
      f"{len([f for f in d['features'] if 'broker-instrument-listing' in f['consumes']])}")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, "
      f"{len(d['data_types'])} data types")
