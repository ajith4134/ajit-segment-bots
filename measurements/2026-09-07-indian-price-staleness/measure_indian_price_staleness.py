"""What an Indian option premium actually does in a second, and what a round trip
on it actually costs -- the two numbers `PriceStalenessEstimator` is built from.

Both were carried through the pivot from crypto unchanged:

    reference_price_prior_one_second_move  0.000898  the median 95th-percentile
                                                     one-second move across six
                                                     Binance/Bybit perpetuals
    materiality = 2 x taker_fee_rate       0.0011    twice Bybit's published
                                                     taker rate

Together they give an unmeasured symbol a believable age of (0.0011/0.000898)^2
= 1.5s, and a measured option contract the 1.0s floor. Measured here against the
project's own tape, the median option contract prints every 14.5s, so the bound
refuses the newest price that exists for most of the session.

This script re-derives all three from the tape, using exactly the estimator's own
method (anchor at least `anchor_seconds` old, move scaled to one second by the
square root of the gap, quantile taken over a bounded window) so the number it
produces is the number the running part would learn.

Run:  .venv/bin/python measurements/2026-09-07-indian-price-staleness/measure_indian_price_staleness.py
"""

from __future__ import annotations

import gzip
import json
import math
import os
import pathlib
import random
import statistics
import sys

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.tape import read_payload, read_tape_index
from runtime.trading_types import BUY, SELL

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"

ANCHOR_SECONDS = 1.0          # reference_price_move_anchor_seconds
QUANTILE = 0.95               # reference_price_move_quantile
SAMPLE = 400
OPTION_SEGMENTS = ("NSE_FO", "BSE_FO")


def one_second_moves(times_ns, prices, anchor_seconds: float) -> list[float]:
    """The estimator's own move series for one instrument, in its own units."""
    moves: list[float] = []
    anchor_time, anchor_price = None, None
    for at_ns, price in zip(times_ns, prices):
        if price is None or price <= 0:
            continue
        if anchor_price is None:
            anchor_time, anchor_price = at_ns, price
            continue
        gap = (at_ns - anchor_time) / 1e9
        if gap < anchor_seconds:
            continue
        moves.append(abs(price / anchor_price - 1.0) / math.sqrt(gap))
        anchor_time, anchor_price = at_ns, price
    return moves


def trade_series(directory: pathlib.Path, day: str):
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return None
    index = read_tape_index(index_path)
    if len(index) < 20:
        return None
    blob = directory / f"{day}.blob"
    times, prices = [], []
    for record in index:
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        price = payload.get("last_traded_price")
        if price:
            times.append(int(record["received_at_ns"]))
            prices.append(float(price))
    return (times, prices) if len(times) >= 20 else None


def spread_fractions(directory: pathlib.Path, day: str, sample_every: int = 25) -> list[float]:
    """Half the touch spread as a fraction of the mid, from the book tape."""
    index_path = directory / f"{day}.book.index"
    if not index_path.exists():
        return []
    index = read_tape_index(index_path)
    blob = directory / f"{day}.book.blob"
    out = []
    for position in range(0, len(index), sample_every):
        try:
            payload = json.loads(read_payload(blob, index[position]))
        except Exception:
            continue
        levels = payload.get("levels") or []
        if not levels:
            continue
        bid, ask = levels[0].get("bid_price"), levels[0].get("ask_price")
        if not bid or not ask or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        out.append((ask - bid) / 2.0 / mid)
    return out


def charge_round_trip_fraction(premium_value: float) -> float:
    """Upstox's real options charge stack for one round trip, as a fraction of premium."""
    rates = dict(
        flat_brokerage=20.0, stt_sell_rate=0.001,
        exchange_transaction_charge_rate=0.0003503, ipft_charge_rate=0.000005,
        stamp_duty_buy_rate=0.00003, gst_rate=0.18,
    )
    buy = upstox_options_order_cost(premium_value, BUY, **rates).total
    sell = upstox_options_order_cost(premium_value, SELL, **rates).total
    return (buy + sell) / premium_value


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-09-07"
    master = {row["instrument_key"]: row for row in json.load(gzip.open(MASTER))}

    directories = [
        entry for entry in os.scandir(TAPE)
        if entry.is_dir() and entry.name.startswith(OPTION_SEGMENTS)
        and os.path.exists(os.path.join(entry.path, f"{day}.index"))
    ]
    random.seed(7)
    sampled = random.sample(directories, min(SAMPLE, len(directories)))

    per_contract_quantile, gaps_all, spreads_all, print_counts = [], [], [], []
    for entry in sampled:
        directory = pathlib.Path(entry.path)
        series = trade_series(directory, day)
        if series is None:
            continue
        times, prices = series
        print_counts.append(len(times))
        moves = one_second_moves(times, prices, ANCHOR_SECONDS)
        if len(moves) >= 20:
            per_contract_quantile.append(float(numpy.quantile(moves, QUANTILE)))
        gaps = numpy.diff(numpy.asarray(times, dtype="int64")) / 1e9
        gaps_all.append(float(numpy.median(gaps)))
        spreads_all.extend(spread_fractions(directory, day))

    print(f"day {day}: {len(sampled)} option contracts sampled, "
          f"{len(per_contract_quantile)} with enough moves to fit\n")

    print("-- one-second move, the estimator's own method --")
    for label, value in (
        ("p50 across contracts", numpy.quantile(per_contract_quantile, 0.50)),
        ("p90 across contracts", numpy.quantile(per_contract_quantile, 0.90)),
        ("mean across contracts", statistics.fmean(per_contract_quantile)),
    ):
        print(f"  {label:24s} {value:.6f}  ({value:.4%})")
    prior = float(numpy.quantile(per_contract_quantile, 0.50))
    print(f"  crypto value in use      0.000898  (0.0898%)\n")

    print("-- how often a contract prints --")
    print(f"  median inter-print gap   p50 {numpy.quantile(gaps_all, 0.50):.2f}s"
          f"  p90 {numpy.quantile(gaps_all, 0.90):.2f}s"
          f"  p95 {numpy.quantile(gaps_all, 0.95):.2f}s")
    print(f"  prints per contract      p50 {numpy.quantile(print_counts, 0.50):.0f}\n")

    print("-- what a round trip costs --")
    half_spread = float(numpy.quantile(spreads_all, 0.50)) if spreads_all else float("nan")
    print(f"  half touch spread        p50 {half_spread:.6f}  ({half_spread:.4%})"
          f"   from {len(spreads_all):,} book snapshots")
    for premium_value in (100_000.0, 200_000.0):
        charges = charge_round_trip_fraction(premium_value)
        print(f"  charges round trip       {charges:.6f}  ({charges:.4%})"
              f"   on a Rs{premium_value:,.0f} premium")
    charges = charge_round_trip_fraction(150_000.0)
    materiality = charges + 2 * half_spread
    print(f"  round trip, all in       {materiality:.6f}  ({materiality:.4%})"
          f"   charges at Rs150,000 + two half spreads")
    print(f"  crypto value in use      0.001100  (0.1100%)\n")

    print("-- believable age this implies, (materiality / move)^2 --")
    for label, m, move in (
        ("crypto materiality, crypto prior", 0.0011, 0.000898),
        ("crypto materiality, measured move", 0.0011, prior),
        ("measured materiality, measured move", materiality, prior),
    ):
        print(f"  {label:36s} {(m / move) ** 2:9.3f}s")
    print()
    print(f"proposed reference_price_prior_one_second_move = {prior:.6f}")
    print(f"proposed materiality (options round trip)      = {materiality:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
