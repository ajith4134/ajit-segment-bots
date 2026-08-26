#!/usr/bin/env python3
"""symbol-catalogue-reader reads what the bot holds, so it stops dropping it.

Proposed by Claude 2026-08-26.
Rationale: docs/proposals/the-feed-drops-a-symbol-the-bot-is-holding.md
Idempotent.

Measured 2026-08-26: the bot holds STORJUSDT on both venues and its tape stops at
2026-08-25 18:40 -- no file for today at all, while BTCUSDT was current to the
minute. The catalogue re-selects the 50 highest-volume symbols every 900 seconds
and nothing in that path knows what is held, so a symbol whose volume slipped was
dropped from the universe, from the stream plan, and from capture. The position
could then not be priced, not be stopped out, and not have its excursion measured
-- and `feed-coverage-auditor` read complete throughout, because it audits
coverage *of the universe* and the symbol had left it.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"

READER = "symbol-catalogue-reader"
POSITION = "position"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

if READER not in features:
    raise SystemExit(f"{READER} is not in the registry; this edit is out of date")

producers = [
    feature["id"] for feature in registry["features"] if POSITION in feature.get("produces", ())
]
if not producers:
    raise SystemExit(
        f"nothing produces {POSITION!r}; this edit would create a dangling edge (R-01)"
    )

consumes = features[READER].setdefault("consumes", [])
if POSITION in consumes:
    print(f"{READER} already consumes {POSITION!r}; nothing to do")
else:
    consumes.append(POSITION)
    consumes.sort()
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{READER} now consumes {POSITION!r}, produced by {', '.join(producers)}")

print(
    "The rank cut is unchanged for everything else: a held symbol is added back "
    "after it, so the universe is the top N by volume plus whatever is still held, "
    "and it leaves on the ordinary rotation once the position is flat."
)
