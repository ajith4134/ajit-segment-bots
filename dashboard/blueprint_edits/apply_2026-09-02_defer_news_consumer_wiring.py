#!/usr/bin/env python3
"""Put back every consumer edge the news-data edit declared ahead of its code.

apply_2026-09-02_stock_market_news_data.py added 31 consume edges to parts that
already exist, and retired two parts plus the `venue-announcement` type while
their source files stayed exactly as they were. Both halves are wrong the same
way, and the test suite said so within the hour: eleven
`test_every_built_declaration_equals_the_blueprint` cases went red, because a
**built** part's declaration must equal the blueprint (RL-067).

The distinction the first edit missed: a blueprint may declare a part that has
no code -- 277 parts were in exactly that state on 2026-08-23 -- but it may not
declare an input for a part whose code is already running, because the wiring
plan then builds an inbox that nothing drains and the diagram claims a wire
that carries nothing. Worse for the retirement: the registry said
`exchange-announcement-reader` and `market-event-reader` were gone while both
files still ran on the spine and still published and read `venue-announcement`.

So the design stays in the spec and the proposal, and the registry carries an
edge only when the code can honour it. Three edges are kept, because
docs/superpowers/plans/2026-09-02-news-hard-channel.md Tasks 8-10 write the
code that reads them in this same pass:

    halt-enforcer         <- instrument-restriction
    paper-fill-simulator  <- market-session-state
    kline-window-builder  <- corporate-action

The other 28, and the retirement of the two crypto-era parts, move to the plan
that rewrites their code. `event-risk-limiter` shows why that is not a
formality: its `register_announcement` path is real logic keyed on a venue
announcement's own fields, and re-pointing it at a news type is a rewrite with
its own tests, not a line in a registry.

The pre-edit consumes are read from the commit before the news edit rather than
retyped, so this restores exactly what was there.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-02"
BEFORE_THE_NEWS_EDIT = "324801b"

# The three whose code is written in this same pass. Everything else goes back.
KEPT = {
    "halt-enforcer": "instrument-restriction",
    "paper-fill-simulator": "market-session-state",
    "kline-window-builder": "corporate-action",
}

before = json.loads(subprocess.run(
    ["git", "show", f"{BEFORE_THE_NEWS_EDIT}:docs/features.json"],
    capture_output=True, text=True, check=True, cwd=ROOT,
).stdout)
d = json.loads(REG.read_text())

was = {f["id"]: f for f in before["features"]}
now = {f["id"]: f for f in d["features"]}

# 1. Restore every existing part's consumes to what it was, then re-add the
#    three edges whose readers are being written now.
restored = []
for part_id, feature in now.items():
    original = was.get(part_id)
    if original is None:
        continue  # a part this edit introduced; it has no "before" to restore
    # Verbatim, in its original order: 197 parts carry an unsorted consumes
    # list from earlier edits, and re-sorting them here would rewrite the
    # declaration of parts this work never touched -- the same over-reach one
    # layer along.
    wanted = list(original["consumes"])
    if part_id in KEPT and KEPT[part_id] not in wanted:
        wanted = sorted([*wanted, KEPT[part_id]])
    if feature["consumes"] != wanted:
        feature["consumes"] = wanted
        feature.pop("rewired", None)
        restored.append(part_id)

# 2. Put back the two parts and the type the news edit retired while their code
#    was still running and still publishing.
for part_id in ("exchange-announcement-reader", "market-event-reader"):
    if part_id not in now:
        d["features"].append(dict(was[part_id]))

if not any(t["id"] == "venue-announcement" for t in d["data_types"]):
    d["data_types"].append(next(
        t for t in before["data_types"] if t["id"] == "venue-announcement"
    ))

# 3. Recompute every block contract from its parts, since consumes moved.
for category in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == category["id"]]
    if not parts:
        continue
    category["consumes"] = sorted({t for p in parts for t in p["consumes"]})
    category["produces"] = sorted({t for p in parts for t in p["produces"]})
    category["contract_recomputed"] = {
        "on": TODAY, "from": "its parts", "origin": "user",
    }

d[f"_proposal_{TODAY}_defer_news_consumer_wiring"] = {
    "what": (
        "restores the 28 consumer edges and the two retirements that the "
        "news-data edit declared ahead of their code, keeping only the three "
        "edges whose readers are written in the same pass."
    ),
    "why": (
        "a built part's declaration must equal the blueprint (RL-067), and "
        "eleven declaration tests went red within the hour. A blueprint may "
        "declare a part with no code; it may not declare an input for a part "
        "whose code is already running, or the wiring plan builds an inbox "
        "nothing drains and the diagram claims a wire that carries nothing."
    ),
    "kept": KEPT,
    "given": TODAY,
    "origin": "user",
    "evidence": "docs/superpowers/plans/2026-09-02-news-hard-channel.md",
}

REG.write_text(json.dumps(d, indent=2) + "\n")
print(f"{len(d['categories'])} categories, {len(d['features'])} features, "
      f"{len(d['data_types'])} data types")
print(f"consumes restored on {len(restored)} parts")
