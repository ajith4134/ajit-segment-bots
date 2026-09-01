#!/usr/bin/env python3
"""Relabel docs/features.json's segments.members from the crypto 3
(spot/futures/options) to the real 6 (docs/goal.md, 2026-09-01).

Pure metadata -- no part reads segments.members directly (each part reads
its own runtime setting segment_id, resolved by the operator per process).
Kept in sync anyway: a blueprint that still describes the retired crypto
goal is exactly the kind of stale-but-convincing state Rule 8 exists to
prevent, even where nothing crashes on it.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
TODAY = "2026-09-01"

d = json.loads(REG.read_text())

d["segments"] = {
    "note": (
        "Every segment is a bot with all of this inside it. The user's words "
        "(docs/goal.md). The same structure is instantiated per segment, and "
        "the directional response to an opportunity depends on which segment "
        "is reacting -- a long call in index-options is a long in index-futures."
    ),
    "members": [
        {"id": "index-options", "name": "Index Options"},
        {"id": "stock-options", "name": "Stock Options"},
        {"id": "index-futures", "name": "Index Futures"},
        {"id": "stock-futures", "name": "Stock Futures"},
        {"id": "commodities", "name": "Commodities"},
        {"id": "cash-equity", "name": "Cash Equity"},
    ],
    "origin": "user",
    "scope_decision": {
        "choice": "split",
        "given": "2026-08-20",
        "note": (
            "Each segment gets its own of every block. Complete isolation: a "
            "fault in one segment cannot reach another, and each segment "
            "learns only from itself. Confirmed to still hold under the "
            "2026-09-01 goal correction -- the split itself did not change, "
            "only which six segments are being split."
        ),
        "flagged": (
            "CONFIRMED by the user 2026-08-20: the hardware resource governor "
            "and observability stay global. One machine means one governor -- "
            "six of them, each blind to the others, would compete for the "
            "same RAM. One board, one place to look."
        ),
        "edges": (
            "The edges from the crypto-era design were accepted provisionally "
            "on 2026-08-20 and have not been re-decided against the real six "
            "segments -- flow_origin reads agreed-provisional, never agreed."
        ),
    },
    "build_order": {
        "ruling": "docs/goal.md item 3, corrected 2026-09-01",
        "given": "2026-09-01",
        "implement_now": ["index-options", "stock-options"],
        "skeleton_only": ["index-futures", "stock-futures", "commodities", "cash-equity"],
        "note": (
            "Phase A: index options and stock options, both, fully -- paper on "
            "historic data, then live-data paper trading, all parts genuinely "
            "running, before Phase B starts. Phase B (only after Phase A is "
            "fully complete): intraday equity with margin/leverage, stock "
            "futures, index futures, commodities. Same one-vertical-first "
            "discipline RL-050 established for the retired crypto build, now "
            "applied at the two-segment-bots granularity."
        ),
    },
}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("segments.members ->", [m["id"] for m in d["segments"]["members"]])
