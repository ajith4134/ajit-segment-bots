"""What one observation is, how often it arrives, and on which instruments -- for
every setting counted in prints or sized to a print rate.

Two facts found 2026-09-13 decide how everything here is measured, and both were
missed by the move-size measurement made earlier the same day
(measurements/2026-09-13-indian-option-move-sizes/):

1. **The price windows see underlyings, not contracts.** `symbol-price-frame`, which
   bear-entry-timer, tail-mover-qualifier, tail-move-remaining-estimator,
   tail-trailing-exit-planner, bull/bear-feature-builder, regime-classifier and
   venue-outage-rider build their windows from, has one producer on this feed:
   broker-underlying-price-frame-bridge, which carries only the underlyings the
   built segments trade. Contract prices travel on `market-data`
   (broker-market-data-bridge), read by liquidity-grader, limit-price-walker,
   paper-fill-simulator and the order-flow chain.

2. **An observation is a distinct trade, not a tape record.** RollingWindow.observe
   skips a value re-delivered with the same time and price, and the time is
   Upstox's last_traded_time_ms. On 60 sampled option contracts on 2026-09-08 only
   17.8% of tape records carried a new last_traded_time_ms; the rest restate the
   last trade alongside a depth or OI update.

So here: underlyings of the F&O list (the master's underlying_key of every NSE_FO
option) for the frame readers, NSE_FO contracts for the market-data readers, and a
record counts only when (last_traded_time_ms, price) differs from the one before.
In session only (09:15-15:30 IST, by the trade's own time).

Days: 2026-09-04, -07 and -08 are the trading days whose tape holds F&O underlyings
(37, 16 and 16 of 216 -- the 16 are NIFTY, BANKNIFTY and the fourteen largest shares,
the names these segments trade; full subscription began 2026-09-12, a Saturday).
Book snapshots exist for -07 and -08 only.

Run:  .venv/bin/python measurements/2026-09-13-indian-observation-cadence/measure_indian_observation_cadence.py
"""

from __future__ import annotations

import bisect
import collections
import gzip
import json
import pathlib
import sys
import tomllib

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_payload, read_tape_index

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"
UNDERLYING_DAYS = ("2026-09-04", "2026-09-07", "2026-09-08")
CONTRACT_DAYS = ("2026-09-07", "2026-09-08")
SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60
SESSION_CLOSE_UTC_SECONDS = 10 * 3600
QUANTILES = (0.05, 0.20, 0.50, 0.80, 0.95)
SILENCE_CANDIDATES = (60, 150, 300, 600, 900, 1800)
SILENCE_SAMPLE_SECONDS = 30
SLICE_INTERVAL_SECONDS = 10.0          # order_slice_interval
WALK_PRINTS = 3                         # limit_walk_cadence: "a few prints"


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def distinct_trades(directory: pathlib.Path, day: str):
    """(time ns, price, quantity or None) for each distinct in-session trade."""
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return []
    blob = directory / f"{day}.blob"
    trades, last = [], None
    for record in read_tape_index(index_path):
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        price, traded_ms = payload.get("last_traded_price"), payload.get("last_traded_time_ms")
        if not price or price <= 0 or not traded_ms:
            continue
        at_ns = int(traded_ms) * 1_000_000
        if not is_in_session(at_ns) or (at_ns, price) == last:
            continue
        last = (at_ns, price)
        trades.append((at_ns, float(price), payload.get("last_traded_quantity")))
    return trades


RECORDING_HOLE_SECONDS = 60


def recording_pieces(series_by_key) -> list[tuple[int, int]]:
    """Stretches the tape was recording, from the union of every underlying's trades.

    NIFTY and BANKNIFTY change price about every second in session, so a minute in
    which no underlying traded at all is the recorder down (a spine restart), not a
    quiet market -- and a silence measured across it would be measuring the outage.
    """
    union = sorted(t for trades in series_by_key.values() for t, _, _ in trades)
    pieces, start = [], union[0]
    for earlier, later in zip(union, union[1:]):
        if (later - earlier) / 1e9 > RECORDING_HOLE_SECONDS:
            pieces.append((start, earlier))
            start = later
    pieces.append((start, union[-1]))
    return [(a, b) for a, b in pieces if b > a]


def span_seconds_of(times: list[int], count: int) -> float | None:
    """Median seconds spanned by `count` consecutive trades."""
    if len(times) < count:
        return None
    array = numpy.asarray(times, dtype="int64")
    return float(numpy.median(array[count - 1:] - array[: len(array) - count + 1]) / 1e9)


def bounce_peaks(prices, window_length):
    peaks, peak = [], None
    series = numpy.asarray(prices)
    if len(series) <= window_length:
        return peaks
    cumulative = numpy.concatenate(([0.0], numpy.cumsum(series)))
    for position in range(window_length, len(series)):
        mean = (cumulative[position + 1] - cumulative[position + 1 - window_length]) / window_length
        extension = (series[position] - mean) / mean
        if extension > 0:
            peak = extension if peak is None else max(peak, extension)
        elif peak is not None:
            peaks.append(peak)
            peak = None
    return peaks


def sustained_move(series, sign, minimum_progressing):
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
    if progressing < minimum_progressing or series[start_index] <= 0:
        return None, None
    return sign * (latest - series[start_index]) / series[start_index], start_index_marker(series, start_index)


def start_index_marker(series, start_index):
    # A run is identified by the price it started from and how far back it began;
    # the price alone would merge two runs that happened to start at one level.
    return (series[start_index], len(series) - start_index)


def completed_moves(prices, window_length, minimum_progressing):
    moves, window = [], collections.deque(maxlen=window_length)
    running = {1.0: None, -1.0: None}
    for price in prices:
        window.append(price)
        series = list(window)
        for sign in (1.0, -1.0):
            move, marker = sustained_move(series, sign, minimum_progressing)
            current = running[sign]
            if move is not None and move > 0:
                # The run continues while its start price is unchanged; its length
                # grows by one per trade, so compare the start price only.
                if current is not None and current[0] == marker[0]:
                    running[sign] = (marker[0], max(current[1], move))
                    continue
                if current is not None:
                    moves.append(current[1])
                running[sign] = (marker[0], move)
            elif current is not None:
                moves.append(current[1])
                running[sign] = None
    moves.extend(current[1] for current in running.values() if current is not None)
    return moves


def continuation_pullbacks(prices):
    """measurements/2026-09-13-indian-option-retracements' definition, on this series."""
    out, high, deepest = [], prices[0], 0.0
    for price in prices[1:]:
        if price > high:
            if deepest > 0:
                out.append(deepest)
            high, deepest = price, 0.0
        else:
            deepest = max(deepest, (high - price) / high)
    return out


def describe(label, values, units=""):
    if not values:
        print(f"  {label:44} none")
        return
    quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q):.6g}" for q in QUANTILES)
    print(f"  {label:44} n={len(values):>8,}  {quantiles}  {units}")


def weighted_median(values, weights):
    order = numpy.argsort(values)
    values, weights = numpy.asarray(values)[order], numpy.asarray(weights, dtype=float)[order]
    return float(values[numpy.searchsorted(numpy.cumsum(weights) / weights.sum(), 0.5)])


def main() -> int:
    settings = tomllib.loads(SETTINGS.read_text())
    number = lambda name: float(settings[name]["value"])
    windows = {
        "bull/bear_feature_short_window": int(number("bull_feature_short_window")),
        "bull_feature_long_window": int(number("bull_feature_long_window")),
        "bear_entry_window_length": int(number("bear_entry_window_length")),
        "regime_window_length": int(number("regime_window_length")),
        "tail_window_length": int(number("tail_window_length")),
    }
    master = json.load(gzip.open(MASTER))
    underlying_keys = {
        row["underlying_key"] for row in master
        if row.get("segment") == "NSE_FO" and row.get("instrument_type") in ("CE", "PE")
    }

    print("==== UNDERLYINGS (symbol-price-frame) ====")
    per_day_rate, rate_weights, spans = [], [], collections.defaultdict(list)
    bounces, moves, pullbacks = [], [], []
    silent_fractions = collections.defaultdict(list)
    for day in UNDERLYING_DAYS:
        series_by_key = {}
        for key in sorted(underlying_keys):
            trades = distinct_trades(TAPE / key, day)
            if len(trades) >= 2:
                series_by_key[key] = trades
        if not series_by_key:
            continue
        pieces = recording_pieces(series_by_key)
        print(f"  {day}: {len(series_by_key)} underlyings with trades; tape recording in "
              f"{len(pieces)} piece(s) totalling {sum(b - a for a, b in pieces) / 60e9:.0f} min "
              f"(holes over {RECORDING_HOLE_SECONDS}s with no underlying trading cut out)")
        for key, trades in series_by_key.items():
            recorded_minutes, count = 0.0, 0
            for start, end in pieces:
                piece = [trade for trade in trades if start <= trade[0] <= end]
                if not piece:
                    continue
                recorded_minutes += (end - start) / 60e9
                count += len(piece)
                times = [t for t, _, _ in piece]
                prices = [p for _, p, _ in piece]
                for label, window_count in windows.items():
                    span = span_seconds_of(times, window_count)
                    if span is not None:
                        spans[label].append(span)
                bounces.extend(bounce_peaks(prices, windows["bear_entry_window_length"]))
                moves.extend(completed_moves(prices, windows["tail_window_length"],
                                             int(number("tail_minimum_observations_in_move"))))
                if len(prices) >= 2:
                    pullbacks.extend(continuation_pullbacks(prices))
            if recorded_minutes > 0:
                per_day_rate.append(count / recorded_minutes)
                rate_weights.append(count)
        # Silence as venue-outage-rider sees it: over symbols that have traded in this
        # recording piece, the share whose last trade is at least S old. The first
        # fifteen minutes of each piece are skipped, as a restarted rider would be.
        times_by_key = {key: [t for t, _, _ in trades] for key, trades in series_by_key.items()}
        for start, end in pieces:
            at = start + 15 * 60 * 1_000_000_000
            while at <= end:
                ages = []
                for times in times_by_key.values():
                    position = bisect.bisect_right(times, at) - 1
                    if position >= 0 and times[position] >= start:
                        ages.append((at - times[position]) / 1e9)
                for threshold in SILENCE_CANDIDATES:
                    if ages:
                        silent_fractions[threshold].append(sum(a >= threshold for a in ages) / len(ages))
                at += SILENCE_SAMPLE_SECONDS * 1_000_000_000
    describe("distinct trades a minute, per underlying-day", per_day_rate)
    print(f"  trade-weighted median trades a minute: {weighted_median(per_day_rate, rate_weights):.2f}")
    for label, values in spans.items():
        describe(f"seconds spanned by {label} ({windows[label]})", values, "s")
    describe("bounce peak over bear_entry_window_length", bounces, "fraction")
    describe("completed sustained move over tail_window_length", moves, "fraction")
    describe("continuation pullback", pullbacks, "fraction")
    for threshold in SILENCE_CANDIDATES:
        values = silent_fractions[threshold]
        if values:
            print(f"  silent >= {threshold:>5}s: share of underlyings  p50 {numpy.quantile(values, .5):.3f}"
                  f"  p95 {numpy.quantile(values, .95):.3f}  max {max(values):.3f}  over {len(values)} samples")

    if "--underlyings-only" in sys.argv:
        return 0
    print("\n==== CONTRACTS (market-data) ====")
    rates, rates_with_size, weights, walk_seconds, walk_weights = [], [], [], [], []
    participation = []
    for day in CONTRACT_DAYS:
        for directory in sorted(TAPE.glob("NSE_FO*")):
            trades = distinct_trades(directory, day)
            if len(trades) < 2:
                continue
            times = [t for t, _, _ in trades]
            minutes = (times[-1] - times[0]) / 60e9
            if minutes <= 0:
                continue
            rates.append(len(trades) / minutes)
            rates_with_size.append(sum(1 for _, _, q in trades if q) / minutes)
            weights.append(len(trades))
            span = span_seconds_of(times, WALK_PRINTS)
            if span is not None:
                walk_seconds.append(span)
                walk_weights.append(len(trades))
            # Traded quantity per slice interval against the size resting at the touch.
            book_index = directory / f"{day}.book.index"
            if not book_index.exists():
                continue
            volume_by_slot = collections.Counter()
            for at_ns, _, quantity in trades:
                if quantity:
                    volume_by_slot[int(at_ns // (SLICE_INTERVAL_SECONDS * 1e9))] += quantity
            blob = directory / f"{day}.book.blob"
            for position, record in enumerate(read_tape_index(book_index)):
                if position % 10 or not is_in_session(int(record[0])):
                    continue
                try:
                    levels = json.loads(read_payload(blob, record)).get("levels") or []
                except Exception:
                    continue
                if not levels or not levels[0].get("ask_quantity"):
                    continue
                slot = int(int(record[0]) // (SLICE_INTERVAL_SECONDS * 1e9))
                traded = volume_by_slot.get(slot - 1, 0)
                if traded > 0:
                    participation.append(levels[0]["ask_quantity"] / traded)
    describe("distinct trades a minute, per contract-day", rates)
    describe("... of those stating a quantity", rates_with_size)
    print(f"  trade-weighted median trades a minute: {weighted_median(rates, weights):.3f}; "
          f"with a quantity: {weighted_median(rates_with_size, weights):.3f}")
    describe(f"seconds spanned by {WALK_PRINTS} trades", walk_seconds, "s")
    print(f"  trade-weighted median seconds for {WALK_PRINTS} trades: "
          f"{weighted_median(walk_seconds, walk_weights):.3f}")
    describe("touch ask quantity / quantity traded in the prior 10s", participation, "ratio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
