#!/usr/bin/env python3
"""The premium both venues already send reaches the part that forecasts from it.

Proposed by Claude 2026-08-28, after tracing why the tailgating bot has never
opened a position.
Rationale: docs/proposals/the-premium-both-venues-send-and-nobody-reads.md
Idempotent.

`funding-rate-forecaster` has received 5,017,806 market-data messages and made
zero forecasts, with zero premium observations and zero refusals -- it never
reaches the code that would refuse, because a premium is mark minus index and
neither is on `market-data`. Six parts consume `funding-forecast` and none has
ever seen one.

`VenueAdapter.read_premiums` is implemented on both venues and called by nothing.
This adds the part that calls it and the data type it produces: one new part of
the same shape as the three stream readers already in this block, one new data
type, and one consume on the forecaster. No part loses a produces or changes
state.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-premium-both-venues-send-and-nobody-reads.md"

READER = "venue-premium-stream-reader"
FORECASTER = "funding-rate-forecaster"
PREMIUM = "venue-premium"

# Modelled on venue-quote-stream-reader, which reads the same plan off the same
# venues and differs only in which message it keeps.
THE_PART = {
    "id": READER,
    "name": "Venue premium stream reader",
    "role": "read one venue's premium stream",
    "category": "market-data-feed",
    "consumes": ["stream-plan", "venue-standing"],
    "produces": [PREMIUM, "part-health"],
    "switchable": True,
    "off_releases_resources": True,
    "states": ["off", "on"],
    "origin": "proposed",
    "evidence": PROPOSAL,
    "resource_class": "bandwidth-bound",
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}

THE_DATA_TYPE = {
    "id": PREMIUM,
    "name": "venue premium",
    "description": (
        "One symbol's mark price and index price on one venue, with the funding rate the "
        "venue has declared for the next settlement and the moment it settles. The premium "
        "is mark minus index as a fraction of the index, and it is what a funding rate is "
        "averaged from -- so it is a different fact from the rate itself, which the venue "
        "states about a settlement that has not happened yet."
    ),
    "origin": "proposed",
    "added": "2026-08-28",
}

registry = json.loads(REGISTRY.read_text())
features = registry["features"]
data_types = registry["data_types"]
by_id = {feature["id"]: feature for feature in features}

if FORECASTER not in by_id:
    raise SystemExit(
        f"the registry has no {FORECASTER}; this edit names a part that does not "
        f"exist, so amend {PROPOSAL}"
    )

added_part = added_type = added_edge = 0

# 1. The data type first: R-01 computes edges from consumes/produces, so a
#    produces naming a type the registry does not define is a dangling edge.
if not any(entry["id"] == PREMIUM for entry in data_types):
    # Appended, never re-sorted: sorting the list rewrites 279 entries that this
    # edit did not touch, and a diff of a thousand lines hides the three it did.
    data_types.append(THE_DATA_TYPE)
    added_type = 1

# 2. The producer. Added before the consume, so the registry is never in a state
#    where the forecaster reads a type nothing produces.
if READER not in by_id:
    features.append(dict(THE_PART))
    added_part = 1
else:
    existing = by_id[READER]
    for field, value in THE_PART.items():
        if existing.get(field) != value:
            raise SystemExit(
                f"{READER} already exists with a different {field}: "
                f"{existing.get(field)!r} rather than {value!r}. This edit will not "
                f"overwrite a part somebody else defined."
            )

# 3. The consume.
forecaster = by_id[FORECASTER]
if PREMIUM not in forecaster["consumes"]:
    forecaster["consumes"].append(PREMIUM)
    added_edge = 1

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")

print(f"{READER:34} added as a part      {added_part}")
print(f"{PREMIUM:34} added as a data type {added_type}")
print(f"{FORECASTER:34} consumes {PREMIUM}   {added_edge}")
print(
    "\nnothing else changed. Run dashboard/check_contracts.py -- a part with no "
    "source file yet is DECLARED, which is the rung this edit puts it on."
)
