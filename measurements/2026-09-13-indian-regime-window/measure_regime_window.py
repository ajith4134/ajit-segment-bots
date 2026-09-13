#!/usr/bin/env python3
"""How long regime-classifier's window must be on NSE instruments, and how many ever fill it.

regime_window_length = 1024 was measured on crypto prints: "the estimator's standard
deviation falls from 0.26 at 64 trades to 0.046-0.139 at 1024 ... 1024 trades is 1.2-6.8
seconds on the busiest symbols". On 2026-09-13 the live part held 20,901 unclassified
readings. This measures both halves of that derivation on Indian data:

1. The spread of the part's own Hurst estimator (`runtime.rolling_statistics.hurst_exponent`)
   across non-overlapping windows of N consecutive distinct prices, N in 64..1024:
   - the tape's prints for 2026-09-07/08 -- what the live part receives (distinct prints;
     repeated frames are skipped by RollingWindow);
   - Upstox one-minute closes, 2026-07-01..2026-09-11, for the 40 contracts measured in
     2026-09-13-indian-option-retracements (cached), windows inside one session.
2. How many instrument-sessions on the tape hold at least N distinct prints -- a window the
   series never reaches classifies nothing, and a session gap clears the series.

The criterion is the part's own geometry: the classification bands sit 0.1 from the
random-walk value (0.6 and 0.4), so an estimator whose spread across windows exceeds 0.1
flips regime on noise alone.

    .venv/bin/python measurements/2026-09-13-indian-regime-window/measure_regime_window.py
"""
import collections
import datetime
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.rolling_statistics import hurst_exponent  # noqa: E402
from runtime.tape import read_payload, read_tape_index  # noqa: E402

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
HISTORY = pathlib.Path.home() / ".local/share/ajit-segment-bots/history"
DAYS = ("2026-09-07", "2026-09-08")
SIZES = (64, 128, 256, 512, 1024)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def kind_of(name):
    parts = name.split()
    if len(parts) >= 4 and parts[2] in ("CE", "PE"):
        return "option"
    if "FUT" in parts:
        return "future"
    return "share-or-index"


def spreads(series_list, label):
    print(f"\n{label}")
    print(f"  {'N':>5} {'windows':>8} {'median H':>9} {'sd of H':>8} {'p10-p90 of H':>16}")
    for n in SIZES:
        values = []
        for series in series_list:
            for start in range(0, len(series) - n + 1, n):
                h = hurst_exponent(series[start:start + n], n)
                if h is not None:
                    values.append(h)
        if len(values) < 20:
            print(f"  {n:>5} {len(values):>8}   too few windows")
            continue
        values.sort()
        q = lambda p: values[int(p * (len(values) - 1))]
        print(f"  {n:>5} {len(values):>8} {statistics.median(values):>9.3f} "
              f"{statistics.pstdev(values):>8.3f}   {q(.1):.3f}-{q(.9):.3f}")


def tape_series():
    by_kind = collections.defaultdict(list)
    for root in sorted(p for p in TAPE.iterdir() if p.is_dir() and "|" not in p.name):
        for day in DAYS:
            index = root / f"{day}.index"
            if not index.exists():
                continue
            prices = []
            for record in read_tape_index(index):
                try:
                    price = json.loads(read_payload(root / f"{day}.blob", record)).get("last_traded_price")
                except (ValueError, OSError):
                    continue
                if price and (not prices or price != prices[-1]):
                    prices.append(float(price))
            if prices:
                by_kind[kind_of(root.name)].append(prices)
    return by_kind


def history_series():
    series = []
    for path in sorted(HISTORY.rglob("*.json")):
        try:
            document = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        candles = (document.get("data") or {}).get("candles") if isinstance(document, dict) else None
        if not candles or "NSE_FO" not in path.as_posix():
            continue
        by_day = collections.defaultdict(list)
        for candle in sorted(candles, key=lambda c: c[0]):
            by_day[candle[0][:10]].append(float(candle[4]))
        series.extend(closes for closes in by_day.values() if closes)
    return series


def main() -> int:
    by_kind = tape_series()
    print(f"tape {', '.join(DAYS)}: distinct prints per instrument-session")
    for kind, sessions in sorted(by_kind.items()):
        counts = sorted(len(s) for s in sessions)
        reach = "  ".join(f">={n}: {sum(c >= n for c in counts) / len(counts):.1%}" for n in SIZES)
        print(f"  {kind:15s} {len(counts):>5} sessions, median {statistics.median(counts):,.0f} prints   {reach}")
    for kind in ("option", "share-or-index", "future"):
        if kind in by_kind:
            spreads(by_kind[kind], f"Hurst estimator on tape prints: {kind}")
    history = history_series()
    spreads(history, f"Hurst estimator on Upstox one-minute closes, {len(history)} option sessions "
                     f"2026-07-01..2026-09-11 (windows inside one session)")
    return 0




def thresholds_at(n=256):
    """p5 and p95 of the estimator at the chosen window: the operator's own rule for the bands."""
    pooled = []
    by_kind = tape_series()
    for sessions in by_kind.values():
        for series in sessions:
            for start in range(0, len(series) - n + 1, n):
                h = hurst_exponent(series[start:start + n], n)
                if h is not None:
                    pooled.append(("tape", h))
    for series in history_series():
        for start in range(0, len(series) - n + 1, n):
            h = hurst_exponent(series[start:start + n], n)
            if h is not None:
                pooled.append(("history", h))
    for label in ("tape", "history", "all"):
        values = sorted(h for source, h in pooled if label == "all" or source == label)
        q = lambda p: values[int(p * (len(values) - 1))]
        print(f"  N={n} {label:8s} windows {len(values):>5}: p5 {q(.05):.3f}  p50 {q(.5):.3f}  p95 {q(.95):.3f}")


if __name__ == "__main__" and "--thresholds" in sys.argv:
    thresholds_at()


if __name__ == "__main__" and "--thresholds" not in sys.argv:
    sys.exit(main())
