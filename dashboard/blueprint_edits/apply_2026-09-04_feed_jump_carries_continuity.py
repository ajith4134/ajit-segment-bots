#!/usr/bin/env python3
"""`feed-jump` states continuity in both directions, not only the break.

docs/proposals/feed-jump-carries-continuity.md: paper-fill-simulator bars a
symbol from filling when feed-jump-detector flags it, and clear_feed_jump has no
caller anywhere in the repository -- so the bar never lifts. 993 of 1,474 streams
on the 2026-09-04 tape cross the threshold at least once, and every one of them
is unfillable for the life of the process. The detector already computes
continuity on every closed candle and throws the answer away when it is "no
jump"; publishing it is what lets the bar lift on evidence rather than on a
clock.

No part's consumes or produces changes -- the data type's meaning does, which is
what this edit records.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-04"

d = json.loads(REG.read_text())

TYPE_ID = "feed-jump"
DESCRIPTION = (
    "One symbol's price continuity as of its last closed candle, from that "
    "candle's open against the previous one's close. `is_continuous` false is a "
    "break -- present but discontinuous data, so a stop in the gap still counts "
    "as crossed and nothing may fill across it; true is the break being over. "
    "Stated in both directions since 2026-09-04: it was a break-only event, and "
    "the only consumer that acts on it had no way to learn the break had ended, "
    "so a flagged symbol could never fill again. A level, not an event -- true "
    "until it changes."
)

data_type = next(t for t in d["data_types"] if t["id"] == TYPE_ID)
data_type["description"] = DESCRIPTION

d["_proposal_2026-09-04_feed_jump_carries_continuity"] = {
    "what": (
        "feed-jump gains is_continuous and is published on every closed candle "
        "the detector can compare, not only on a break. paper-fill-simulator's "
        "clear_feed_jump -- which existed with no caller -- is wired to the true "
        "case, so a symbol barred from filling is released when its own prices "
        "are continuous again. Published as a level keyed per symbol so one "
        "symbol changing does not restate the rest (2026-08-26). Neither part's "
        "consumes or produces changes; the type's meaning does."
    ),
    "origin": "designed by Claude, user chose this over a timer 2026-09-04",
    "applied_by": (
        "dashboard/blueprint_edits/apply_2026-09-04_feed_jump_carries_continuity.py"
    ),
    "proposal": "docs/proposals/feed-jump-carries-continuity.md",
    "recorded_on": TODAY,
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(f"{TYPE_ID}: {data_type['description'][:80]}...")
print(f"total: {len(d['features'])} features, {len(d['data_types'])} data types")
