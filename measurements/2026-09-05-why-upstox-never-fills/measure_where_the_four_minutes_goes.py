"""Which stage of the decision chain spends the four minutes.

A decision took a median 243s to become a routed order on 2026-09-04. That is the
whole chain, and a whole-chain number names no part. The journal timestamps each
stage as the part that produced it stamped it, so the gaps between them localise
the delay:

    entry-candidate.detected_at_ns -> trade-intent.formed_at_ns
    trade-intent.formed_at_ns      -> bounded-order.bounded_at_ns
    bounded-order.bounded_at_ns    -> order-request.routed_at_ns

Joined per symbol in time order, because the ids do not match across stages (an
order-request carries a client-order hash where the intent carries "upstox|SYMBOL").
Each stage is matched to the most recent earlier record of the stage before it on
the same symbol, which is what "the decision this order came from" means when
there is no shared id to follow.
"""

from __future__ import annotations

import bisect
import collections
import datetime
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

DAY = "2026-09-04"
JOURNAL = pathlib.Path.home() / ".local/share/ajit-segment-bots/journal.trade-lifecycle-recorder.sqlite"
JOURNAL_TAIL_BYTES = 8_000_000
NS_PER_SECOND = 1_000_000_000

# The moment each stage stamps as its own.
STAMPED_AT = {
    "entry-candidate": "detected_at_ns",
    "trade-intent": "formed_at_ns",
    "bounded-order": "bounded_at_ns",
    "order-request": "routed_at_ns",
}
CHAIN = ("entry-candidate", "trade-intent", "bounded-order", "order-request")


def friday_stages() -> dict[str, dict[str, list[int]]]:
    """Per stage, per symbol, every moment that stage stamped -- sorted."""
    size = JOURNAL.stat().st_size
    stages: dict[str, dict[str, list[int]]] = {
        kind: collections.defaultdict(list) for kind in STAMPED_AT
    }
    with open(JOURNAL, "rb") as journal:
        journal.seek(max(size - JOURNAL_TAIL_BYTES, 0))
        journal.readline()
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("kind")
            if kind not in STAMPED_AT:
                continue
            at = datetime.datetime.fromtimestamp(record.get("recorded_at_ns", 0) / 1e9, datetime.UTC)
            if at.strftime("%Y-%m-%d") != DAY:
                continue
            payload = record["payload"]
            moment = payload.get(STAMPED_AT[kind])
            symbol = payload.get("symbol")
            if moment and symbol:
                stages[kind][symbol].append(int(moment))
    for by_symbol in stages.values():
        for moments in by_symbol.values():
            moments.sort()
    return stages


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


def gaps_between(earlier: dict[str, list[int]], later: dict[str, list[int]]) -> list[float]:
    """For each later record, how long since the most recent earlier one."""
    measured = []
    for symbol, moments in later.items():
        before = earlier.get(symbol)
        if not before:
            continue
        for moment in moments:
            position = bisect.bisect_right(before, moment) - 1
            if position >= 0:
                measured.append((moment - before[position]) / NS_PER_SECOND)
    return [gap for gap in measured if gap >= 0]


def main() -> int:
    stages = friday_stages()
    print(f"records joined on {DAY}:")
    for kind in CHAIN:
        print(f"  {kind:18} {sum(len(v) for v in stages[kind].values()):6}"
              f"  over {len(stages[kind])} symbols")
    print()
    print(f"  {'stage':>44}  {'median':>9} {'p90':>9} {'worst':>9}  n")
    total_median = 0.0
    for earlier_kind, later_kind in zip(CHAIN, CHAIN[1:]):
        gaps = gaps_between(stages[earlier_kind], stages[later_kind])
        label = f"{earlier_kind} -> {later_kind}"
        if not gaps:
            print(f"  {label:>44}  NOT MEASURED (no pair joined)")
            continue
        median = statistics.median(gaps)
        total_median += median
        print(f"  {label:>44}  {median:8.2f}s {percentile(gaps, 0.90):8.2f}s "
              f"{max(gaps):8.2f}s  {len(gaps)}")
    print(f"\n  {'sum of the stage medians':>44}  {total_median:8.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
