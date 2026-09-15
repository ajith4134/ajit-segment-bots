"""Runs the real FeedJumpDetector over the real tape, before and after the fix.

Not a re-implementation of the rule: it imports the part and feeds it the
captured Upstox `I1` bars, which is the only interval broker-candle-bridge
republishes. The "before" run is the same class constructed the way it was
constructed until 2026-09-04 -- patience_multiple=None, so the stated floor is
the whole bound -- so the two columns differ only by the change under test.

What it has to show, for a paper order ever to fill:
  - the flag rate on option contracts falls to something a real discontinuity
    could reach, rather than 38% of ordinary bars;
  - the index underlyings are judged exactly as before;
  - every symbol that breaks is afterwards reported continuous again, because
    that report is what releases paper-fill-simulator's bar on it.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from parts.market_data_feed.feed_jump_detector import Candle, FeedJumpDetector

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
INTERVAL_ON_THE_WIRE = "I1"
VENUE = "upstox"

# The live settings this runs against.
THRESHOLD_INCREMENTS = 20.0
THRESHOLD_FRACTION = 0.005
PATIENCE_MULTIPLE = 2.8
MOVES_NEEDED = 8
MOVES_REMEMBERED = 256


def bars_of(directory: pathlib.Path):
    index_path = next(directory.glob("*.candle.index"), None)
    if index_path is None:
        return
    from runtime.tape import read_tape_index

    records = read_tape_index(index_path)
    if len(records) == 0:
        return
    with open(index_path.with_suffix(".blob"), "rb") as blob:
        for record in records:
            blob.seek(int(record["blob_offset"]))
            try:
                candle = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            if candle.get("interval") != INTERVAL_ON_THE_WIRE:
                continue
            if not candle.get("open") or not candle.get("close"):
                continue
            yield candle


def replay(patience_multiple: float | None) -> dict:
    detector = FeedJumpDetector(
        jump_threshold_increments=THRESHOLD_INCREMENTS,
        warmup_floor_fraction=THRESHOLD_FRACTION,
        patience_multiple=patience_multiple,
        moves_needed=MOVES_NEEDED,
        moves_remembered=MOVES_REMEMBERED,
    )
    judged = collections.Counter()
    broke = collections.Counter()
    ever_broken: dict[str, set[str]] = collections.defaultdict(set)
    left_broken: dict[str, set[str]] = collections.defaultdict(set)

    for directory in sorted(TAPE.iterdir()):
        if not directory.is_dir():
            continue
        exchange = directory.name.split("|", 1)[0]
        symbol = directory.name
        for bar in bars_of(directory):
            verdict = detector.observe_closed_candle(
                Candle(
                    venue_id=VENUE,
                    symbol=symbol,
                    open_time_ns=int(bar["bar_time_ms"]) * 1_000_000,
                    open_price=bar["open"],
                    close_price=bar["close"],
                    high_price=bar["high"],
                    low_price=bar["low"],
                )
            )
            if verdict is None:
                continue
            judged[exchange] += 1
            if not verdict.is_continuous:
                broke[exchange] += 1
                ever_broken[exchange].add(symbol)
                left_broken[exchange].add(symbol)
            else:
                left_broken[exchange].discard(symbol)

    return {
        "judged": judged,
        "broke": broke,
        "ever_broken": ever_broken,
        "left_broken": left_broken,
        "standing": detector.standing,
    }


def main() -> int:
    before = replay(patience_multiple=None)
    after = replay(patience_multiple=PATIENCE_MULTIPLE)

    header = (
        f"{'exchange':<11}{'judged':>10}{'flagged before':>16}{'flagged after':>15}"
        f"{'barred before':>15}{'still barred':>14}"
    )
    print("Real FeedJumpDetector over the captured Upstox I1 tape.\n")
    print(header)
    print("-" * len(header))
    for exchange in sorted(after["judged"], key=lambda name: -after["judged"][name]):
        judged = after["judged"][exchange]
        was, now = before["broke"][exchange], after["broke"][exchange]
        print(
            f"{exchange:<11}{judged:>10}"
            f"{was:>9} {was / judged:>5.1%}"
            f"{now:>9} {now / judged:>4.1%}"
            f"{len(before['ever_broken'][exchange]):>15}"
            f"{len(after['left_broken'][exchange]):>14}"
        )
    print("-" * len(header))
    print()
    standing = after["standing"]
    print(f"symbols with a measured rhythm   {standing.symbols_with_a_measured_rhythm}")
    print(f"judgements inside a widened bound {standing.checks_inside_a_widened_bound}")
    print(f"breaks reported                  {standing.jumps_found}")
    print(f"continuity restored              {standing.continuity_restored}")
    print(f"still discontinuous at the end   {standing.symbols_discontinuous_now}")
    print()
    barred_before = sum(len(names) for names in before["ever_broken"].values())
    still_barred = sum(len(names) for names in after["left_broken"].values())
    print(
        f"Before: {barred_before} symbols were flagged at least once and, with no caller "
        "for clear_feed_jump, none was ever released."
    )
    print(
        f"After: {still_barred} are left discontinuous when the tape ends -- every other "
        "symbol that broke was reported continuous again, which is what lifts the bar."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
