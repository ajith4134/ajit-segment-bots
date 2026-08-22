"""Will a signal label ever resolve, and how long does it take?

`signal-outcome-labeller` calls a detector's claim right or wrong when price moves a
threshold one way or the other inside the claim's horizon, and drops it when neither
happens. Two numbers decide whether the models can train at all:

  - the threshold. Set from costs alone it is 20 basis points -- twice the round
    trip in taker fees. But a threshold the market does not reach inside the horizon
    produces no labels, and a model with no labels never becomes measured, which is
    the exact cycle this part was added to break.
  - the horizon, which the detector sets per candidate, currently 60 seconds.

So this measures the resolution rate directly on real prices: for each threshold and
horizon, what share of claims resolve, how long they take, and how the right and
wrong verdicts split. A threshold that resolves everything within a second is
measuring noise; one that resolves nothing is measuring nothing.

The claims are placed at fixed intervals rather than where a detector fired: what is
being measured is the market's own behaviour, not any detector's timing.

Run:  PYTHONPATH=. .venv/bin/python measurements/2026-08-22-vertical-numbers/measure_label_resolution.py
"""

from __future__ import annotations

import datetime
import json
import pathlib
import statistics

from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"

MOVE_FRACTIONS = (0.0005, 0.001, 0.002, 0.005)
HORIZON_SECONDS = (60.0, 300.0, 900.0)
SYMBOLS_PER_VENUE = 3
TRADES_PER_SYMBOL = 120_000
CLAIMS_PER_SERIES = 400
NANOSECONDS_PER_SECOND = 1e9


def read_series(venue_id: str, symbol: str, day: str, limit: int):
    adapter = load_venue_adapter(venue_id)
    index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
    blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
    prices: list[float] = []
    times: list[int] = []
    for record in read_tape_index(index_path):
        for trade in adapter.read_trades(read_payload(blob_path, record)):
            prices.append(trade.price)
            times.append(trade.venue_time_ns)
            if len(prices) >= limit:
                return prices, times
    return prices, times


def resolve_one(prices, times, start, move_fraction, horizon_ns) -> tuple[str, float]:
    """Walk forward from one claim until a barrier or the horizon. Long side."""
    entry = prices[start]
    deadline = times[start] + horizon_ns
    for index in range(start + 1, len(prices)):
        if times[index] > deadline:
            break
        moved = (prices[index] - entry) / entry
        if moved >= move_fraction:
            return "right", (times[index] - times[start]) / NANOSECONDS_PER_SECOND
        if moved <= -move_fraction:
            return "wrong", (times[index] - times[start]) / NANOSECONDS_PER_SECOND
    return "unresolved", 0.0


def measure_symbol(venue_id: str, symbol: str, day: str) -> dict:
    prices, times = read_series(venue_id, symbol, day, TRADES_PER_SYMBOL)
    if len(prices) < 1000:
        return {"venue_id": venue_id, "symbol": symbol, "trades": len(prices), "too_few": True}

    span_seconds = (times[-1] - times[0]) / NANOSECONDS_PER_SECOND
    starts = list(range(0, len(prices) - 1, max(1, len(prices) // CLAIMS_PER_SERIES)))
    by_setting = {}
    for move_fraction in MOVE_FRACTIONS:
        for horizon in HORIZON_SECONDS:
            horizon_ns = int(horizon * NANOSECONDS_PER_SECOND)
            verdicts = {"right": 0, "wrong": 0, "unresolved": 0}
            seconds = []
            for start in starts:
                verdict, took = resolve_one(prices, times, start, move_fraction, horizon_ns)
                verdicts[verdict] += 1
                if verdict != "unresolved":
                    seconds.append(took)
            resolved = verdicts["right"] + verdicts["wrong"]
            by_setting[f"{move_fraction}@{int(horizon)}s"] = {
                "claims": len(starts),
                "resolved_share": round(resolved / len(starts), 3),
                "right_share_of_resolved": round(verdicts["right"] / resolved, 3) if resolved else None,
                "median_seconds_to_resolve": round(statistics.median(seconds), 2) if seconds else None,
            }
    return {
        "venue_id": venue_id,
        "symbol": symbol,
        "trades": len(prices),
        "market_span_seconds": round(span_seconds, 1),
        "by_setting": by_setting,
    }


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    sized = []
    for symbol_directory in (TAPE_ROOT / venue_id).iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists() and index_path.stat().st_size > 0:
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


def main() -> None:
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    rows = [
        measure_symbol(venue_id, symbol, day)
        for venue_id in ("binance-usdm", "bybit-linear")
        for symbol in busiest_symbols(venue_id, day, SYMBOLS_PER_VENUE)
    ]
    pooled = {}
    for key in rows[0]["by_setting"]:
        shares = [row["by_setting"][key]["resolved_share"] for row in rows if "by_setting" in row]
        rights = [
            row["by_setting"][key]["right_share_of_resolved"]
            for row in rows
            if "by_setting" in row and row["by_setting"][key]["right_share_of_resolved"] is not None
        ]
        pooled[key] = {
            "resolved_share_min": min(shares),
            "resolved_share_median": round(statistics.median(shares), 3),
            "resolved_share_max": max(shares),
            "right_share_median": round(statistics.median(rights), 3) if rights else None,
        }
    print(json.dumps({"day": day, "pooled": pooled, "per_symbol": rows}, indent=2))


if __name__ == "__main__":
    main()
