"""How big a real trade is on this tape, in quote currency.

Two of the vertical's numbers are a size rather than a threshold, and a size chosen
by eye is the easiest kind of number to get quietly wrong:

  - `bull-feature-builder` measures book impact against a reference order size. Set
    it far above what actually trades and every symbol looks illiquid; far below and
    every symbol looks bottomless.
  - the first paper order has to be some size, and one that no real trade on this
    tape resembles would make the fill simulation a fiction from the first message.

So this reports what actually traded: quote volume per trade, per venue, in
quantiles. Binance figures are 100 ms aggregates and Bybit's are single prints,
which is the fidelity difference NormalisedTrade carries -- an aggregate is several
prints added together, so the two columns are not the same quantity and the table
says so rather than averaging them.

Run:  PYTHONPATH=. .venv/bin/python measurements/2026-08-22-vertical-numbers/measure_trade_sizes.py
"""

from __future__ import annotations

import datetime
import json
import pathlib
import statistics

from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
SYMBOLS_PER_VENUE = 6
TRADES_PER_SYMBOL = 20_000
QUANTILES = (0.5, 0.75, 0.9, 0.95, 0.99)


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    sized = []
    for symbol_directory in (TAPE_ROOT / venue_id).iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists() and index_path.stat().st_size > 0:
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


def quantiles_of(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        f"q{int(q * 100):02d}": round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)
        for q in QUANTILES
    }


def measure_symbol(venue_id: str, symbol: str, day: str) -> dict:
    adapter = load_venue_adapter(venue_id)
    index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
    blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
    quote_volumes = []
    for record in read_tape_index(index_path):
        for trade in adapter.read_trades(read_payload(blob_path, record)):
            quote_volumes.append(trade.quote_volume)
        if len(quote_volumes) >= TRADES_PER_SYMBOL:
            break
    if not quote_volumes:
        return {"venue_id": venue_id, "symbol": symbol, "trades": 0}
    return {
        "venue_id": venue_id,
        "symbol": symbol,
        "trades": len(quote_volumes),
        "mean_quote": round(statistics.fmean(quote_volumes), 2),
        "total_quote": round(sum(quote_volumes), 2),
        **quantiles_of(quote_volumes),
    }


def main() -> None:
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    rows = []
    for venue_id in ("binance-usdm", "bybit-linear"):
        for symbol in busiest_symbols(venue_id, day, SYMBOLS_PER_VENUE):
            rows.append(measure_symbol(venue_id, symbol, day))
    pooled = {}
    for venue_id in ("binance-usdm", "bybit-linear"):
        medians = [row["q50"] for row in rows if row["venue_id"] == venue_id and row.get("q50")]
        if medians:
            pooled[venue_id] = {
                "symbols": len(medians),
                "median_of_symbol_medians": round(statistics.median(medians), 2),
                "smallest_symbol_median": round(min(medians), 2),
                "largest_symbol_median": round(max(medians), 2),
            }
    print(json.dumps({"day": day, "per_symbol": rows, "pooled": pooled}, indent=2))


if __name__ == "__main__":
    main()
