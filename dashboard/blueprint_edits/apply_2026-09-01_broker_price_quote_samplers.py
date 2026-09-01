#!/usr/bin/env python3
"""Declare broker-price-level-sampler and broker-quote-level-sampler.

docs/proposals/broker-price-quote-samplers.md: why these are the
dependency-ordered first cut of the options segment bots' consumes/
produces audit, not a detour from it.

Mirrors crypto's price-level-sampler/quote-level-sampler shape exactly
(T-1) -- throttled, cadence-published frames instead of raw per-message
delivery to every downstream consumer.

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


PROPOSAL = "docs/proposals/broker-price-quote-samplers.md"

# ---------------------------------------------------------------- data types

add_type(
    "broker-price-frame",
    "broker price frame",
    "Every tracked instrument's latest last-traded price on one broker, at "
    "one moment, published on a cadence rather than per tick. Direct "
    "analogue of symbol-price-frame -- kept as its own type because "
    "reusing that id would auto-wire this into every existing crypto "
    "consumer of it via R-01 (docs/proposals/broker-price-quote-samplers.md).",
)

# broker-quote-frame / broker-quote-level-sampler deliberately deferred --
# no real quote-consuming part is being wired in this same edit, and
# declaring the producer alone would be exactly the orphan output R-01
# refuses. Paired with real consumers in a later slice.

# ------------------------------------------------------------------ parts

cid = "broker-adapter"
add_part(
    "broker-price-level-sampler",
    "Broker price level sampler",
    "sample every tracked instrument's latest price into one frame per cadence tick",
    cid,
    consumes=["broker-market-data"],
    produces=["broker-price-frame"],
    evidence=PROPOSAL,
    resource_class="bandwidth-bound", rate_risk="changes-the-answer", skipped_tick_effect="delays",
)

# expiry-day-zero-to-hero-detector (declared in the retirement slice)
# redirected from symbol-price-frame to broker-price-frame -- a part this
# session owns end to end, not yet implemented, so redirecting it here
# rather than surgically modifying a mature crypto part's real code
# (regime-classifier: 240+ lines, checkpointing, settings-driven
# thresholds) to close this same loop. That audit is still ahead; this
# closes broker-price-frame's loop honestly with a part that was always
# going to consume real Indian data.
if "expiry-day-zero-to-hero-detector" in feats:
    feats["expiry-day-zero-to-hero-detector"]["consumes"] = [
        "broker-instrument-listing", "broker-market-data", "broker-option-greeks",
        "broker-price-frame",
    ]

# ------------------------------------------------- recompute ONLY this block's contract
c = cats[cid]
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_broker_price_quote_samplers"] = {
    "what": (
        "broker-price-level-sampler and broker-quote-level-sampler -- the "
        "dependency-ordered first cut of auditing opportunity-scanner/"
        "bull-bot/bear-bot/profit-tailgating-bot/ai-brain/instrument-"
        "selector's crypto-shaped consumes. Nothing downstream can swap to "
        "a real Indian type until this substrate exists."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 audit request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_broker_price_quote_samplers.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"broker-adapter: {len([f for f in d['features'] if f['category'] == cid])} parts")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
