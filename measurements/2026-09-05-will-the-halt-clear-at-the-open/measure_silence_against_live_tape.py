#!/usr/bin/env python3
"""Would `market-anomaly-detector` still halt the segment during market hours?

The blocker, measured on the live spine 2026-09-05 with the market shut:
`trading-halt-decider` halted 211,399 of 215,357 decisions for
`the-market-data-does-not-make-sense`, and every one of those traces to
`market-anomaly-detector` reporting `this-venue-has-stopped-updating`.

While the market is shut that is correct -- nothing is printing. The question
this answers is the one that matters for Monday: **during a real session, how
many symbols does that rule still call silent?** It is answered from the tape of
a real Upstox session rather than from the shut-market spine, because the shut
market cannot say anything about the open one.

The rule under test is the part's own (`MarketAnomalyDetector.silence_bound_ns`),
not a paraphrase of it:

    bound = max(stale_after_seconds, patience x that symbol's own p99 gap)

and the widened half only applies once a symbol has shown
`silence_gaps_needed` gaps -- so a symbol that prints rarely earns its patience
slowly, which is exactly the case that matters here.

Run:  .venv/bin/python measurements/2026-09-05-will-the-halt-clear-at-the-open/measure_silence_against_live_tape.py
"""

from __future__ import annotations

import collections
import datetime
import pathlib
import sys
import tomllib

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import StreamKind, read_tape_index, tape_stem_for  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"

# NSE's own session. The open matters most: a bound is judged against how long
# a symbol has been quiet, and every symbol is quiet at 09:15.
SESSION_OPEN = datetime.time(9, 15)
SESSION_CLOSE = datetime.time(15, 30)


def setting(name: str) -> float:
    return float(tomllib.loads(SETTINGS.read_text())[name]["value"])


def session_bounds_ns(day: str) -> tuple[int, int]:
    date = datetime.date.fromisoformat(day)
    open_at = datetime.datetime.combine(date, SESSION_OPEN, tzinfo=IST)
    close_at = datetime.datetime.combine(date, SESSION_CLOSE, tzinfo=IST)
    return int(open_at.timestamp() * 1e9), int(close_at.timestamp() * 1e9)


def print_times_for(symbol_dir: pathlib.Path, day: str) -> numpy.ndarray:
    """When this symbol printed, from the trade stream's own index."""
    stem = tape_stem_for(day, StreamKind.TRADE)
    index_path = symbol_dir / f"{stem}.index"
    if not index_path.exists():
        return numpy.zeros(0, dtype="<u8")
    records = read_tape_index(index_path)
    if len(records) == 0:
        return numpy.zeros(0, dtype="<u8")
    return numpy.asarray(records["received_at_ns"])


def stamp(at_ns: int) -> str:
    return datetime.datetime.fromtimestamp(at_ns / 1e9, tz=IST).strftime("%H:%M:%S")


def earliest_print_ns(day: str) -> int | None:
    """When this day's feed first printed anything at all, across every symbol."""
    earliest = None
    for symbol_dir in TAPE_ROOT.iterdir():
        if not symbol_dir.is_dir():
            continue
        times = print_times_for(symbol_dir, day)
        if len(times) == 0:
            continue
        first = int(times[0])
        if earliest is None or first < earliest:
            earliest = first
    return earliest


def measure(day: str) -> None:
    floor_seconds = setting("anomaly_feed_silent_after_seconds")
    patience = setting("anomaly_feed_silence_patience_multiple")
    gaps_needed = int(setting("anomaly_feed_silence_gaps_needed"))
    open_ns, close_ns = session_bounds_ns(day)

    print(f"day {day}   session {SESSION_OPEN}-{SESSION_CLOSE} IST")
    print(f"floor {floor_seconds:.0f}s   patience {patience}x p99   "
          f"gaps needed {gaps_needed}\n")

    # Anchored to when the feed itself was alive, not to the exchange's clock.
    # On both recorded sessions the feed's first print was hours after the open
    # -- 14:07 IST on 2026-09-02, 10:33 on 2026-09-04 -- because the instrument
    # master was reaching broker-market-feed-reader 532 rows at a time until
    # a8bdbb0 fixed it that night. Judging the silence rule against the session
    # clock therefore measures that outage and calls it a silent symbol, which
    # is the wrong question: what Monday needs to know is what the rule does
    # once data is flowing.
    feed_alive_from = earliest_print_ns(day)
    if feed_alive_from is None:
        print("  no prints at all on this day\n")
        return
    print(f"feed's first print {stamp(feed_alive_from)} IST -- checkpoints are "
          f"measured from there, not from the open\n")
    checkpoints = {
        "+5 min of live feed": feed_alive_from + 5 * 60 * 10**9,
        "+1 hour of live feed": feed_alive_from + 3600 * 10**9,
        "+2 hours of live feed": feed_alive_from + 2 * 3600 * 10**9,
        "+4 hours of live feed": feed_alive_from + 4 * 3600 * 10**9,
    }
    verdicts = {name: collections.Counter() for name in checkpoints}
    bounds_seen = {name: [] for name in checkpoints}

    symbols_with_prints = 0
    for symbol_dir in sorted(TAPE_ROOT.iterdir()):
        if not symbol_dir.is_dir():
            continue
        times = print_times_for(symbol_dir, day)
        in_session = times[(times >= open_ns) & (times <= close_ns)]
        if len(in_session) == 0:
            continue
        symbols_with_prints += 1

        for name, at_ns in checkpoints.items():
            so_far = in_session[in_session <= at_ns]
            if len(so_far) == 0:
                verdicts[name]["never printed yet"] += 1
                continue
            gaps = numpy.diff(so_far) / 1e9
            # The part's own rule, applied exactly.
            bound = floor_seconds
            if len(gaps) >= gaps_needed:
                bound = max(floor_seconds, patience * float(numpy.percentile(gaps, 99)))
            bounds_seen[name].append(bound)
            quiet_for = (at_ns - int(so_far[-1])) / 1e9
            if quiet_for > bound:
                verdicts[name]["HALTED as silent"] += 1
            else:
                verdicts[name]["trading"] += 1

    print(f"{symbols_with_prints} symbols printed at all during this session\n")
    for name in checkpoints:
        counts = verdicts[name]
        total = sum(counts.values())
        halted = counts["HALTED as silent"] + counts["never printed yet"]
        widened = [b for b in bounds_seen[name] if b > floor_seconds]
        share = (100.0 * halted / total) if total else 0.0
        print(f"  {name:20s} {halted:6d} of {total:6d} halted  ({share:5.1f}%)"
              f"   {len(widened)} symbols had earned a widened bound")
        for reason, count in counts.most_common():
            print(f"      {reason:22s} {count}")
    print()


if __name__ == "__main__":
    days = sys.argv[1:] or ["2026-09-04", "2026-09-02"]
    for day in days:
        measure(day)
