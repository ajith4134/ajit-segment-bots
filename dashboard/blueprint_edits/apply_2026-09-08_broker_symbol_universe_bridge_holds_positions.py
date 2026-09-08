#!/usr/bin/env python3
"""broker-symbol-universe-bridge also consumes `position`.

docs/proposals/broker-symbol-universe-bridge.md's ranking -- nearest expiry,
distance from the underlying's own price, capped chain width -- bounds what a
segment might newly buy. It was also, until this date, the only thing standing
between an already-open position and its own price: a position opened weeks
earlier, on a strike the price has since moved away from or an expiry that has
since rolled past nearest, fell out of that window and stayed out for the rest
of its life. Measured live 2026-09-08: 10 of 16 open stock-options positions
had zero live price, real capital committed, no stop able to trigger, no
unrealised P&L computable -- confirmed by reading the tape directly, no file
for any of the ten under today's date.

The fix reads `position` -- the level fill-reconciler and position-close-
detector already publish -- and forces a held symbol's own listing into the
universe unconditionally, bypassing the ranking and the nearest-expiry filter
that correctly still bound everything else. Nothing evicts a position from the
book; nothing should be allowed to evict its listing from the universe.

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
cid = "market-data-feed"
part_id = "broker-symbol-universe-bridge"

f = feats[part_id]
f["consumes"] = sorted(set(f["consumes"]) | {"position"})

c = next(cat for cat in d["categories"] if cat["id"] == cid)
parts = [feature for feature in d["features"] if feature["category"] == cid]
c["consumes"] = sorted({x for feature in parts for x in feature["consumes"]})
c["produces"] = sorted({x for feature in parts for x in feature["produces"]})
c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

d["_proposal_2026-09-08_broker_symbol_universe_bridge_holds_positions"] = {
    "what": (
        "broker-symbol-universe-bridge now reads `position` and forces every "
        "currently-held symbol's own listing into the published universe, "
        "unconditionally -- no distance-from-the-money rank, no nearest-expiry "
        "filter, no chain-width cap. Those three correctly bound what a segment "
        "might newly buy; they were also, until this date, capable of dropping "
        "a position already holding real capital out of the universe forever "
        "once the market moved past the window they define. Measured live "
        "2026-09-08: 10 of 16 open stock-options positions had no tape record "
        "for today at all under their own venue+symbol path."
    ),
    "origin": "found and fixed by Claude during a live-spine verification the "
              "user asked for, 2026-09-08",
    "applied_by": "dashboard/blueprint_edits/"
                  "apply_2026-09-08_broker_symbol_universe_bridge_holds_positions.py",
    "proposal": PROPOSAL,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{part_id}: consumes now {f['consumes']}")
print(f"{cid}: {len([x for x in d['features'] if x['category'] == cid])} parts")
