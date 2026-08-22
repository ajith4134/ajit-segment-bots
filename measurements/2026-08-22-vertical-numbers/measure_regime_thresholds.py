"""What Hurst actually reads on this tape, so the regime thresholds are not invented.

`regime-classifier` needs four numbers: how many prices to keep, how many are
enough to estimate on, and the two thresholds either side of the random-walk value
of 0.5 that separate trending from reverting. RL-061 says each is estimated from
data or set with provenance -- and three of the four can be estimated, because the
tape holds what the market actually did.

What this measures, on real captured trades:

  1. The distribution of the estimator over rolling windows, per symbol. Thresholds
     chosen without it would either fire constantly or never, and both look the
     same from outside: a regime that never changes.
  2. How the estimate settles as the window grows, which is what decides the
     minimum. Below it the same data reads as trending or reverting depending on
     where the window happens to start.
  3. What a window costs in market time, from the tape's own message spacing. A
     window is a count of trades; whether that count is two minutes or two hours
     depends on how fast the symbol trades, and that is measured, not assumed.

Run:  PYTHONPATH=. .venv/bin/python measurements/2026-08-22-vertical-numbers/measure_regime_thresholds.py
"""

from __future__ import annotations

import json
import pathlib
import statistics

from runtime.rolling_statistics import hurst_exponent
from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"

# Windows to compare. Powers of two so the settling test spans an order of
# magnitude either side of the value being chosen.
CANDIDATE_WINDOWS = (64, 128, 256, 512, 1024, 2048)

# How many trades to read per symbol. Enough for thousands of overlapping windows
# at the largest candidate, and small enough to finish in seconds.
TRADES_PER_SYMBOL = 60_000

# How far apart the rolling windows are taken. Overlapping heavily would count the
# same market twice and report a distribution narrower than the real one.
WINDOW_STRIDE = 256

NANOSECONDS_PER_SECOND = 1e9
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def read_prices(venue_id: str, symbol: str, day: str, limit: int) -> tuple[list[float], list[int]]:
    """Real trades off the tape, normalised through the venue's own adapter."""
    adapter = load_venue_adapter(venue_id)
    index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
    blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
    records = read_tape_index(index_path)
    prices: list[float] = []
    times: list[int] = []
    for record in records:
        for trade in adapter.read_trades(read_payload(blob_path, record)):
            prices.append(trade.price)
            times.append(trade.venue_time_ns)
            if len(prices) >= limit:
                return prices, times
    return prices, times


def quantiles_of(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        f"q{int(q * 100):02d}": round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 4)
        for q in QUANTILES
    }


def measure_symbol(venue_id: str, symbol: str, day: str) -> dict:
    prices, times = read_prices(venue_id, symbol, day, TRADES_PER_SYMBOL)
    if len(prices) < max(CANDIDATE_WINDOWS):
        return {"symbol": symbol, "venue_id": venue_id, "trades_read": len(prices), "too_few": True}

    span_seconds = (times[-1] - times[0]) / NANOSECONDS_PER_SECOND
    trades_per_second = len(prices) / span_seconds if span_seconds > 0 else 0.0

    by_window = {}
    for window in CANDIDATE_WINDOWS:
        estimates = []
        for start in range(0, len(prices) - window, WINDOW_STRIDE):
            estimate = hurst_exponent(prices[start : start + window], window)
            if estimate is not None:
                estimates.append(estimate)
        if not estimates:
            continue
        by_window[window] = {
            "windows_measured": len(estimates),
            "market_seconds_per_window": round(window / trades_per_second, 1) if trades_per_second else None,
            "mean": round(statistics.fmean(estimates), 4),
            "standard_deviation": round(statistics.pstdev(estimates), 4),
            **quantiles_of(estimates),
            "share_above_half": round(sum(1 for e in estimates if e > 0.5) / len(estimates), 3),
        }

    return {
        "venue_id": venue_id,
        "symbol": symbol,
        "trades_read": len(prices),
        "market_span_seconds": round(span_seconds, 1),
        "trades_per_second": round(trades_per_second, 2),
        "by_window": by_window,
    }


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    """The symbols with the most trades today -- the ones a first run would watch."""
    venue_root = TAPE_ROOT / venue_id
    sized = []
    for symbol_directory in venue_root.iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists():
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


def main() -> None:
    import datetime

    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    findings = {"day": day, "symbols": []}
    for venue_id in ("binance-usdm", "bybit-linear"):
        for symbol in busiest_symbols(venue_id, day, count=2):
            findings["symbols"].append(measure_symbol(venue_id, symbol, day))
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
