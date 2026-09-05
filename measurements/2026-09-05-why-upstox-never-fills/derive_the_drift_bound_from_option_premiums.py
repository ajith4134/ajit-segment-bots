"""What `maximum_decision_price_drift` should be for Indian option premiums.

The bound in force is 0.4% and its own note says where it came from: "BTCUSDT
traded through 0.133% in the 28 seconds captured on 2026-08-22". It was measured
on crypto spot. An option premium is a leveraged claim on its underlying and does
not move in that range -- on 2026-09-04 the contracts these bots actually traded
moved p90 1.20% in a single second, three times the entire bound.

The window the bound has to admit is not arbitrary. `order_latency_maximum` caps
how long the simulated round trip holds an order at 5 seconds, so once the
decision chain is healthy the gap between pricing a decision and filling it is
bounded by that. This measures how far the traded contracts moved over exactly
that horizon, on their own prints, and reports the quantiles a bound would sit at.

Reported per contract as well as pooled, because a far out-of-the-money expiry-week
contract and a near-the-money monthly are not one population, and a bound set on
the pooled figure alone would be loose for one and tight for the other.

What this cannot do is separate a fresh decision from a stale one by price. Over
5 seconds p99 is one number and over 30 seconds p90 is a similar one, so drift
alone cannot tell a legitimate fast move from an old decision. That is what the
setting's own note means by "it comes down as decision staleness is measured and
fixed rather than guarded against" -- the age belongs on the order.
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

from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_tape_index

DAY = "2026-09-04"
JOURNAL = pathlib.Path.home() / ".local/share/ajit-segment-bots/journal.trade-lifecycle-recorder.sqlite"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTERS = pathlib.Path(__file__).resolve().parents[2] / "tests/captured/upstox"
JOURNAL_TAIL_BYTES = 8_000_000
NS_PER_SECOND = 1_000_000_000


def symbols_actually_traded() -> set[str]:
    size = JOURNAL.stat().st_size
    symbols = set()
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
                symbols.add(record["payload"]["symbol"])
    return symbols


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


def moves_over(series, horizon_ns: int) -> list[float]:
    times, prices = series
    observed = []
    for position in range(len(times)):
        later = bisect.bisect_left(times, times[position] + horizon_ns)
        if later >= len(times):
            break
        observed.append(abs(prices[later] - prices[position]) / prices[position])
    return observed


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


def main() -> int:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    in_force = float(document.read_value("maximum_decision_price_drift"))
    horizon = float(document.read_value("order_latency_maximum"))
    print(f"maximum_decision_price_drift in force: {in_force:.2%}")
    print(f"order_latency_maximum (the window a decision must survive): {horizon:g}s\n")

    keys = instrument_key_by_trading_symbol()
    pooled: list[float] = []
    per_contract = []
    for symbol in sorted(symbols_actually_traded()):
        key = keys.get(symbol)
        if key is None:
            continue
        series = price_series(key)
        if series is None:
            continue
        moves = moves_over(series, int(horizon * NS_PER_SECOND))
        if not moves:
            continue
        pooled.extend(moves)
        per_contract.append((symbol, len(moves), statistics.median(moves),
                             percentile(moves, 0.99)))

    if not pooled:
        print("NOT MEASURED: no traded contract has prints on the tape")
        return 1

    print(f"per contract, moves over {horizon:g}s:")
    print(f"  {'contract':32} {'n':>7} {'median':>9} {'p99':>9}")
    for symbol, count, median, p99 in sorted(per_contract, key=lambda row: -row[3]):
        print(f"  {symbol:32} {count:7} {median:8.2%} {p99:8.2%}")

    print(f"\npooled over {len(pooled)} observations on {len(per_contract)} contracts:")
    for fraction in (0.50, 0.90, 0.95, 0.99, 0.999):
        print(f"  p{fraction*100:<5g} {percentile(pooled, fraction):7.2%}")
    print(f"  worst  {max(pooled):7.2%}")

    refused_now = sum(1 for move in pooled if move > in_force)
    print(f"\n  ordinary {horizon:g}s movement the {in_force:.2%} bound would refuse: "
          f"{refused_now} of {len(pooled)}  ({refused_now/len(pooled):.1%})")
    widest = max(p99 for _, _, _, p99 in per_contract)
    print(f"  loosest contract's p99: {widest:.2%}  -- a bound below this refuses ordinary")
    print(f"  movement on that contract alone, which is how one name goes untradeable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
