#!/usr/bin/env python3
"""broker-market-tape-writer also consumes broker-subscribed-instrument-listing.

Real incident, 2026-09-08: every Upstox tick reaching this part carries only
`instrument_key` (the venue's own naming -- T-4, the raw decoded feed types
genuinely have nothing else), and the tape was written under that key
directly -- tape/upstox/NSE_FO|56316/... But every reader of this tape
(dashboard/build_trade_board.py's read_last_price, called with a Position's
own `symbol`) looks the file up by `trading_symbol`, the shared name
broker-symbol-universe-bridge's own docstring names for exactly this purpose.
Ten open stock-options positions with real capital in them ticked every
second and showed NOT MEASURED on the trade board for it -- confirmed live by
resolving one held symbol's instrument_key by hand and finding today's tape
file sitting there, correctly written, under the wrong name.

Resolved the same way broker_history_reader.py already resolves the same gap
for its own output: an instrument_key -> trading_symbol map built from
broker-subscribed-instrument-listing, the narrowed set
expiry-day-zero-to-hero-detector already reads for the same reason.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-08"

d = json.loads(REG.read_text())
feats = {f["id"]: f for f in d["features"]}

PROPOSAL = "docs/proposals/broker-symbol-universe-bridge.md"
cid = "broker-adapter"
part_id = "broker-market-tape-writer"

f = feats[part_id]
f["consumes"] = sorted(set(f["consumes"]) | {"broker-subscribed-instrument-listing"})

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-08_broker_market_tape_writer_resolves_symbol"] = {
    "what": (
        "broker-market-tape-writer now resolves instrument_key to "
        "trading_symbol via broker-subscribed-instrument-listing before "
        "writing to the tape, instead of writing under the venue's own "
        "instrument_key. Every reader of this tape (the trade board's "
        "read_last_price included) looks the file up by trading_symbol -- "
        "the writer was the one part out of step. A tick for an unresolved "
        "key still writes, under the instrument_key as before, and "
        "unresolved_writes counts how often, so a resolution gap that never "
        "closes stays visible."
    ),
    "origin": "found and fixed by Claude during a live-spine verification the "
              "user asked for, 2026-09-08",
    "applied_by": "dashboard/blueprint_edits/"
                  "apply_2026-09-08_broker_market_tape_writer_resolves_symbol.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{part_id}: consumes now {f['consumes']}")
print(f"{cid}: {len([x for x in d['features'] if x['category'] == cid])} parts")
