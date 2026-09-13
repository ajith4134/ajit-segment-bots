"""Three settings the drift guard still counts, each measured on the NSE tape.

1. feed_jump_threshold_fraction (0.005, "above the 99th percentile boundary move on every
   symbol measured" -- six perpetuals). feed-jump-detector judges a symbol against this
   floor until it has feed_jump_moves_needed (8) of its own close-to-open moves, then
   against feed_jump_patience_multiple x its own p99. So the floor decides the first
   eight bars of every symbol. Measured on the I1 bars broker-candle-bridge republishes
   (last record per bar_time_ms), per symbol: |open - previous close| / previous close.

2. reference_price_maximum_age_seconds (60, "at sixty seconds every symbol's
   95th-percentile drift is at or above the median stop distance ... a price that old
   cannot place a stop at all"). Measured: p95 |price change| over an age A on option
   contract distinct trades (the instruments instrument-selector prices), against the
   option stop distance this project uses elsewhere, 6.09% (p80 option pullback,
   measurements/2026-09-13-indian-option-retracements/).

3. volatility_gap_minimum_fraction (0.2, "the spread between the two on BTC on an
   ordinary day in the literature ... unmeasured here"). The detector's gap is
   (implied - forecast) / forecast. No fitted forecast exists yet (realised-vol-
   regressor forecasts_produced 0), so the ordinary gap is measured against the
   underlying's realised volatility the same day: annualised from one-minute log returns
   of its distinct trades, against the median implied_volatility Upstox states for its
   near-the-money contracts (0.4 <= |delta| <= 0.6), per underlying-day.

Run:  .venv/bin/python measurements/2026-09-13-indian-guard-remainder/measure_jump_drift_and_vol_gap.py
"""

from __future__ import annotations

import bisect
import collections
import gzip
import importlib.util
import json
import math
import pathlib
import sys

import numpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.tape import read_payload, read_tape_index  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cadence", ROOT / "measurements/2026-09-13-indian-observation-cadence/measure_indian_observation_cadence.py"
)
cadence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cadence)

DAYS = ("2026-09-07", "2026-09-08")
FLOOR_CANDIDATES = (0.005, 0.01, 0.02, 0.05, 0.10, 0.20)
MOVES_NEEDED = 8
AGES_SECONDS = (15, 30, 60, 120, 300)
OPTION_STOP_FRACTION = 0.0609
SESSION_MINUTES = 375
TRADING_DAYS_PER_YEAR = 252


def closed_bar_moves(directory: pathlib.Path, day: str) -> list[float]:
    index_path = directory / f"{day}.candle.index"
    if not index_path.exists():
        return []
    blob = directory / f"{day}.candle.blob"
    bars = {}
    for record in read_tape_index(index_path):
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        if payload.get("interval") != "I1" or payload.get("bar_time_ms") is None:
            continue
        if not cadence.is_in_session(int(payload["bar_time_ms"]) * 1_000_000):
            continue
        bars[int(payload["bar_time_ms"])] = (float(payload.get("open") or 0), float(payload.get("close") or 0))
    ordered = [bars[t] for t in sorted(bars)]
    return [
        abs(nxt[0] - prev[1]) / prev[1]
        for prev, nxt in zip(ordered, ordered[1:])
        if prev[1] > 0 and nxt[0] > 0
    ]


def drifts_at_ages(times, prices):
    out = {age: [] for age in AGES_SECONDS}
    for age in AGES_SECONDS:
        age_ns = age * 1_000_000_000
        anchor = 0
        for position in range(1, len(times)):
            while anchor < position and times[position] - times[anchor] > age_ns:
                anchor += 1
            # the last trade at least `age` old: anchor - 1 once the window slid past it
            if anchor > 0 and times[position] - times[anchor - 1] >= age_ns:
                out[age].append(abs(prices[position] / prices[anchor - 1] - 1.0))
    return out


def realised_vol_annualised(times, prices) -> float | None:
    minute_close = {}
    for at, price in zip(times, prices):
        minute_close[at // 60_000_000_000] = price
    closes = [minute_close[m] for m in sorted(minute_close)]
    if len(closes) < 60:
        return None
    returns = numpy.diff(numpy.log(numpy.asarray(closes)))
    return float(numpy.std(returns) * math.sqrt(SESSION_MINUTES * TRADING_DAYS_PER_YEAR))


def main() -> int:
    master = json.load(gzip.open(cadence.MASTER))
    options = {r["instrument_key"]: r for r in master
               if r.get("segment") == "NSE_FO" and r.get("instrument_type") in ("CE", "PE")}
    underlying_keys = sorted({r["underlying_key"] for r in options.values()})
    contracts_by_underlying = collections.defaultdict(list)
    for key, row in options.items():
        contracts_by_underlying[row["underlying_key"]].append(key)

    # ---- 1. feed jump floor -------------------------------------------------------
    per_symbol_p99, warmup_moves, all_moves = [], [], []
    for day in DAYS:
        for directory in sorted(cadence.TAPE.glob("NSE_FO*")) + [cadence.TAPE / k for k in underlying_keys]:
            moves = closed_bar_moves(directory, day)
            if not moves:
                continue
            warmup_moves.extend(moves[:MOVES_NEEDED])
            all_moves.extend(moves)
            if len(moves) >= 20:
                per_symbol_p99.append(float(numpy.quantile(moves, 0.99)))
    print("-- 1. close-to-open move between closed I1 bars, contracts and F&O underlyings --")
    print(f"  all moves n={len(all_moves):,}: p50 {numpy.quantile(all_moves, .5):.5f}  p95 "
          f"{numpy.quantile(all_moves, .95):.5f}  p99 {numpy.quantile(all_moves, .99):.5f}")
    print(f"  per-symbol p99 over {len(per_symbol_p99):,} symbols with 20+ moves: p50 "
          f"{numpy.quantile(per_symbol_p99, .5):.5f}  p80 {numpy.quantile(per_symbol_p99, .8):.5f}  "
          f"p95 {numpy.quantile(per_symbol_p99, .95):.5f}  max {max(per_symbol_p99):.5f}")
    print(f"  first {MOVES_NEEDED} moves of each symbol-day (judged against the floor alone), n={len(warmup_moves):,}:")
    for floor in FLOOR_CANDIDATES:
        print(f"    floor {floor:<5}  called a jump: {sum(m > floor for m in warmup_moves) / len(warmup_moves):.2%}")

    # ---- 2 and 3: distinct trades ---------------------------------------------------
    drifts = {age: [] for age in AGES_SECONDS}
    realised, implied_by_underlying_day = {}, collections.defaultdict(list)
    for day in DAYS:
        for key in underlying_keys:
            trades = cadence.distinct_trades(cadence.TAPE / key, day)
            if len(trades) >= 2:
                vol = realised_vol_annualised([t for t, _, _ in trades], [p for _, p, _ in trades])
                if vol:
                    realised[(key, day)] = vol
        for directory in sorted(cadence.TAPE.glob("NSE_FO*")):
            trades = cadence.distinct_trades(directory, day)
            if len(trades) >= 2:
                for age, values in drifts_at_ages([t for t, _, _ in trades], [p for _, p, _ in trades]).items():
                    drifts[age].extend(values)
            row = options.get(directory.name)
            greeks = directory / f"{day}.option_greeks.index"
            if row is None or (row["underlying_key"], day) not in realised or not greeks.exists():
                continue
            blob = directory / f"{day}.option_greeks.blob"
            for position, record in enumerate(read_tape_index(greeks)):
                if position % 10 or not cadence.is_in_session(int(record[0])):
                    continue
                try:
                    payload = json.loads(read_payload(blob, record))
                except Exception:
                    continue
                delta, iv = payload.get("delta"), payload.get("implied_volatility")
                if delta is not None and iv and 0.4 <= abs(float(delta)) <= 0.6:
                    implied_by_underlying_day[(row["underlying_key"], day)].append(float(iv))

    print("\n-- 2. p95 |price change| over an age, option contract distinct trades --")
    for age in AGES_SECONDS:
        values = drifts[age]
        p95 = numpy.quantile(values, .95)
        print(f"  {age:>4}s  n={len(values):>9,}  p50 {numpy.quantile(values, .5):.5f}  p95 {p95:.5f}  "
              f"{'>=' if p95 >= OPTION_STOP_FRACTION else '< '} option stop {OPTION_STOP_FRACTION}")

    print("\n-- 3. implied (near-the-money median) against same-day realised volatility --")
    gaps = []
    for key, vol in sorted(realised.items()):
        ivs = implied_by_underlying_day.get(key)
        if not ivs:
            continue
        implied = float(numpy.median(ivs))
        gap = (implied - vol) / vol
        gaps.append(gap)
        print(f"  {key[0]:32} {key[1]}  realised {vol:.4f}  implied {implied:.4f}  gap {gap:+.3f}  ({len(ivs)} greeks)")
    if gaps:
        absolute = [abs(g) for g in gaps]
        print(f"  {len(gaps)} underlying-days: |gap| p20 {numpy.quantile(absolute, .2):.3f}  p50 "
              f"{numpy.quantile(absolute, .5):.3f}  p80 {numpy.quantile(absolute, .8):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
