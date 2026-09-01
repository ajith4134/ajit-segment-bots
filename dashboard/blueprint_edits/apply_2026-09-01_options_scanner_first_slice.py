#!/usr/bin/env python3
"""Retire the crypto-only opportunity-scanner detectors, add the zero-to-hero one.

docs/superpowers/specs/2026-09-01-options-segment-bots-design.md section 4.
docs/proposals/options-scanner-first-slice.md: full cascade trace and why
this is deliberately a bounded slice, not the whole conversion.

Retired: funding-skew-detector, whale-flow-detector,
liquidation-cascade-detector, sentiment-shift-detector (crypto-only
concepts, no honest Indian equivalent) and onchain-flow-aggregator
(orphaned once whale-flow-detector, its only consumer, is gone -- traced
one level, verified the cascade stops there).

Added: expiry-day-zero-to-hero-detector, the user's own addition.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}
cats = {c["id"]: c for c in d["categories"]}


def add_type(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc}
        d["data_types"].append(t)
        types[tid] = t


def add_part(pid, name, role, cat, consumes, produces, evidence,
             resource_class, rate_risk, skipped_tick_effect, origin="proposed"):
    produces = list(produces) + (["part-health"] if "part-health" not in produces else [])
    f = {
        "id": pid, "name": name, "role": role, "category": cat,
        "consumes": list(consumes), "produces": produces,
        "switchable": True, "off_releases_resources": True,
        "states": ["off", "on"], "origin": origin, "proposed": TODAY,
        "evidence": evidence,
        "resource_class": resource_class, "rate_risk": rate_risk,
        "skipped_tick_effect": skipped_tick_effect,
    }
    if pid in feats:
        feats[pid].update(f)
    else:
        d["features"].append(f)
        feats[pid] = f


def retire(pid):
    if pid in feats:
        d["features"].remove(feats.pop(pid))


SPEC = "docs/superpowers/specs/2026-09-01-options-segment-bots-design.md"
PROPOSAL = "docs/proposals/options-scanner-first-slice.md"

# ---------------------------------------------------------------- retirements

for pid in (
    "funding-skew-detector",
    "whale-flow-detector",
    "liquidation-cascade-detector",
    "sentiment-shift-detector",
    "onchain-flow-aggregator",  # orphaned once whale-flow-detector is gone
):
    retire(pid)

# No new data type here -- broker-instrument-listing, broker-market-data
# and broker-option-greeks all already exist from the market-data-feed
# work; this part is only a new consumer of them.

# ------------------------------------------------------------------ new part

add_part(
    "expiry-day-zero-to-hero-detector",
    "Expiry day zero to hero detector",
    "spot a far-out-of-the-money index option cheap enough to spike sharply if the underlying reaches its strike before today's close",
    "opportunity-scanner",
    consumes=[
        "broker-instrument-listing", "broker-market-data", "broker-option-greeks",
        "symbol-price-frame",
    ],
    produces=["entry-candidate"],
    evidence=SPEC,
    resource_class="compute-bound", rate_risk="changes-the-answer", skipped_tick_effect="corrupts",
)

# ------------------------------------------------- recompute ONLY this block's contract
c = cats["opportunity-scanner"]
parts = [f for f in d["features"] if f["category"] == "opportunity-scanner"]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_options_scanner_first_slice"] = {
    "what": (
        "Retire 4 crypto-only opportunity-scanner detectors plus one "
        "orphaned producer (onchain-flow-aggregator), add "
        "expiry-day-zero-to-hero-detector. Deliberately not the full "
        "conversion -- segments.members and the bull-bot/bear-bot/"
        "instrument-selector consumes audit are explicitly deferred."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 options segment bots brainstorm",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_options_scanner_first_slice.py",
    "proposal": PROPOSAL,
    "spec": SPEC,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"opportunity-scanner: {len([f for f in d['features'] if f['category'] == 'opportunity-scanner'])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
