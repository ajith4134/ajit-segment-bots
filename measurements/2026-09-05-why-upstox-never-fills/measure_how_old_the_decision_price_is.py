"""How old is the price an order says it was decided at?

`bounded-order -> order-request` takes 0.01s and `position-sizer` stamps
`entry_price` inside that window, so the number an order carries as
`decided_at_price` ought to be the market of a moment ago. It measures 5.88%
away from the tape instead.

Either the sizer is reading a fresh price and something else is wrong, or the
price reaching it is already old when it arrives. This tells them apart without
guessing: for each of Friday's orders, walk that contract's own prints backwards
from the moment the order was routed and find when the market was last at the
price the order claims it decided at. That timestamp is the age of the decision.

An age near zero means the price was fresh and the drift is elsewhere. An age
that lands on the candidate's detection time means the price rode the pipeline
unchanged -- the decision was priced once, at the top, and never re-priced when
the order was finally formed.
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

from runtime.tape import read_tape_index

DAY = "2026-09-04"
JOURNAL = pathlib.Path.home() / ".local/share/ajit-segment-bots/journal.trade-lifecycle-recorder.sqlite"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTERS = pathlib.Path(__file__).resolve().parents[2] / "tests/captured/upstox"
JOURNAL_TAIL_BYTES = 8_000_000
NS_PER_SECOND = 1_000_000_000
# Close enough to call it the same print: the finest increment these contracts
# are quoted in is 0.05 rupees, so half a tick either way is one price.
SAME_PRICE_WITHIN = 0.025


def friday(kind: str) -> list[dict]:
    size = JOURNAL.stat().st_size
    found = []
    with open(JOURNAL, "rb") as journal:
        journal.seek(max(size - JOURNAL_TAIL_BYTES, 0))
        journal.readline()
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") != kind:
                continue
            at = datetime.datetime.fromtimestamp(record.get("recorded_at_ns", 0) / 1e9, datetime.UTC)
            if at.strftime("%Y-%m-%d") == DAY:
                found.append(record["payload"])
    return found


def instrument_key_by_trading_symbol() -> dict[str, str]:
    mapping = {}
    for master in sorted(MASTERS.glob("*.json")):
        try:
            rows = json.loads(master.read_text())
        except ValueError:
            continue
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("trading_symbol") and row.get("instrument_key"):
                    mapping[row["trading_symbol"]] = row["instrument_key"]
    return mapping


def price_series(instrument_key: str):
    directory = TAPE / instrument_key
    index_path = directory / f"{DAY}.index"
    if not index_path.exists():
        return None
    records = read_tape_index(index_path)
    times, prices = [], []
    with open(directory / f"{DAY}.blob", "rb") as blob:
        for record in records:
            blob.seek(int(record["blob_offset"]))
            try:
                payload = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = payload.get("price") or payload.get("last_traded_price")
            if price:
                times.append(int(record["received_at_ns"]))
                prices.append(float(price))
    return (times, prices) if prices else None


def age_of_the_decision_price(series, routed_at_ns: int, decided_at: float):
    """Seconds back from routing to when the tape was last at that price."""
    times, prices = series
    end = bisect.bisect_right(times, routed_at_ns) - 1
    if end < 0:
        return None
    for position in range(end, -1, -1):
        if abs(prices[position] - decided_at) <= SAME_PRICE_WITHIN:
            return (routed_at_ns - times[position]) / NS_PER_SECOND
    return None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


def main() -> int:
    orders = friday("order-request")
    keys = instrument_key_by_trading_symbol()
    series_cache: dict[str, object] = {}

    ages, never_traded_there = [], 0
    for order in orders:
        decided = order.get("decided_at_price")
        key = keys.get(order.get("symbol", ""))
        if not decided or key is None or not order.get("routed_at_ns"):
            continue
        if key not in series_cache:
            series_cache[key] = price_series(key)
        series = series_cache[key]
        if series is None:
            continue
        age = age_of_the_decision_price(series, int(order["routed_at_ns"]), float(decided))
        if age is None:
            never_traded_there += 1
        else:
            ages.append(age)

    print(f"order-requests on {DAY}: {len(orders)}")
    print(f"  decision price located on the contract's own tape: {len(ages)}")
    print(f"  decision price the contract never printed that day: {never_traded_there}")
    if not ages:
        print("\nNOT MEASURED: no decision price could be located on the tape")
        return 1
    print(f"\nage of the price each order says it decided at, at the moment it was routed:")
    print(f"  median {statistics.median(ages):9.2f}s")
    print(f"  p90    {percentile(ages, 0.90):9.2f}s")
    print(f"  p99    {percentile(ages, 0.99):9.2f}s")
    print(f"  worst  {max(ages):9.2f}s")
    fresh = sum(1 for age in ages if age <= 5.0)
    print(f"\n  priced within 5s of routing: {fresh} of {len(ages)}  ({fresh/len(ages):.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
