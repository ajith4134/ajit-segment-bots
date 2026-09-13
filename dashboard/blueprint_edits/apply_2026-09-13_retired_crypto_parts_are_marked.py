#!/usr/bin/env python3
"""Mark the crypto parts retired on 2026-09-02 as retired, in the blueprint itself.

Their retirement was recorded only as a comment in operate/run_live_spine.py, so
nothing that reads the blueprint could tell a retired part from a broken one:
`test_every_launchable_part_starts` failed `symbol-catalogue-reader` and
`stream-budget-planner` for refusing to start on `captured_venues = []`, which is
exactly what a retired crypto feed part should do.

The set is that comment's list, narrowed to what is true today: declared here and
not on the live spine. Three it names are running again and are NOT marked --
`venue-outage-rider`, `clock-skew-monitor` (converted to watch the broker) and
`leverage-selector` -- and two it names were never declared.

Retired, not deleted: each stays declared with its contracts, for the crypto goal
in git history (docs/goal.md: convert or replace, never just delete).

docs/proposals/retired-crypto-parts-are-marked.md carries the reasoning.
Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PROPOSAL_KEY = "_proposal_2026-09-13_retired_crypto_parts_are_marked"

WHY = (
    "2026-09-02: the crypto (Binance/Bybit) path, retired when the goal pivoted to the "
    "Indian market. Off the live spine; captured_venues is empty, so a venue part "
    "refuses to start by design."
)
REPLACED_BY = {
    "symbol-catalogue-reader": "broker-instrument-catalogue-reader",
    "venue-trade-stream-reader": "broker-market-feed-reader",
    "ccxt-order-router": "broker-order-router",
    "venue-balance-reader": "broker-account-funds-reader",
}
RETIRED = (
    "symbol-catalogue-reader", "stream-budget-planner", "venue-trade-stream-reader",
    "ban-signal-detector", "venue-quote-stream-reader", "ccxt-venue-reader",
    "cross-venue-price-consolidator", "venue-pool-rotator", "ccxt-order-router",
    "venue-rate-budgeter", "venue-order-status-translator", "venue-balance-reader",
    "venue-position-reader", "order-book-reader", "api-key-pool-rotator",
    "order-state-poller", "order-reject-classifier", "order-not-found-debouncer",
    "order-resubmitter", "liquidation-cluster-mapper", "paper-liquidation-simulator",
    "funding-settlement-recorder", "liquidation-price-tracker", "margin-liquidation-watch",
)

d = json.loads(REG.read_text())
by_id = {feature["id"]: feature for feature in d["features"]}
changed = []
for part_id in RETIRED:
    feature = by_id.get(part_id)
    if feature is None:
        raise SystemExit(f"{part_id} is not declared")
    if "retired" in feature:
        continue
    feature["retired"] = {"on": "2026-09-02", "why": WHY}
    if part_id in REPLACED_BY:
        replacement = REPLACED_BY[part_id]
        if replacement not in by_id:
            raise SystemExit(f"{part_id}'s replacement {replacement} is not declared")
        feature["retired"]["replaced_by"] = replacement
    changed.append(part_id)

if PROPOSAL_KEY not in d:
    d[PROPOSAL_KEY] = {
        "what": (
            "Parts retired with the crypto path carry a `retired` record in the blueprint, "
            "so a checker can tell a retired part from a broken one."
        ),
        "evidence": (
            "Full suite 2026-09-13: symbol-catalogue-reader and stream-budget-planner failed "
            "test_every_launchable_part_starts by refusing to start on captured_venues = [], "
            "their designed response to crypto being switched off."
        ),
        "origin": 'the operator, 2026-09-13 -- "yes do A B and C"',
        "applied_by": "dashboard/blueprint_edits/apply_2026-09-13_retired_crypto_parts_are_marked.py",
        "proposal": "docs/proposals/retired-crypto-parts-are-marked.md",
    }
    changed.append("proposal record")

if not changed:
    print("already applied")
    raise SystemExit(0)

REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"applied: {len(changed)} change(s): {', '.join(changed)}")
