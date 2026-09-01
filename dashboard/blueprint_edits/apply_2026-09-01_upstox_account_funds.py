#!/usr/bin/env python3
"""Declare broker-account-funds-reader, the second pass on the broker-adapter block.

docs/superpowers/specs/2026-09-01-upstox-adapter-design.md section 6/6a/9b.
docs/proposals/upstox-broker-account-funds.md: what this declares and why.

Account funds (GET /v2/user/get-funds-and-margin) needs only a token, no
order -- direct analogue of venue-balance-reader. Order placement and
per-order margin both need an order intent nothing in this project
produces yet, so neither gets declared here (RL-062's no-placeholder
discipline applies to a blueprint edit too).

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


SPEC = "docs/superpowers/specs/2026-09-01-upstox-adapter-design.md"
PROPOSAL = "docs/proposals/upstox-broker-account-funds.md"

# ---------------------------------------------------------------- data type

add_type(
    "broker-account-funds",
    "broker account funds",
    "What a broker says is available right now, and how old that statement "
    "is -- span/exposure/adhoc margin, payin amount, notional cash and "
    "available margin, split by segment. Direct analogue of account-balance, "
    "kept as its own type because the field shape genuinely differs (SEBI "
    "margin categories, not free/used/total) and reusing account-balance "
    "would wire this into every existing crypto consumer of it via R-01 "
    "(spec section 6a).",
)

# ------------------------------------------------------------------ part

cid = "broker-adapter"
add_part(
    "broker-account-funds-reader",
    "Broker account funds reader",
    "fetch a broker's account funds, refusing a reading past its freshness bound",
    cid,
    consumes=["broker-token-standing"],
    produces=["broker-account-funds"],
    evidence=SPEC,
    resource_class="io-bound", rate_risk="changes-the-answer", skipped_tick_effect="corrupts",
)

# broker-account-funds needs a real consumer or R-01 refuses it as an
# orphan output. board-snapshot-builder is global observability, already
# rendering "the whole system's measured state, every tile traced to a
# probe" -- account-balance (crypto's version) is already one of its
# inputs. Adding this one is extending an existing global sink to a new
# data type it should render, not inventing a part to satisfy a checker.
#
# Appended, never sorted: RL-067's wiring check (dashboard/build_part_
# monitor.py) compares this list against board_snapshot_builder.py's own
# PART_DECLARATION.consumes tuple as literal sequences, not sets -- an
# alphabetical .sort() here would silently stop matching that file's
# insertion order even though the two sides name the same set of types.
if "broker-account-funds" not in feats["board-snapshot-builder"]["consumes"]:
    feats["board-snapshot-builder"]["consumes"].append("broker-account-funds")

# ------------------------------------------------- recompute ONLY this block's contract
c = cats[cid]
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

# board-snapshot-builder sits in "observability" -- recompute only that one
# category's contract too, for the same reason: touch what changed, not
# every category the way the first attempt at the four-part edit did.
obs = cats["observability"]
obs_parts = [f for f in d["features"] if f["category"] == "observability"]
obs["consumes"] = sorted({x for f in obs_parts for x in f["consumes"]})
obs["produces"] = sorted({x for f in obs_parts for x in f["produces"]})
obs["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_upstox_account_funds"] = {
    "what": (
        "broker-account-funds-reader: the self-contained half of 'order "
        "placement and margin' -- account funds needs only a token. Order "
        "placement and per-order margin need an order intent nothing "
        "produces yet, so they land as tested UpstoxAdapter methods instead "
        "of a part with nothing upstream to call it."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 'next phase' request",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_upstox_account_funds.py",
    "proposal": PROPOSAL,
    "spec": SPEC,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"broker-adapter: {len([f for f in d['features'] if f['category'] == cid])} parts declared")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
