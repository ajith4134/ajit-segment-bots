#!/usr/bin/env python3
"""Add signal-excursion-profiler and signal-horizon-profiler.

Proposed by Claude 2026-08-23, agreed by the user the same day.
Rationale: docs/proposals/live-excursion-and-horizon-profiling.md. Idempotent.

Why the blueprint has to change at all: the bull bot's conviction model became
trained on the live spine at 05:35 on 2026-08-23 -- 102 labelled outcomes, 52
right and 50 wrong -- and still formed no opinion, because
`bull-exit-plan-proposer` refuses without a fitted excursion profile and a fitted
horizon profile, and both come from parts that consume `closed-trade`. A closed
trade needs an intent, which needs an exit plan, which needs the profiles. That is
the same cycle `signal-outcome-labeller` was created to break, one layer along.

Measured, not inferred: 9 900 entry candidates in the first 27 minutes of that run
and zero trade intents.

Both parts read `training-label` rather than the feed. The labeller already tracks
each claim's best favourable and worst adverse excursion on every tick and already
carries how long the claim took to resolve; what was missing is that those numbers
were discarded when the claim settled. Three parts each re-tracking the same claims
would be three times the labeller's hot loop -- indexed by symbol precisely because
at the 5 000-claim bound and 285 trades a second a linear scan is over a million
comparisons a second -- to compute the same numbers three times.

They read the live feed by way of live claims, never a replay (RL-071).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/live-excursion-and-horizon-profiling.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

PARTS = [
    {
        "id": "signal-excursion-profiler",
        "name": "Signal excursion profiler",
        "role": (
            "profile how far price travels around a detector's claim before the market "
            "settles it"
        ),
        "category": "learning-loop",
        "consumes": ["training-label"],
        "produces": ["excursion-profile", "part-health"],
        "switchable": True,
        "off_releases_resources": True,
        "states": ["off", "on"],
        "origin": "proposed",
        "evidence": PROPOSAL,
        "resource_class": "compute-bound",
        # A quantile over a rolling window is not damaged by arriving late; it is
        # damaged by being taken over fewer observations than it needs, which the
        # part refuses on its own. A skipped tick delays the profile by one label.
        "rate_risk": "latency-only",
        "skipped_tick_effect": "delays",
    },
    {
        "id": "signal-horizon-profiler",
        "name": "Signal horizon profiler",
        "role": "measure how long each detector's claims take the market to settle",
        "category": "learning-loop",
        "consumes": ["training-label"],
        "produces": ["horizon-profile", "part-health"],
        "switchable": True,
        "off_releases_resources": True,
        "states": ["off", "on"],
        "origin": "proposed",
        "evidence": PROPOSAL,
        "resource_class": "compute-bound",
        "rate_risk": "latency-only",
        "skipped_tick_effect": "delays",
    },
]

for part in PARTS:
    if part["id"] in features:
        features[part["id"]].update(part)
    else:
        registry["features"].append(part)

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(
    f"{', '.join(part['id'] for part in PARTS)}: present, "
    f"{len(registry['features'])} features in the blueprint"
)
