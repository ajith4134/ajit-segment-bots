#!/usr/bin/env python3
"""What market is each part actually working on, and is that data alive?

The instrument for the **third temporary goal** (2026-09-06, `docs/goal.md`):
take every part and find whether it is *"really providing or working with the
data according to its intended purpose, or if it is just a skeleton providing or
inputting or outputing rubbish data which is not useful just decorating data"*.

**Why the other three instruments cannot answer this.**
`audit_feature_dataflow.py` reads each part's bus counters and says whether a
message ever travelled. The four checkers say the contracts hold. All of them
can be perfectly green while a part reasons over a universe that stopped
existing. That is not hypothetical -- it is what this probe found on the day it
was written:

    cointegration-pair-finder   5,949 symbols held, every one a real Indian
                                instrument, 98.7% of them holding a single
                                price, newest observation two days old, and
                                7,814,961 pairs tested against them. The live
                                board reads WORKING at 1,619/s.

Busy is not the same as useful, and only a probe that opens the state a part
reasons over can tell the two apart.

**What it measures, per part, from sources that cannot flatter it:**

- the universe it says it holds, from its own standing counters
- whether those counters actually moved, taken as a delta between two readings
  of the heartbeat table this process observed itself
- for every part that checkpoints, the symbols in that checkpoint resolved
  against the **real Upstox instrument master** -- so "Indian" is a lookup, not
  a guess about what a ticker looks like
- how stale that state is, per symbol

The symbol classifier is deliberately a master lookup rather than a pattern. A
regex written for this on 2026-09-06 read `788BOBPERP` as a crypto perpetual; it
is a Bank of Baroda perpetual **bond**, and the pattern would have reported
Indian debt as crypto drift.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
HEARTBEAT = STATE / "heartbeat-table.json"
CHECKPOINT_ROOTS = (STATE / "positions", STATE / "learned")

# The venues this project has retired. Named explicitly rather than inferred:
# these are the only three strings that make a part crypto-shaped by venue.
CRYPTO_VENUES = ("binance-usdm", "bybit-linear", "binance", "bybit")
INDIAN_VENUE = "upstox"

# A standing field naming how much of a universe a part is holding. Matched by
# meaning rather than by an exact list, because parts add their own and a list
# would silently stop covering them.
UNIVERSE_WORDS = ("symbols_tracked", "symbols_held", "instruments_tracked",
                  "instruments_known", "pairs_known", "restored_symbols",
                  "subscribed_instruments", "priced_symbols")

SECONDS_BETWEEN_READINGS = 6.0
A_DAY_IN_SECONDS = 86_400.0


def read_heartbeat() -> dict:
    try:
        return json.loads(HEARTBEAT.read_text())
    except (OSError, ValueError):
        return {}


def standing_of(table: dict) -> dict[str, dict]:
    return {
        beat.get("part_id"): beat
        for beat in (table.get("heartbeats") or [])
        if beat.get("part_id")
    }


def instrument_master() -> dict[str, dict]:
    """Upstox's own master, by trading symbol. Empty when it is not on disk."""
    import importlib.util

    replay = PROJECT / "operate/replay_a_captured_session.py"
    try:
        spec = importlib.util.spec_from_file_location("replay_for_audit", replay)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        by_key = module.instruments_by_key()
    except Exception:
        return {}
    by_symbol = {}
    for row in by_key.values():
        symbol = row.get("trading_symbol")
        if symbol:
            by_symbol[symbol] = row
    return by_symbol


def symbols_in(document) -> set[str]:
    """Every venue|symbol key a checkpoint holds, split into bare symbols."""
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and "|" in key:
                    found.add(key)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    bare: set[str] = set()
    for key in found:
        for piece in re.split(r"[,|]", key):
            piece = piece.strip()
            if piece and piece not in CRYPTO_VENUES and piece != INDIAN_VENUE:
                bare.add(piece)
    return bare


def venues_in(document) -> set[str]:
    """Which venue ids a checkpoint's own keys name."""
    text = json.dumps(document)[:5_000_000]
    return {venue for venue in (*CRYPTO_VENUES, INDIAN_VENUE) if f'"{venue}|' in text or f"{venue}|" in text}


def freshness_of(document) -> tuple[int, int, float | None]:
    """(series carrying one value, series in total, seconds since the newest).

    A series holding a single price cannot support any window this project
    judges on -- every detector here wants 256 observations -- so counting them
    separately is what separates a real universe from a remembered one.
    """
    saved = document.get("saved_at_ns") or 0
    singles = total = 0
    newest = None
    def walk(node) -> None:
        nonlocal singles, total, newest
        if isinstance(node, dict):
            if "values" in node and "last_observed_at_ns" in node:
                values = node.get("values")
                if isinstance(values, list):
                    total += 1
                    if len(values) <= 1:
                        singles += 1
                stamp = node.get("last_observed_at_ns")
                if isinstance(stamp, (int, float)) and stamp > 1e18:
                    newest = stamp if newest is None else max(newest, stamp)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(document)
    age = ((saved - newest) / 1e9) if (saved and newest) else None
    return singles, total, age


def checkpoints() -> list[pathlib.Path]:
    found = []
    for root in CHECKPOINT_ROOTS:
        if root.is_dir():
            found.extend(sorted(path for path in root.iterdir() if path.suffix == ".json"))
    return found


def main() -> int:
    master = instrument_master()
    print("What each part is actually working on\n")
    print(f"  instrument master   {len(master)} Upstox trading symbols"
          if master else "  instrument master   NOT AVAILABLE — symbols cannot be resolved")

    first = read_heartbeat()
    if not first:
        print("  heartbeat table     NOT MEASURED — nothing at " + str(HEARTBEAT))
        return 1
    time.sleep(SECONDS_BETWEEN_READINGS)
    second = read_heartbeat()
    before, after = standing_of(first), standing_of(second)
    print(f"  parts reporting     {len(after)}")
    print(f"  rate window         {SECONDS_BETWEEN_READINGS:.0f}s, observed by this process\n")

    print("=" * 78)
    print("THE STATE PARTS RESTORE FROM — symbols resolved against the real master")
    print("=" * 78)
    for path in checkpoints():
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"\n  {path.name}: UNREADABLE")
            continue
        part_id = document.get("part_id") or path.stem
        symbols = symbols_in(document)
        venues = venues_in(document)
        singles, total, age = freshness_of(document)
        print(f"\n  {part_id}   ({path.name})")
        if not symbols:
            print("      holds no per-symbol state")
        else:
            known = [s for s in symbols if s in master]
            unknown = sorted(s for s in symbols if s not in master)
            print(f"      symbols held            {len(symbols)}")
            print(f"      resolve to real Indian  {len(known)}")
            if unknown:
                print(f"      unresolved              {len(unknown)}  {unknown[:4]}")
        crypto = sorted(v for v in venues if v in CRYPTO_VENUES)
        print(f"      venues named            {sorted(venues) or 'none'}"
              + ("   <-- CRYPTO" if crypto else ""))
        if total:
            print(f"      series holding 1 price  {singles} of {total}"
                  + ("   <-- cannot fill any 256-observation window" if singles > total * 0.5 else ""))
        if age is not None:
            days = age / A_DAY_IN_SECONDS
            flag = "   <-- STALE" if age > A_DAY_IN_SECONDS else ""
            print(f"      newest observation      {age:,.0f}s before the save ({days:.1f} days){flag}")

    print("\n" + "=" * 78)
    print("UNIVERSE EACH RUNNING PART HOLDS, AND WHETHER ITS COUNTERS MOVED")
    print("=" * 78)
    rows = []
    for part_id, beat in sorted(after.items()):
        standing = beat.get("standing") or {}
        held = {name: value for name, value in standing.items()
                if any(word in name for word in UNIVERSE_WORDS) and isinstance(value, (int, float)) and value}
        crypto_named = sorted(
            name for name in standing
            if any(venue in name for venue in CRYPTO_VENUES)
        )
        was = (before.get(part_id) or {}).get("standing") or {}
        moved = any(
            isinstance(value, (int, float)) and was.get(name) != value
            for name, value in standing.items()
        ) if was else None
        if held or crypto_named:
            rows.append((part_id, held, crypto_named, moved))
    for part_id, held, crypto_named, moved in sorted(rows, key=lambda r: -max(r[1].values(), default=0)):
        rate = {True: "counters moved", False: "IDLE, nothing moved", None: "NOT MEASURED"}[moved]
        biggest = ", ".join(f"{name} {int(value):,}" for name, value in
                            sorted(held.items(), key=lambda kv: -kv[1])[:3])
        print(f"\n  {part_id:<40} {rate}")
        if biggest:
            print(f"      {biggest}")
        if crypto_named:
            print(f"      NAMES A CRYPTO VENUE IN ITS OWN STANDING: {crypto_named}")
    print("\nA universe far larger than what the feed subscribes is a part reasoning")
    print("over instruments nobody is pricing. Cross-check against the feed's own")
    print("subscribed_instruments above before calling any of it healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
