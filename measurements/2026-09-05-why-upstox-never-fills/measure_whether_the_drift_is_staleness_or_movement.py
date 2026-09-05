"""Is Friday's 5.88% median drift a stale decision, or is it what a premium does?

The drift measurement shows 97.5% of Friday's orders were refused for being priced
at "a market that has gone". Two things produce that number and they need opposite
fixes:

  - the decision really is old, and the guard is right. Widening the bound then
    opens positions at prices the bot never saw -- the 2026-08-23 defect, where
    every trade opened six per cent from where it thought it was and both exits
    were already through their triggers before they were placed.
  - the decision is seconds old and an option premium simply moves that far in
    seconds. The guard is then mis-scaled, carrying a bound measured on BTCUSDT
    spot into a market where it means something else.

Telling them apart takes two numbers, both off Friday's own record: how long each
order actually took from the intent being formed to being routed, and how far the
contracts being traded moved over exactly that horizon. If the observed drift
matches what the tape does over the observed delay, it is movement. If it is far
larger, the decision was stale.
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


def friday_records() -> dict[str, list[dict]]:
    size = JOURNAL.stat().st_size
    by_kind: dict[str, list[dict]] = collections.defaultdict(list)
    with open(JOURNAL, "rb") as journal:
        journal.seek(max(size - JOURNAL_TAIL_BYTES, 0))
        journal.readline()
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            at = datetime.datetime.fromtimestamp(record.get("recorded_at_ns", 0) / 1e9, datetime.UTC)
            if at.strftime("%Y-%m-%d") == DAY:
                by_kind[record.get("kind")].append(record)
    return by_kind


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


def moves_over(series, horizon_ns: int, samples: int = 4000) -> list[float]:
    """How far this contract's premium actually travelled over one horizon."""
    times, prices = series
    if len(times) < 2:
        return []
    observed = []
    step = max(len(times) // samples, 1)
    for position in range(0, len(times), step):
        later = bisect.bisect_left(times, times[position] + horizon_ns)
        if later >= len(times):
            break
        observed.append(abs(prices[later] - prices[position]) / prices[position])
    return observed


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


def main() -> int:
    records = friday_records()
    orders = [record["payload"] for record in records.get("order-request", [])]
    print(f"order-requests on {DAY}: {len(orders)}")

    # How long a decision took to become a routed order, from the journal's own
    # timestamps: the intent's formation against the moment the order was routed.
    # Joined by symbol and time, not by trade_id: an order-request carries a
    # client-order hash as its trade_id while the intent carries "upstox|SYMBOL",
    # so the two ids never match. That mismatch is its own finding -- nothing
    # downstream can join a fill back to the opinion that asked for it.
    formed_by_symbol: dict[str, list[int]] = collections.defaultdict(list)
    for record in records.get("trade-intent", []):
        payload = record["payload"]
        if payload.get("formed_at_ns") and payload.get("symbol"):
            formed_by_symbol[payload["symbol"]].append(payload["formed_at_ns"])
    for moments in formed_by_symbol.values():
        moments.sort()
    delays = []
    for order in orders:
        moments = formed_by_symbol.get(order.get("symbol", ""))
        if not moments or not order.get("routed_at_ns"):
            continue
        position = bisect.bisect_right(moments, order["routed_at_ns"]) - 1
        if position >= 0:
            delays.append((order["routed_at_ns"] - moments[position]) / NS_PER_SECOND)
    delays = [delay for delay in delays if delay >= 0]

    if delays:
        print(f"\nintent formed -> order routed, {len(delays)} joined:")
        print(f"  median {statistics.median(delays):.2f}s   p90 {percentile(delays, 0.90):.2f}s   "
              f"worst {max(delays):.2f}s")
        horizon_seconds = statistics.median(delays)
    else:
        print("\nNOT MEASURED: no order-request could be joined to its intent by trade_id")
        horizon_seconds = None

    # What a premium does over that same horizon, on the contracts actually traded.
    keys = instrument_key_by_trading_symbol()
    traded = {order["symbol"] for order in orders}
    horizons = [h for h in (1.0, 5.0, 30.0, 300.0, horizon_seconds) if h]
    print("\nhow far the traded contracts' premiums actually moved, on Friday's tape:")
    print(f"  {'horizon':>10}  {'median':>8} {'p90':>8} {'p99':>8}   samples")
    for horizon in sorted(set(round(h, 2) for h in horizons)):
        pooled: list[float] = []
        for symbol in traded:
            key = keys.get(symbol)
            if key is None:
                continue
            series = price_series(key)
            if series:
                pooled.extend(moves_over(series, int(horizon * NS_PER_SECOND)))
        if pooled:
            print(f"  {horizon:9.2f}s  {statistics.median(pooled):7.2%} "
                  f"{percentile(pooled, 0.90):7.2%} {percentile(pooled, 0.99):7.2%}   {len(pooled)}")
        else:
            print(f"  {horizon:9.2f}s  NOT MEASURED (no tape for the traded contracts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
