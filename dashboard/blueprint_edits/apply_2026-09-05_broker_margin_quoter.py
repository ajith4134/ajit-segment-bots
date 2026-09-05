#!/usr/bin/env python3
"""What the broker will actually lend, asked rather than assumed.

docs/proposals/broker-margin-quoter.md: bot 3 trades cash equity intraday on
leverage and nothing in the system could find out how much it is allowed. The
segment states a ceiling of 5.0 because the operator asked for it; the real
limit is the broker's, per stock, from SEBI VAR+ELM margins that change daily.

It cannot be read from the instrument master -- measured 2026-09-05,
`intraday_margin` appears in ZERO of the 102,789 rows of Upstox's real file --
so it is asked for at the margin endpoint, which the adapter already speaks.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-05"
PROPOSAL = "docs/proposals/broker-margin-quoter.md"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

NEW_TYPE = {
    "id": "broker-margin-requirement",
    "name": "broker margin requirement",
    "description": (
        "What a broker requires against one instrument for an intraday order, and "
        "the leverage that implies -- notional quoted divided by margin required. "
        "Quoted per instrument rather than per order because the requirement "
        "scales with quantity and the ratio does not, which is what lets "
        "position-sizer learn the leverage before it has chosen a size. Absence "
        "is never a leverage of one and never the operator's ceiling: an "
        "instrument the broker has not answered for carries no requirement at all."
    ),
    "origin": "proposed", "proposed": TODAY, "evidence": PROPOSAL,
}
types_by_id = {t["id"]: t for t in d["data_types"]}
if NEW_TYPE["id"] in types_by_id:
    types_by_id[NEW_TYPE["id"]].update(NEW_TYPE)
else:
    d["data_types"].append(NEW_TYPE)

QUOTER = {
    "id": "broker-margin-quoter",
    "name": "Broker margin quoter",
    "role": "ask the broker what margin an intraday order on each instrument requires",
    "category": "broker-adapter",
    "consumes": ["symbol-universe", "broker-market-data", "broker-token-standing"],
    "produces": ["broker-margin-requirement", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "io-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if QUOTER["id"] in feats:
    feats[QUOTER["id"]].update(QUOTER)
else:
    d["features"].append(QUOTER)
    feats[QUOTER["id"]] = QUOTER

# leverage-selector gains the one input that is not this system's own opinion.
selector = feats["leverage-selector"]
if "broker-margin-requirement" not in selector["consumes"]:
    selector["consumes"] = [*selector["consumes"], "broker-margin-requirement"]
    history = selector.setdefault("rewired", [])
    record = {"on": TODAY, "why": PROPOSAL}
    if record not in history:
        history.append(record)

for category_id in sorted({QUOTER["category"], selector["category"]}):
    category = next(c for c in d["categories"] if c["id"] == category_id)
    parts = [f for f in d["features"] if f["category"] == category_id]
    category["consumes"] = sorted({t for f in parts for t in f["consumes"]})
    category["produces"] = sorted({t for f in parts for t in f["produces"]})
    category["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d[f"_proposal_{TODAY}_broker_margin_quoter"] = {
    "what": (
        "broker-margin-quoter -- asks Upstox's margin endpoint what an intraday "
        "order on each universe instrument would require, and publishes the "
        "leverage that implies as broker-margin-requirement. leverage-selector "
        "consumes it as a HARD CAP: the operator's ceiling and the "
        "volatility-implied figure are this system's opinions, the broker's "
        "limit is not."
    ),
    "why": (
        "Bot 3's 5x had no source. Measured 2026-09-05: `intraday_margin` "
        "appears in ZERO of the 102,789 rows of Upstox's real instrument "
        "master, so InstrumentListing.intraday_margin_percent is always None -- "
        "a field the adapter parses that the source never carries. Anything "
        "built on it would have read None forever and defaulted to an invented "
        "number. The real per-stock limit is the broker's, from SEBI VAR+ELM "
        "margins that change daily, and it is always the smaller of it and the "
        "operator's ceiling that binds."
    ),
    "decisions": {
        "D-M1": "quoted per instrument on an interval, not per order: the "
                "requirement scales with quantity so the ratio does not, which "
                "removes the circularity -- position-sizer needs the leverage "
                "to choose a size, so the leverage must not need a size",
        "D-M2": "a missing quote means UNLEVERED on a segment that borrows, "
                "never the ceiling. A margin endpoint that is down must not "
                "silently become 5x",
        "D-M3": "AT_BROKER_LIMIT is its own outcome, distinct from AT_CEILING: "
                "one is a policy this project chose and the other a fact about "
                "the account, and a board showing them as one number could not "
                "say which to change",
        "D-M4": "net_buy_premium is excluded from the margin total -- it is the "
                "cash cost of buying an option, not margin lent against",
    },
    "origin": "designed by Claude, user asked for the margin quote part 2026-09-05",
    "applied_by": ("dashboard/blueprint_edits/"
                   "apply_2026-09-05_broker_margin_quoter.py"),
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"broker-adapter: {len([f for f in d['features'] if f['category'] == 'broker-adapter'])} parts")
print(f"total: {len(d['features'])} features, {len(d['data_types'])} data types")
