#!/usr/bin/env python3
"""The funding-formula facts symbol-catalogue-reader already fetches reach the forecaster.

Proposed by Claude 2026-08-29, while fixing why 100% of every bull and bear
feature vector has ever been incomplete.
Rationale: docs/proposals/the-forecaster-reads-the-caps-it-already-receives.md
Idempotent.

`funding-rate-forecaster.observe_venue_parameters` (renamed `observe_funding_
parameters`, now per (venue, symbol) rather than per venue) was called nowhere
in the codebase, and its data type was not declared on the part's own
`consumes`. Every forecast refused `NO_SYMBOL_PARAMETERS`; `funding_forecast_
change` was missing on 129/129 bull and 186/186 bear feature vectors.

Both venue adapters already fetch cap, floor and interest rate in the same
round-trip they use for the rate and interval `venue-declared-funding-facts.md`
already wired onto `symbol-universe` -- they were being parsed and then
dropped. This adds one consume edge. No new part, no new data type, no part
loses a produces.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-forecaster-reads-the-caps-it-already-receives.md"

FORECASTER = "funding-rate-forecaster"
SYMBOL_UNIVERSE = "symbol-universe"

registry = json.loads(REGISTRY.read_text())
features = registry["features"]
data_types = registry["data_types"]
by_id = {feature["id"]: feature for feature in features}

if FORECASTER not in by_id:
    raise SystemExit(
        f"the registry has no {FORECASTER}; this edit names a part that does not "
        f"exist, so amend {PROPOSAL}"
    )
if not any(entry["id"] == SYMBOL_UNIVERSE for entry in data_types):
    raise SystemExit(
        f"the registry has no data type {SYMBOL_UNIVERSE}; this edit assumes it "
        f"already exists (it does, for tick-size-resolver and instrument-selector), "
        f"so amend {PROPOSAL}"
    )

forecaster = by_id[FORECASTER]
added_edge = 0
if SYMBOL_UNIVERSE not in forecaster["consumes"]:
    forecaster["consumes"].append(SYMBOL_UNIVERSE)
    added_edge = 1

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")

print(f"{FORECASTER:24} consumes {SYMBOL_UNIVERSE}   {added_edge}")
print("\nnothing else changed. Run dashboard/check_contracts.py to confirm.")
