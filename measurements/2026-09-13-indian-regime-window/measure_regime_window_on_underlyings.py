"""regime-classifier's window and Hurst bands, re-measured on the series it actually reads.

measure_regime_window.py beside this fitted regime_window_length (256) and the bands
(0.696 / 0.429) on option contracts and one-minute option closes. regime-classifier reads
`symbol-price-frame`, whose only producer on this feed carries the underlyings
(measurements/2026-09-13-indian-observation-cadence/), so those figures describe a series
the part never sees. Also, that script dropped a repeated price, where RollingWindow keeps
a repeated price at a new trade time and skips only an identical (time, price) re-delivery.

Same rules as before, now on the right series:

- **Window:** the smallest N at which the part's own estimator
  (`runtime.rolling_statistics.hurst_exponent`) has a spread across non-overlapping windows
  under 0.1 -- the distance the original bands sat from the random walk, beyond which the
  classifier flips regime on estimator noise alone.
- **Bands:** p95 and p5 of the estimator at that N ("0.60 is about the 95th percentile").

Series: F&O underlyings (the master's underlying_key of every NSE_FO option), distinct
trades, in session, cut at tape recording holes, on 2026-09-04/07/08 -- the trading days
whose tape holds them.

Run:  .venv/bin/python measurements/2026-09-13-indian-regime-window/measure_regime_window_on_underlyings.py
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
import statistics
import sys

import numpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.rolling_statistics import hurst_exponent  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cadence", ROOT / "measurements/2026-09-13-indian-observation-cadence/measure_indian_observation_cadence.py"
)
cadence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cadence)

SIZES = (64, 128, 256, 512, 1024)
SPREAD_CEILING = 0.1


def underlying_pieces():
    master = json.load(gzip.open(cadence.MASTER))
    keys = {
        row["underlying_key"] for row in master
        if row.get("segment") == "NSE_FO" and row.get("instrument_type") in ("CE", "PE")
    }
    pieces, spans_by_size = [], {n: [] for n in SIZES}
    for day in cadence.UNDERLYING_DAYS:
        series_by_key = {}
        for key in sorted(keys):
            trades = cadence.distinct_trades(cadence.TAPE / key, day)
            if len(trades) >= 2:
                series_by_key[key] = trades
        if not series_by_key:
            continue
        for start, end in cadence.recording_pieces(series_by_key):
            for trades in series_by_key.values():
                piece = [trade for trade in trades if start <= trade[0] <= end]
                if len(piece) < 2:
                    continue
                pieces.append([price for _, price, _ in piece])
                times = [at for at, _, _ in piece]
                for n in SIZES:
                    span = cadence.span_seconds_of(times, n)
                    if span is not None:
                        spans_by_size[n].append(span)
    return pieces, spans_by_size


def main() -> int:
    pieces, spans = underlying_pieces()
    print(f"F&O underlyings, days {', '.join(cadence.UNDERLYING_DAYS)}: {len(pieces)} recorded "
          f"series pieces, {sum(len(p) for p in pieces):,} distinct trades\n")
    print(f"  {'N':>5} {'windows':>8} {'median H':>9} {'sd of H':>8}   {'p5':>6} {'p10':>6} {'p90':>6} {'p95':>6}"
          f"   {'span p50':>9}   {'pieces reaching N':>18}")
    chosen = None
    results = {}
    for n in SIZES:
        values = []
        for series in pieces:
            for start in range(0, len(series) - n + 1, n):
                h = hurst_exponent(series[start:start + n], n)
                if h is not None:
                    values.append(h)
        reach = sum(len(p) >= n for p in pieces) / len(pieces)
        span = numpy.median(spans[n]) if spans[n] else float("nan")
        if len(values) < 20:
            print(f"  {n:>5} {len(values):>8}   too few windows{'':>40} {span:>8.0f}s   {reach:>17.1%}")
            continue
        q = lambda p: float(numpy.quantile(values, p))
        sd = statistics.pstdev(values)
        results[n] = (q(.05), q(.95))
        print(f"  {n:>5} {len(values):>8} {statistics.median(values):>9.3f} {sd:>8.3f}   "
              f"{q(.05):>6.3f} {q(.10):>6.3f} {q(.90):>6.3f} {q(.95):>6.3f}   {span:>8.0f}s   {reach:>17.1%}")
        if chosen is None and sd < SPREAD_CEILING:
            chosen = n
    if chosen is None:
        print("\nno window size brings the estimator's spread under 0.1")
        return 0
    low, high = results[chosen]
    print(f"\nsmallest N with sd under {SPREAD_CEILING}: {chosen}; bands at that N: "
          f"trending above p95 {high:.3f}, reverting below p5 {low:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
