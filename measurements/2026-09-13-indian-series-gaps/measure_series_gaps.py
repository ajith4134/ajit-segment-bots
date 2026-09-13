#!/usr/bin/env python3
"""How long NSE options go quiet in ordinary trading, and how often the series gap rule
mistakes that quiet for a hole in the feed.

The rule (`runtime.rolling_statistics.RollingWindow`): a gap longer than
max(price_series_maximum_gap_seconds, gap_patience_multiple x the series' own p99 gap)
clears the window -- but until the window has seen `length // 2` gaps, only the floor
applies. Its purpose, in the setting's own note: "ordinary quiet does not clear a window
and a dead connection always does".

The data has no dead connections: Upstox one-minute history, 2026-07-01..2026-09-11, the
40 option contracts measured in 2026-09-13-indian-option-retracements (cached), in which
Upstox writes every minute and a minute with no trade carries zero volume. So a trade
time is a bar with volume, a silence is the stretch between two such bars (resolution
one minute; overstates a silence by at most 60 s), and every window clearing inside a
session is a false one. Session boundaries are real holes and are not counted.

    .venv/bin/python measurements/2026-09-13-indian-series-gaps/measure_series_gaps.py
"""
import collections
import datetime
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from runtime.rolling_statistics import RollingWindow  # noqa: E402

HISTORY = pathlib.Path.home() / ".local/share/ajit-segment-bots/history"
WINDOW = 256


def sessions():
    """(contract, day, [(at_ns, close)]) for every session, trades only."""
    out = []
    for path in sorted(HISTORY.rglob("*.json")):
        if "NSE_FO" not in path.as_posix():
            continue
        try:
            candles = (json.loads(path.read_text()).get("data") or {}).get("candles") or []
        except (ValueError, OSError, AttributeError):
            continue
        by_day = collections.defaultdict(list)
        for c in candles:
            if c[5]:
                at = datetime.datetime.fromisoformat(c[0])
                by_day[c[0][:10]].append((int(at.timestamp() * 1e9), float(c[4])))
        for day, rows in by_day.items():
            out.append((path.parent.name, day, sorted(rows)))
    return out


class VariantWindow(RollingWindow):
    """The shipped rule with the warm-up gap count as a parameter, for comparison only."""

    warmup_gaps: int | None = None

    def _gap_bound_seconds(self) -> float:
        if self.warmup_gaps is None:
            return super()._gap_bound_seconds()
        gaps = self._recent_gaps_seconds
        if len(gaps) < max(2, self.warmup_gaps):
            return self.maximum_gap_seconds
        ordered = sorted(gaps)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return max(self.maximum_gap_seconds, self.gap_patience_multiple * p99)


def false_breaks(rows, floor, multiple, warmup):
    window = VariantWindow(length=WINDOW, maximum_gap_seconds=floor, gap_patience_multiple=multiple)
    window.warmup_gaps = warmup
    for at, price in rows:
        window.observe(price, at)
    return window.series_breaks, len(window.values)


def outage_caught(floor, multiple, warmup, rows, outage_seconds):
    """Insert a feed hole of `outage_seconds` after the first half of a session: does it clear?"""
    if len(rows) < 40:
        return None
    half = len(rows) // 2
    shift = int(outage_seconds * 1e9) + (rows[half][0] - rows[half - 1][0])
    shifted = rows[:half] + [(at + shift, price) for at, price in rows[half:]]
    window = VariantWindow(length=WINDOW, maximum_gap_seconds=floor, gap_patience_multiple=multiple)
    window.warmup_gaps = warmup
    before = None
    for i, (at, price) in enumerate(shifted):
        if i == half:
            before = window.series_breaks
        window.observe(price, at)
        if i == half:
            return window.series_breaks > before


def main() -> int:
    data = sessions()
    gaps, longest = [], []
    for _, _, rows in data:
        g = [(b[0] - a[0]) / 1e9 for a, b in zip(rows, rows[1:])]
        gaps.extend(g)
        if g:
            longest.append(max(g))
    gaps.sort(); longest.sort()
    q = lambda v, p: v[min(len(v) - 1, int(p * len(v)))]
    print(f"{len(data)} option sessions, {len(gaps):,} gaps between trading minutes")
    print(f"  gap p50 {q(gaps,.5):.0f}s  p90 {q(gaps,.9):.0f}s  p95 {q(gaps,.95):.0f}s  p99 {q(gaps,.99):.0f}s  p99.9 {q(gaps,.999):.0f}s")
    print(f"  longest silence per session: p50 {q(longest,.5):.0f}s  p90 {q(longest,.9):.0f}s  p99 {q(longest,.99):.0f}s")
    print(f"  sessions with any silence over 150s: {sum(l > 150 for l in longest) / len(longest):.1%}")

    print(f"\nfalse window clearings inside a session (window {WINDOW}), by rule:")
    variants = (
        ("as set: floor 150s, warm-up length//2 = 128 gaps", 150.0, None),
        ("floor 150s, warm-up 64 gaps", 150.0, 64),
        ("floor 150s, warm-up 32 gaps", 150.0, 32),
        ("floor 150s, warm-up 16 gaps", 150.0, 16),
        ("floor 150s, warm-up 8 gaps", 150.0, 8),
        ("floor 660s (option p99 gap), warm-up 128", 660.0, None),
        ("floor 660s, warm-up 32 gaps", 660.0, 32),
    )
    liquid = [rows for _, _, rows in data if len(rows) >= 300]
    for label, floor, warmup in variants:
        breaks, full = [], 0
        for _, _, rows in data:
            b, held = false_breaks(rows, floor, 2.8, warmup)
            breaks.append(b); full += held >= WINDOW
        caught = [outage_caught(floor, 2.8, warmup, rows, 300) for rows in liquid]
        caught = [c for c in caught if c is not None]
        print(f"  {label:44s} false clearing in {sum(b > 0 for b in breaks) / len(breaks):5.1%} of sessions, "
              f"{statistics.fmean(breaks):5.2f}/session, window full at end {full / len(data):5.1%}; "
              f"a 5-min feed hole mid-session on the {len(caught)} liquid sessions caught {sum(caught) / len(caught):5.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
