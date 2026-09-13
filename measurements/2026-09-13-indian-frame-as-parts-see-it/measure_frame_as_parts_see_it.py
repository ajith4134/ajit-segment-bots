"""The price windows' settings, measured on everything `symbol-price-frame` actually carries.

Corrects two earlier measurements made on 2026-09-13, each on half the truth:

- measurements/2026-09-13-indian-option-move-sizes/ measured option contracts, but
  counted tape records (only 17.8% of option tape records are a new trade).
- measurements/2026-09-13-indian-observation-cadence/ counted distinct trades, but
  measured underlyings only, on the belief that `symbol-price-frame` carries only
  underlyings. It does not. It has two producers on the live spine:
  broker-underlying-price-frame-bridge (the underlyings) **and price-level-sampler**,
  which reads `market-data` -- every subscribed instrument -- and publishes every
  symbol's latest price. Measured on the live spine 2026-09-13: price-level-sampler
  symbols_tracked 1,995, and regime-classifier's checkpoint holds 1,775 option
  contract series beside 220 underlyings.

So the windows hold both, and one setting serves both. Here: option contracts and F&O
underlyings, each as distinct (last_traded_time_ms, price) trades in session, cut at
tape recording holes, reported separately and pooled. Contracts are what the entry
candidates overwhelmingly name (111,265 of 116,179 in the lifecycle journal's last
150 MB) and 89% of the symbols the frame carries.

Quantities, each by the reading part's own definition:
    bounce peak       bear-entry-timer: peak of (price - mean)/mean per run above the
                      mean over bear_entry_window_length
    completed move    tail_mover_qualifier._sustained_move over tail_window_length with
                      tail_minimum_observations_in_move progressing steps
    pullback          measurements/2026-09-13-indian-option-retracements' definition
    Hurst             runtime.rolling_statistics.hurst_exponent, non-overlapping windows

Run:  .venv/bin/python measurements/2026-09-13-indian-frame-as-parts-see-it/measure_frame_as_parts_see_it.py
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
import statistics
import sys
import tomllib

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
QUANTILES = (0.05, 0.20, 0.50, 0.80, 0.95)
CONTRACT_DAYS = ("2026-09-07", "2026-09-08")
UNDERLYING_DAYS = ("2026-09-04", "2026-09-07", "2026-09-08")


def pieces_for(keys, days):
    """Each instrument's distinct trades, split at the day's tape recording holes."""
    pieces = []
    for day in days:
        series_by_key = {}
        for key in keys:
            trades = cadence.distinct_trades(cadence.TAPE / key, day)
            if len(trades) >= 2:
                series_by_key[key] = trades
        if not series_by_key:
            continue
        for start, end in cadence.recording_pieces(series_by_key):
            for trades in series_by_key.values():
                piece = [price for at, price, _ in trades if start <= at <= end]
                if len(piece) >= 2:
                    pieces.append(piece)
        print(f"  {day}: {len(series_by_key):,} instruments with trades", flush=True)
    return pieces


def measure(pieces, number):
    bounces, moves, pullbacks, hurst = [], [], [], {n: [] for n in SIZES}
    for prices in pieces:
        bounces.extend(cadence.bounce_peaks(prices, int(number("bear_entry_window_length"))))
        moves.extend(cadence.completed_moves(
            prices, int(number("tail_window_length")), int(number("tail_minimum_observations_in_move"))
        ))
        pullbacks.extend(cadence.continuation_pullbacks(prices))
        for n in SIZES:
            for start in range(0, len(prices) - n + 1, n):
                h = hurst_exponent(prices[start:start + n], n)
                if h is not None:
                    hurst[n].append(h)
    return {"bounce": bounces, "move": moves, "pullback": pullbacks, "hurst": hurst,
            "reach": {n: sum(len(p) >= n for p in pieces) / len(pieces) for n in SIZES}}


def report(label, result):
    print(f"\n==== {label} ====")
    for name, key in (("bounce peak over bear_entry_window_length", "bounce"),
                      ("completed sustained move over tail_window_length", "move"),
                      ("continuation pullback", "pullback")):
        values = result[key]
        quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q):.6g}" for q in QUANTILES) if values else "none"
        print(f"  {name:50} n={len(values):>9,}  {quantiles}")
    print(f"  {'Hurst N':>9} {'windows':>8} {'median':>7} {'sd':>6}   {'p5':>6} {'p95':>6}   reaching N")
    for n in SIZES:
        values = result["hurst"][n]
        if len(values) < 20:
            print(f"  {n:>9} {len(values):>8}   too few windows")
            continue
        print(f"  {n:>9} {len(values):>8} {statistics.median(values):>7.3f} {statistics.pstdev(values):>6.3f}   "
              f"{numpy.quantile(values, .05):>6.3f} {numpy.quantile(values, .95):>6.3f}   {result['reach'][n]:.1%}")


def main() -> int:
    settings = tomllib.loads(cadence.SETTINGS.read_text())
    number = lambda name: float(settings[name]["value"])
    master = json.load(gzip.open(cadence.MASTER))
    options = [r for r in master if r.get("segment") == "NSE_FO" and r.get("instrument_type") in ("CE", "PE")]
    underlying_keys = sorted({r["underlying_key"] for r in options})
    contract_keys = sorted(p.name for p in cadence.TAPE.glob("NSE_FO*"))

    print("contracts:", flush=True)
    contracts = measure(pieces_for(contract_keys, CONTRACT_DAYS), number)
    print("underlyings:", flush=True)
    underlyings = measure(pieces_for(underlying_keys, UNDERLYING_DAYS), number)
    pooled = {
        key: (contracts[key] + underlyings[key]) if key in ("bounce", "move", "pullback")
        else ({n: contracts[key][n] + underlyings[key][n] for n in SIZES} if key == "hurst" else contracts[key])
        for key in contracts
    }
    report(f"OPTION CONTRACTS, {', '.join(CONTRACT_DAYS)}", contracts)
    report(f"F&O UNDERLYINGS, {', '.join(UNDERLYING_DAYS)}", underlyings)
    report("POOLED, as the frame carries both (reach column is contracts')", pooled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
