"""How far the market had moved by the time each of Friday's 325 orders was routed.

The companion script proves one order was refused by `maximum_decision_price_drift`.
One order is an anecdote. This measures the whole population: for every
order-request journalled on 2026-09-04, the price its decision was made at against
the price that contract was actually printing on the tape when the order was
routed, and how that gap compares with the 0.4% bound the book enforces.

The bound's own note says where it came from: "BTCUSDT traded through 0.133% in
the 28 seconds captured on 2026-08-22". It was measured on crypto spot and never
re-derived for the pivot. An option premium is a leveraged claim on its
underlying, so it moves percentage-wise in a different range entirely -- and this
is what that difference costs in refused orders.
"""

from __future__ import annotations

import bisect
import datetime
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_tape_index

DAY = "2026-09-04"
JOURNAL = pathlib.Path.home() / ".local/share/ajit-segment-bots/journal.trade-lifecycle-recorder.sqlite"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTERS = pathlib.Path(__file__).resolve().parents[2] / "tests/captured/upstox"
JOURNAL_TAIL_BYTES = 8_000_000


def friday_orders() -> list[dict]:
    size = JOURNAL.stat().st_size
    routed = []
    with open(JOURNAL, "rb") as journal:
        journal.seek(max(size - JOURNAL_TAIL_BYTES, 0))
        journal.readline()
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") != "order-request":
                continue
            at = datetime.datetime.fromtimestamp(record.get("recorded_at_ns", 0) / 1e9, datetime.UTC)
            if at.strftime("%Y-%m-%d") == DAY:
                routed.append(record["payload"])
    return routed


def instrument_key_by_trading_symbol() -> dict[str, str]:
    mapping = {}
    for master in sorted(MASTERS.glob("*.json")):
        try:
            rows = json.loads(master.read_text())
        except ValueError:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("trading_symbol") and row.get("instrument_key"):
                mapping[row["trading_symbol"]] = row["instrument_key"]
    return mapping


def price_series(instrument_key: str):
    """(times, prices) for one contract on Friday, in tape order."""
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


def price_at(series, at_ns: int) -> float | None:
    """The last price printed at or before this moment -- what the book held."""
    times, prices = series
    position = bisect.bisect_right(times, at_ns) - 1
    return prices[position] if position >= 0 else None


def main() -> int:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    bound = float(document.read_value("maximum_decision_price_drift"))
    orders = friday_orders()
    keys = instrument_key_by_trading_symbol()
    print(f"order-requests on {DAY}: {len(orders)}")
    print(f"maximum_decision_price_drift in force: {bound:.2%}\n")

    series_cache: dict[str, object] = {}
    drifts, unmeasurable = [], 0
    for order in orders:
        decided = order.get("decided_at_price")
        key = keys.get(order["symbol"])
        if not decided or key is None:
            unmeasurable += 1
            continue
        if key not in series_cache:
            series_cache[key] = price_series(key)
        series = series_cache[key]
        if series is None:
            unmeasurable += 1
            continue
        market = price_at(series, int(order["routed_at_ns"]))
        if not market:
            unmeasurable += 1
            continue
        drifts.append(abs(market - decided) / decided)

    print(f"orders whose drift can be measured: {len(drifts)}")
    print(f"orders with no tape or no decision price: {unmeasurable}")
    if not drifts:
        print("NOT MEASURED: no order could be joined to a price on the tape")
        return 1

    over = sum(1 for drift in drifts if drift > bound)
    drifts.sort()
    def at(fraction):
        return drifts[min(int(len(drifts) * fraction), len(drifts) - 1)]
    print()
    print(f"  refused by the {bound:.2%} bound : {over} of {len(drifts)}  ({over/len(drifts):.1%})")
    print(f"  median drift                : {statistics.median(drifts):.2%}")
    print(f"  p10 / p50 / p90 / p99       : {at(0.10):.2%} / {at(0.50):.2%} / {at(0.90):.2%} / {at(0.99):.2%}")
    print(f"  worst                       : {max(drifts):.2%}")
    print()
    for candidate in (0.004, 0.01, 0.02, 0.05, 0.10, 0.15, 0.25):
        passed = sum(1 for drift in drifts if drift <= candidate)
        print(f"  a bound of {candidate:6.2%} would pass {passed:4} of {len(drifts)}  ({passed/len(drifts):5.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
