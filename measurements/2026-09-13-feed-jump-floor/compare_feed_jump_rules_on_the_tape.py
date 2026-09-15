"""Which feed-jump rule lets ordinary NSE bars through and still catches a real break.

feed-jump-detector's floor does two jobs with one number (measurements/2026-09-13-indian-
guard-remainder/): it is the whole bound for a symbol's first feed_jump_moves_needed moves,
and the permanent minimum after, bound = max(floor, patience x own p99). At 0.005 half of
every symbol's first eight bars are called a jump; raised to what contracts need (~0.18) it
would permanently blind the detector on an index. And the tick floor it prefers where a
symbol declares an increment is never used, because nothing feeds it increments -- though
tick-size-resolver publishes a declared increment for 1,974 Upstox symbols.

Rules compared, each replayed over the same bars:

    live      floor = fraction; after warm-up max(floor, patience x p99). No ticks.
    ticks     the live rule with each symbol's declared tick fed in, so the floor is
              feed_jump_threshold_increments ticks where a tick is known.
    split     the proposed rule: during warm-up max(N ticks, F); after warm-up
              max(N ticks, patience x p99) -- the fraction floor no longer outlives
              warm-up, and a few ticks stay the permanent minimum so a flat contract's
              one-tick move is never a jump.

Two measures per instrument kind (NSE_FO contracts, NSE_EQ shares, NSE_INDEX):

    ordinary  share of judged bars flagged, excluding bars that follow a recording hole
    hole      share of bars that follow a tape recording hole that are flagged -- a
              recording hole (no bar on any instrument for over two minutes, a spine
              restart) is a real discontinuity in what the feed delivered, the one kind
              the tape can prove without an invented fixture

The bars are the I1 candles broker-candle-bridge republishes, last record per bar, in
session, 2026-09-07/08. Ticks from the instrument master in paise, converted as
runtime/brokers/upstox.tick_size_in_rupees does.

Run:  .venv/bin/python measurements/2026-09-13-feed-jump-floor/compare_feed_jump_rules_on_the_tape.py
"""

from __future__ import annotations

import collections
import gzip
import json
import pathlib
import sys

import numpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.brokers.upstox import tick_size_in_rupees  # noqa: E402
from runtime.tape import read_payload, read_tape_index  # noqa: E402

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
DAYS = ("2026-09-07", "2026-09-08")
SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60
SESSION_CLOSE_UTC_SECONDS = 10 * 3600
HOLE_SECONDS = 120
PATIENCE = 2.8
MOVES_NEEDED = 8
MOVES_REMEMBERED = 256
LIVE_FRACTION = 0.005
LIVE_TICKS = 20
TICK_CANDIDATES = (2, 5, 10, 20)
WARMUP_CANDIDATES = (0.02, 0.05, 0.10, 0.20)
KINDS = ("NSE_FO", "NSE_EQ", "NSE_INDEX")


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def bars_for(directory: pathlib.Path, day: str):
    index_path = directory / f"{day}.candle.index"
    if not index_path.exists():
        return []
    blob = directory / f"{day}.candle.blob"
    bars = {}
    for record in read_tape_index(index_path):
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        at = payload.get("bar_time_ms")
        if payload.get("interval") != "I1" or at is None or not is_in_session(int(at) * 1_000_000):
            continue
        if payload.get("open") and payload.get("close"):
            bars[int(at)] = (float(payload["open"]), float(payload["close"]))
    return [(t, *bars[t]) for t in sorted(bars)]


class Rule:
    """One bound rule and its per-symbol memory; mirrors FeedJumpDetector's bookkeeping."""

    def __init__(self, name, warmup_bound, settled_bound):
        self.name, self._warmup, self._settled = name, warmup_bound, settled_bound
        self._moves = collections.defaultdict(lambda: collections.deque(maxlen=MOVES_REMEMBERED))

    def judge(self, key, price, move, tick) -> bool:
        moves = self._moves[key]
        if len(moves) < MOVES_NEEDED:
            bound = self._warmup(price, tick)
        else:
            ordered = sorted(moves)
            p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
            bound = self._settled(price, tick, p99)
        crossed = move > bound
        if not crossed:
            moves.append(move)
        return crossed


def tick_fraction(count, price, tick):
    return count * tick / price if tick else 0.0


def build_rules():
    rules = [
        Rule("live", lambda price, tick: LIVE_FRACTION,
             lambda price, tick, p99: max(LIVE_FRACTION, PATIENCE * p99)),
        Rule("ticks", lambda price, tick: tick_fraction(LIVE_TICKS, price, tick) if tick else LIVE_FRACTION,
             lambda price, tick, p99: max(tick_fraction(LIVE_TICKS, price, tick) if tick else LIVE_FRACTION,
                                          PATIENCE * p99)),
    ]
    for count in TICK_CANDIDATES:
        for warmup in WARMUP_CANDIDATES:
            rules.append(Rule(
                f"split N={count} F={warmup}",
                (lambda c, w: lambda price, tick: max(tick_fraction(c, price, tick), w))(count, warmup),
                (lambda c: lambda price, tick, p99: max(tick_fraction(c, price, tick), PATIENCE * p99))(count),
            ))
    return rules


def main() -> int:
    ticks = {row["instrument_key"]: tick_size_in_rupees(row.get("tick_size"))
             for row in json.load(gzip.open(MASTER))}
    rules = build_rules()
    ordinary = {r.name: collections.Counter() for r in rules}
    ordinary_judged = collections.Counter()
    hole = {r.name: collections.Counter() for r in rules}
    hole_judged = collections.Counter()
    directories = [p for p in TAPE.iterdir() if p.is_dir() and p.name.split("|", 1)[0] in KINDS]
    for day in DAYS:
        bars_by_key = {d.name: bars_for(d, day) for d in directories}
        bars_by_key = {k: v for k, v in bars_by_key.items() if len(v) >= 2}
        all_times = sorted({t for bars in bars_by_key.values() for t, _, _ in bars})
        hole_ends = {later for earlier, later in zip(all_times, all_times[1:])
                     if (later - earlier) / 1000 > HOLE_SECONDS}
        print(f"{day}: {len(bars_by_key):,} instruments with bars, {len(hole_ends)} recording holes", flush=True)
        for key, bars in bars_by_key.items():
            kind = key.split("|", 1)[0]
            tick = ticks.get(key)
            for (t0, _, close0), (t1, open1, _) in zip(bars, bars[1:]):
                if close0 <= 0:
                    continue
                move = abs(open1 - close0) / close0
                # a bar that follows a recording hole: any hole end between the two bars
                after_hole = any(t0 < h <= t1 for h in hole_ends) if hole_ends else False
                for rule in rules:
                    crossed = rule.judge((day, key), close0, move, tick)
                    (hole if after_hole else ordinary)[rule.name][kind] += crossed
                (hole_judged if after_hole else ordinary_judged)[kind] += 1
    print("\nshare flagged -- ordinary bars / bars after a recording hole")
    header = f"  {'rule':22}" + "".join(f"{k:>24}" for k in KINDS)
    print(header)
    print(f"  {'judged':22}" + "".join(f"{ordinary_judged[k]:>13,} / {hole_judged[k]:>8,}" for k in KINDS))
    for rule in rules:
        cells = []
        for kind in KINDS:
            o = ordinary[rule.name][kind] / ordinary_judged[kind] if ordinary_judged[kind] else float("nan")
            h = hole[rule.name][kind] / hole_judged[kind] if hole_judged[kind] else float("nan")
            cells.append(f"{o:>13.2%} / {h:>8.1%}")
        print(f"  {rule.name:22}" + "".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
