#!/usr/bin/env python3
"""Declare the first four parts of the broker-adapter block for Indian markets.

docs/goal.md (2026-09-01): crypto retired, converting to Indian stock trading.
docs/superpowers/specs/2026-09-01-upstox-adapter-design.md: the adapter design.
docs/proposals/upstox-broker-adapter.md: what this declares and why, in full.

Read-side substrate only -- keep a session token valid, know what's tradable,
stream live prices, write the tape. Order placement and margin stay
undeclared until this segment actually moves toward live orders and has a
real consumer to wire them into (RL-062's no-placeholder discipline applies
to a blueprint edit too: don't invent a consumer just to satisfy R-01).

Parts are broker-agnostic on purpose -- same shape as execution-venue-adapter
never naming Binance or Bybit. Upstox is the first of six planned adapters
(docs/goal.md #6); which broker answers is a settings choice, not a part's
identity (T-1, T-4).

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
PROPOSAL = "docs/proposals/upstox-broker-adapter.md"

# ---------------------------------------------------------------- data types

add_type(
    "broker-token-standing",
    "broker token standing",
    "Whether the day's broker session token is still valid right now, and "
    "until when -- the daily-expiry analogue of a venue's key-standing, "
    "since a broker session here is a whole login rather than a signed API "
    "key (spec section 3).",
)
add_type(
    "broker-instrument-listing",
    "broker instrument listing",
    "One contract as an Indian broker's own instrument master lists it: "
    "instrument key, exchange, segment, lot size, tick size, and -- for a "
    "derivative -- its expiry, strike and option type. Kept separate from "
    "symbol-universe because the shape genuinely differs (spec section 4).",
)
add_type(
    "broker-market-data",
    "broker market data",
    "One trade or last-traded-price update from a broker's feed. Carries "
    "the same TradeFidelity distinction as market-data, extended with "
    "LAST_TRADED_PRICE_ONLY for a retail feed that states the exchange's "
    "last print rather than streaming every one (spec section 5).",
)
add_type(
    "broker-candle",
    "broker candle",
    "One OHLC bar from a broker's feed, same shape as candle -- kept "
    "separate rather than merged into it so this edit stays self-contained "
    "(docs/proposals/upstox-broker-adapter.md).",
)
add_type(
    "broker-order-book-snapshot",
    "broker order book snapshot",
    "Bid/ask depth from a broker's feed, same shape as order-book-snapshot "
    "-- kept separate for the same reason as broker-candle.",
)
add_type(
    "broker-open-interest",
    "broker open interest",
    "Open interest and today's traded volume/buy-sell quantity for one "
    "derivative contract, from a broker's feed. No crypto equivalent -- a "
    "spot/perpetual venue never published this (spec section 5).",
)
add_type(
    "broker-option-greeks",
    "broker option greeks",
    "Delta, theta, gamma, vega, rho and implied volatility for one options "
    "contract, from a broker's feed. No crypto equivalent (spec section 5).",
)

# --------------------------------------------------------------- category

cid = "broker-adapter"
if cid not in cats:
    c = {
        "id": cid,
        "name": "Broker adapter (Indian markets)",
        "summary": (
            "Reads, and later trades through, India's retail broker APIs -- "
            "one BrokerAdapter contract that any of the six brokers named in "
            "docs/goal.md can satisfy. Upstox is the first and only "
            "implementation; part ids are broker-agnostic on purpose, same as "
            "execution-venue-adapter never naming Binance or Bybit -- which "
            "broker answers is a settings choice, not a part's identity."
        ),
        "origin": "proposed",
        "approved": True,
        "consumes": [], "produces": [],
        "flow_origin": "agreed-provisional",
        "scope": "global",
        "scope_origin": "claude-flagged",
    }
    d["categories"].append(c)
    cats[cid] = c

# ------------------------------------------------------------------ parts

# Same triple as symbol-catalogue-reader / venue-trade-stream-reader for
# every part here except the token scheduler: io-bound (network or disk),
# changes-the-answer (a lower rate would change what the answer is, not just
# when it arrives), corrupts (a skipped tick loses data rather than merely
# delaying it -- a missed live tick or a stale universe is gone, not late).
add_part(
    "broker-token-refresh-scheduler",
    "Broker token refresh scheduler",
    "keep today's broker session token valid, refreshed automatically before market open",
    cid,
    consumes=[],
    produces=["broker-token-standing"],
    evidence=SPEC,
    # Different from the other three: a delayed refresh doesn't corrupt
    # anything, it just means broker-token-standing says "not yet valid" a
    # little longer -- consumers wait rather than act on wrong data.
    resource_class="io-bound", rate_risk="latency-only", skipped_tick_effect="delays",
)
add_part(
    "broker-instrument-catalogue-reader",
    "Broker instrument catalogue reader",
    "fetch a broker's daily instrument master, every tradable contract as the broker lists it",
    cid,
    consumes=[],
    produces=["broker-instrument-listing"],
    evidence=SPEC,
    resource_class="io-bound", rate_risk="changes-the-answer", skipped_tick_effect="corrupts",
)
add_part(
    "broker-market-feed-reader",
    "Broker market feed reader",
    "stream a broker's feed, decomposed into this project's own record kinds",
    cid,
    consumes=["broker-token-standing", "broker-instrument-listing"],
    produces=[
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks",
    ],
    evidence=SPEC,
    resource_class="io-bound", rate_risk="changes-the-answer", skipped_tick_effect="corrupts",
)
add_part(
    "broker-market-tape-writer",
    "Broker market tape writer",
    "persist every decomposed broker feed record to the tape before anything else reads it",
    cid,
    consumes=[
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks",
    ],
    produces=[],
    evidence=SPEC,
    resource_class="io-bound", rate_risk="changes-the-answer", skipped_tick_effect="corrupts",
)

# ------------------------------------------------- recompute ONLY this block's contract
# Deliberately scoped to the one category this script touches. The other 27
# were found to have drifted from their own parts' current consumes/produces
# (a legitimate but separate finding) -- recomputing all of them here would
# bundle 27 unrelated content changes into an edit meant to add four parts.
# That drift is for its own dedicated blueprint edit, not a side effect of
# this one.
c = cats[cid]
parts = [f for f in d["features"] if f["category"] == cid]
c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
c["produces"] = sorted({x for f in parts for x in f["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-01_upstox_broker_adapter"] = {
    "what": (
        "First four parts of the broker-adapter block: token refresh, "
        "instrument catalogue, market feed, tape write. Read-side substrate "
        "only -- order placement and margin stay undeclared until this "
        "segment moves toward live orders (RL-062)."
    ),
    "origin": "designed by Claude against the user's 2026-09-01 crypto-to-India conversion",
    "applied_by": "dashboard/blueprint_edits/apply_2026-09-01_upstox_broker_adapter.py",
    "proposal": PROPOSAL,
    "spec": SPEC,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"broker-adapter: {len([f for f in d['features'] if f['category'] == cid])} parts declared")
print(f"total: {len(d['features'])} features, {len(d['categories'])} categories, {len(d['data_types'])} data types")
