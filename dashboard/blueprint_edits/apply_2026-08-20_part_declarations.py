#!/usr/bin/env python3
"""Give every part in docs/features.json its resource class and two throttle facts.

Sections 6 and 7 of docs/superpowers/specs/2026-08-20-part-runtime-design.md, and
docs/proposals/part-declarations.md for the full argument. Three fields land on
every one of the 321 features: resource_class (section 7), and the pair rate_risk
plus skipped_tick_effect that section 6 requires before a part may ever be
admitted to a rate ladder (runtime.part_declaration.may_enter_rate_ladder).

Classification is per part, from the part's own id, not from its blueprint
category -- a category is frequently a mix of shapes (bull-bot holds a feature
builder, a model, a calibrator and a learner in one block), so the id is split on
"-" and its tokens are checked against four ordered vocabularies, most specific
first; the first vocabulary a part's tokens intersect wins, and a part matching
none of the four gets the catch-all. Two ids that carry the "ledger" token but
are active reservation locks rather than passive records are forced to the
catch-all by hand -- see FORCED_CATCH_ALL below and the proposal's "Two forced
exceptions" section.

Idempotent: each of the three fields is set only if the feature does not already
carry it, so a hand-correction made after this script runs -- as a part is
actually built and its real throttle behaviour measured -- is never overwritten
by a second run. Re-running once applied changes nothing.
"""
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"

# Order matters: checked top to bottom, first token intersection wins.
DEFAULTS = [
    (
        "ledger, journal, provenance, reporting",
        {"ledger", "journal", "provenance", "recorder", "registry", "keeper", "archive",
         "auditor", "checker", "store", "cache", "publisher", "versioner", "accountant",
         "verifier", "monitor"},
        ("io-bound", "latency-only", "delays"),
    ),
    (
        "model, scorer, search, learning",
        {"model", "scorer", "scorekeeper", "learner", "learning", "forecast", "forecaster",
         "classifier", "regressor", "calibrator", "search", "miner", "optimizer",
         "ensembler", "critic", "estimator", "selector", "ranker", "decoder", "reflector",
         "planner", "picker"},
        ("compute-bound", "latency-only", "delays"),
    ),
    (
        "indicator, feature, rolling-window",
        {"indicator", "feature", "window", "tracker", "gauge", "meter", "profiler",
         "aggregator", "consolidator"},
        ("bandwidth-bound", "changes-the-answer", "corrupts"),
    ),
    (
        "feed reader, socket loop, REST poller, venue client",
        {"reader", "feed", "venue", "poller", "fetcher", "socket", "api", "caller",
         "rotator", "resubmitter"},
        ("io-bound", "changes-the-answer", "corrupts"),
    ),
]
CATCH_ALL_LABEL = "everything else (catch-all, most restrictive on purpose)"
CATCH_ALL = ("compute-bound", "changes-the-answer", "corrupts")

# Both carry the "ledger" token and would otherwise land in row 1, throttleable.
# Both are active reservation locks, not passive records: a skipped tick on
# either one is exactly the corruption the part exists to prevent, not a delay
# of some separate answer. Forced to the catch-all regardless of token match.
FORCED_CATCH_ALL = {
    "fund-lock-ledger": (
        "reserves capital against an order the instant it is sent so two orders "
        "never spend the same balance -- delaying the reservation is the "
        "double-spend it exists to prevent"
    ),
    "resource-reservation-ledger": (
        "holds a guaranteed CPU/RAM floor for parts that must never be starved "
        "-- delaying the reservation is the starvation it exists to prevent"
    ),
}


def default_for(part_id: str) -> tuple[str, tuple[str, str, str]]:
    """Return (label, (resource_class, rate_risk, skipped_tick_effect)) for a part id."""
    if part_id in FORCED_CATCH_ALL:
        return CATCH_ALL_LABEL, CATCH_ALL
    tokens = set(part_id.split("-"))
    for label, vocabulary, triple in DEFAULTS:
        if tokens & vocabulary:
            return label, triple
    return CATCH_ALL_LABEL, CATCH_ALL


d = json.loads(REG.read_text())

touched = 0
left_alone = 0
matched = Counter()
for feature in d["features"]:
    label, (resource_class, rate_risk, skipped_tick_effect) = default_for(feature["id"])
    matched[label] += 1
    changed = False
    if "resource_class" not in feature:
        feature["resource_class"] = resource_class
        changed = True
    if "rate_risk" not in feature:
        feature["rate_risk"] = rate_risk
        changed = True
    if "skipped_tick_effect" not in feature:
        feature["skipped_tick_effect"] = skipped_tick_effect
        changed = True
    if changed:
        touched += 1
    else:
        left_alone += 1

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

print(f"{touched} parts touched, {left_alone} already fully declared and left alone")
print("classification by shape:")
for label, _, _ in DEFAULTS:
    print(f"  {label}: {matched[label]}")
print(f"  {CATCH_ALL_LABEL}: {matched[CATCH_ALL_LABEL]}")
