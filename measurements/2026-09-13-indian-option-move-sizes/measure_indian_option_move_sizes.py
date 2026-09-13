"""The move sizes three priors still take from crypto's intraday range, measured on
NSE option prints with each part's own definition.

docs/settings-fitted-to-crypto-the-guard-cannot-see.md, "Move sizes fitted to
crypto's intraday range". Every note there rests on "the 1.5% largest intraday
move measured on the captured thirty" or "the median five-minute move on the
liquid captured symbols" -- BTCUSDT-class perpetuals on 2026-08-23.

Three quantities, each computed the way the reading part computes it, so the
figure is the one the part would have learned:

    bounce above the mean   bear-entry-timer: extension = (price - mean) / mean,
                            mean over the last bear_entry_window_length prints.
                            One bounce is a run of prints above the mean; its
                            size is the run's peak extension.
    completed move          tail-mover-qualifier._sustained_move over the last
                            tail_window_length prints, a run needing
                            tail_minimum_observations_in_move progressing steps.
                            A move completes when its run ends; its size is the
                            largest the run reached.
    five-minute move        entropy-magnitude-forecaster: |return| over
                            forecast_horizon, from a print to the first print at
                            least that much later, non-overlapping.

Why contracts: of 116,179 entry candidates in the lifecycle journal's last
150 MB, 111,265 (95.8%) name an option contract, and every position is one.
Underlyings (NSE_EQ / NSE_INDEX) are reported beside them because volatility-gap
detector's candidates are on the underlying.

In-session prints only (09:15-15:30 IST), per session, never across the night.

Run:  .venv/bin/python measurements/2026-09-13-indian-option-move-sizes/measure_indian_option_move_sizes.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
import tomllib

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_payload, read_tape_index

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"
DAYS = ("2026-09-07", "2026-09-08")
SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60
SESSION_CLOSE_UTC_SECONDS = 10 * 3600
QUANTILES = (0.20, 0.50, 0.80, 0.95)


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def session_prints(directory: pathlib.Path, day: str):
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return None
    index = read_tape_index(index_path)
    blob = directory / f"{day}.blob"
    times, prices = [], []
    for record in index:
        if not is_in_session(int(record[0])):
            continue
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        price = payload.get("last_traded_price")
        if price and price > 0:
            times.append(int(record[0]))
            prices.append(float(price))
    return times, prices


def bounce_peaks(prices: list[float], window_length: int) -> list[float]:
    """Peak extension above the rolling mean, one per run of prints above it."""
    peaks = []
    series = numpy.asarray(prices)
    if len(series) <= window_length:
        return peaks
    cumulative = numpy.concatenate(([0.0], numpy.cumsum(series)))
    peak = None
    for position in range(window_length, len(series)):
        # The part's window holds the latest `window_length` prints, this one included.
        mean = (cumulative[position + 1] - cumulative[position + 1 - window_length]) / window_length
        extension = (series[position] - mean) / mean
        if extension > 0:
            peak = extension if peak is None else max(peak, extension)
        elif peak is not None:
            peaks.append(peak)
            peak = None
    return peaks


def sustained_move(series, sign: float, minimum_progressing: int):
    """tail_mover_qualifier._sustained_move, verbatim in logic. Returns (move, start)."""
    if len(series) < minimum_progressing:
        return None, None
    latest = series[-1]
    start_index = len(series) - 1
    for index in range(len(series) - 2, -1, -1):
        if sign * (latest - series[index]) <= 0:
            break
        start_index = index
    progressing = sum(
        1 for earlier, later in zip(series[start_index:], series[start_index + 1:])
        if sign * (later - earlier) > 0
    )
    if progressing < minimum_progressing:
        return None, None
    start = series[start_index]
    if start <= 0:
        return None, None
    return sign * (latest - start) / start, start


def completed_moves(prices: list[float], window_length: int, minimum_progressing: int) -> list[float]:
    moves = []
    window = collections.deque(maxlen=window_length)
    running = {1.0: None, -1.0: None}     # sign -> (start price, largest move so far)
    for price in prices:
        window.append(price)
        series = list(window)
        for sign in (1.0, -1.0):
            move, start = sustained_move(series, sign, minimum_progressing)
            current = running[sign]
            if move is not None and move > 0:
                if current is not None and current[0] == start:
                    running[sign] = (start, max(current[1], move))
                    continue
                if current is not None:
                    moves.append(current[1])
                running[sign] = (start, move)
            elif current is not None:
                moves.append(current[1])
                running[sign] = None
    moves.extend(current[1] for current in running.values() if current is not None)
    return moves


def horizon_moves(times: list[int], prices: list[float], horizon_seconds: float) -> list[float]:
    moves = []
    anchor_time, anchor_price = times[0], prices[0]
    horizon_ns = horizon_seconds * 1e9
    for at_ns, price in zip(times[1:], prices[1:]):
        if at_ns - anchor_time >= horizon_ns:
            moves.append(abs(price / anchor_price - 1.0))
            anchor_time, anchor_price = at_ns, price
    return moves


def describe(label: str, values: list[float], units: str) -> None:
    if not values:
        print(f"  {label}: none")
        return
    quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q):.6f}" for q in QUANTILES)
    print(f"  {label:34} n={len(values):>9,}  {quantiles}  ({units})")


def main() -> int:
    settings = tomllib.loads(SETTINGS.read_text())
    number = lambda name: float(settings[name]["value"])
    bear_window = int(number("bear_entry_window_length"))
    tail_window = int(number("tail_window_length"))
    tail_progressing = int(number("tail_minimum_observations_in_move"))
    horizon = number("forecast_horizon")
    print(f"days {', '.join(DAYS)}; bear_entry_window_length {bear_window}, "
          f"tail_window_length {tail_window}, tail_minimum_observations_in_move "
          f"{tail_progressing}, forecast_horizon {horizon:.0f}s\n")

    for kind, prefixes in (("option contracts", ("NSE_FO",)), ("underlyings", ("NSE_EQ", "NSE_INDEX"))):
        bounces, moves, five_minute = [], [], []
        contract_days = prints = 0
        for day in DAYS:
            for prefix in prefixes:
                for directory in sorted(TAPE.glob(f"{prefix}*")):
                    series = session_prints(directory, day)
                    if series is None or len(series[0]) < 2:
                        continue
                    times, prices = series
                    contract_days += 1
                    prints += len(prices)
                    bounces.extend(bounce_peaks(prices, bear_window))
                    moves.extend(completed_moves(prices, tail_window, tail_progressing))
                    five_minute.extend(horizon_moves(times, prices, horizon))
        print(f"-- {kind}: {contract_days:,} instrument-days, {prints:,} in-session prints --")
        describe("bounce peak above the mean", bounces, "fraction of price")
        describe("completed sustained move", moves, "fraction of price")
        describe(f"|return| over {horizon:.0f}s", five_minute, "fraction of price")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
