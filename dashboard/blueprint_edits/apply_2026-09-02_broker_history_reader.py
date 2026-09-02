#!/usr/bin/env python3
"""Declare broker-history-reader.

docs/proposals/broker-history-reader.md: fetches Upstox historical candles for
the instruments already listed and publishes them as `candle`, so Phase A can
paper-trade during the hours the market is shut.

It produces `candle` and deliberately not `broker-candle`: the latter is what
broker-market-tape-writer records, and writing replayed bars into the live
capture would make every measurement taken over that tape quietly wrong.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-02"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-history-reader.md"
cid = "broker-adapter"

f = {
    "id": "broker-history-reader",
    "name": "Broker history reader",
    "role": "fetch a broker's historical candles for the hours the market is shut",
    "category": cid,
    "consumes": [
        "broker-instrument-listing",
        "broker-token-standing",
        "market-session-state",
    ],
    "produces": ["candle", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "io-bound",
    # The bars it publishes are what the deciding half reads when the market is
    # shut; a tick skipped is a stretch of history nobody sees.
    "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
}
if f["id"] in feats:
    feats[f["id"]].update(f)
else:
    d["features"].append(f)
    feats[f["id"]] = f

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-02_broker_history_reader"] = {
    "what": (
        "broker-history-reader -- fetches Upstox v3 historical candles for "
        "currently listed contracts and publishes them as `candle`, so the "
        "same bots decide on the same wire whether the prices came from the "
        "live feed or from history. Reads market-session-state and fetches "
        "only while the session is not open."
    ),
    "why": (
        "Phase A paper-trades on live prices when the market is open and on "
        "history when it is shut (goal.md; user 2026-09-02). RL-071 is not "
        "crossed: a replay that runs only when there is no live market to "
        "read is not standing in for one, and market-session-state is the "
        "fact that tells the two apart."
    ),
    "produces_candle_not_broker_candle": (
        "broker-candle is what broker-market-tape-writer records as live "
        "capture. History published there would mix replayed bars into the "
        "tape and every measurement taken over it afterwards would be wrong "
        "with nothing saying so. `candle` reaches kline-window-builder, "
        "historical-bar-store and feed-jump-detector and no recorder of live "
        "capture -- history joins the circuit after the tape."
    ),
    "verified_against_the_live_api": (
        "2026-09-02: 385 one-minute bars for NSE_FO|42654 (NIFTY 24350 PE 08 "
        "SEP 26) across 2026-09-01, 09:15-15:39 IST, ascending after the "
        "reader's sort, every one closed. Minute data begins January 2022, "
        "one month per request. Expired contracts are absent from the "
        "instrument master, so history is read for currently listed "
        "contracts over the days they have already traded."
    ),
    "evidence": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2) + "\n")
print(f"declared {f['id']} in {cid}; {len(d['features'])} features total")
