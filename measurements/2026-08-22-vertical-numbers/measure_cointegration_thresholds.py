"""Are there cointegrated pairs on this tape at all, and at what thresholds?

The first paper fill needs an `entry-candidate`, and exactly one detector in the
blueprint can produce one from inputs the phase-3 spine actually has:
`spread-reversion-detector`, fed by `cointegration-pair-finder`. Every other
detector needs a `playbook-rule`, and the playbook is built by the learning loop
from instructions that do not exist until trades have happened.

So this measures the thing the whole phase turns on: whether real symbols on this
tape pass the pair finder's two tests, and at what thresholds. A threshold set too
high finds nothing and the run produces no trade; set too low it calls every pair
cointegrated and the first fill is on a spread that was never a spread.

It runs the real `CointegrationPairFinder` rather than a re-implementation, with
its thresholds opened wide, and reports the distribution of what it measured.

Two properties of the part it also exposes, because they matter more than the
thresholds:

  - the finder aligns two symbols **by observation count, not by time**, so a
    symbol trading 200 times a second is compared against one trading twice a
    second as though their prices were contemporaneous. Trades are replayed here
    in true time order, which is what the live bus delivers, so whatever this
    reports is what the running part would see.
  - it needs `minimum_observations` of *each* symbol before it judges anything.

Run:  PYTHONPATH=. .venv/bin/python measurements/2026-08-22-vertical-numbers/measure_cointegration_thresholds.py
"""

from __future__ import annotations

import itertools
import json
import pathlib
import statistics

from parts.opportunity_scanner.cointegration_pair_finder import (
    COINTEGRATED,
    CORRELATED_ONLY,
    CointegrationPairFinder,
    UNRELATED,
)
from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"

# Thresholds opened as wide as the part allows, so nothing is filtered before it
# can be counted. The reversion floor cannot be zero -- the finder refuses that at
# construction, because a floor of zero calls a drifting spread reverting.
WIDE_OPEN_CORRELATION = 0.0
NEARLY_ZERO_REVERSION = 1e-9

CANDIDATE_WINDOWS = (256, 1024)
SYMBOLS_PER_VENUE = 8
TRADES_PER_SYMBOL = 40_000
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def read_trades_in_time_order(venue_id: str, symbols: list[str], day: str, per_symbol: int):
    """Every symbol's trades, merged into the order the bus would deliver them."""
    adapter = load_venue_adapter(venue_id)
    merged = []
    for symbol in symbols:
        index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
        blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
        if not index_path.exists():
            continue
        read = 0
        for record in read_tape_index(index_path):
            for trade in adapter.read_trades(read_payload(blob_path, record)):
                merged.append((trade.venue_time_ns, trade.symbol, trade.price))
                read += 1
            if read >= per_symbol:
                break
    merged.sort()
    return merged


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    sized = []
    for symbol_directory in (TAPE_ROOT / venue_id).iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists():
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


def quantiles_of(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        f"q{int(q * 100):02d}": round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 4)
        for q in QUANTILES
    }


def measure_venue(venue_id: str, day: str, window: int) -> dict:
    symbols = busiest_symbols(venue_id, day, SYMBOLS_PER_VENUE)
    trades = read_trades_in_time_order(venue_id, symbols, day, TRADES_PER_SYMBOL)
    finder = CointegrationPairFinder(
        window_length=window,
        minimum_observations=window,
        minimum_correlation=WIDE_OPEN_CORRELATION,
        minimum_reversion_strength=NEARLY_ZERO_REVERSION,
    )
    for _time_ns, symbol, price in trades:
        finder.observe_price(venue_id, symbol, price)

    correlations = []
    reversions = []
    verdicts: dict[str, int] = {}
    strongest = []
    for left, right in itertools.combinations(sorted(symbols), 2):
        pair = finder.test_pair(venue_id, left, right)
        verdicts[pair.state] = verdicts.get(pair.state, 0) + 1
        if pair.correlation is not None:
            correlations.append(abs(pair.correlation))
        if pair.reversion_strength is not None:
            reversions.append(pair.reversion_strength)
            strongest.append((round(pair.reversion_strength, 4), abs(round(pair.correlation or 0, 3)), left, right))
    strongest.sort(reverse=True)

    return {
        "venue_id": venue_id,
        "window": window,
        "symbols": symbols,
        "trades_replayed": len(trades),
        "pairs_tested": sum(verdicts.values()),
        "verdicts": verdicts,
        "absolute_correlation": {
            "count": len(correlations),
            "mean": round(statistics.fmean(correlations), 4) if correlations else None,
            **quantiles_of(correlations),
        },
        "reversion_per_step": {
            "count": len(reversions),
            "mean": round(statistics.fmean(reversions), 5) if reversions else None,
            **quantiles_of(reversions),
        },
        "strongest_five": strongest[:5],
    }


def main() -> None:
    import datetime

    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    findings = {
        "day": day,
        "note": (
            "thresholds opened wide so every pair is measured rather than filtered; "
            f"verdicts are the finder's own states {COINTEGRATED}/{CORRELATED_ONLY}/{UNRELATED}"
        ),
        "measurements": [
            measure_venue(venue_id, day, window)
            for venue_id in ("binance-usdm", "bybit-linear")
            for window in CANDIDATE_WINDOWS
        ],
    }
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
