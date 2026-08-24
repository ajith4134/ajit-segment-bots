"""How wrong a price is, as a function of how old it is, on this system's own tape.

RL-061: the bound on how old a reference price may be is a number that decides
whether a trade is taken, so it is estimated from data rather than chosen. The
question this answers is not "how often does a symbol print" -- that is an arrival
rate, and a symbol can print steadily while the price runs away -- but "if I size a
position against a price of age A, how far from the market am I likely to be".

The threshold that makes an answer out of the distribution is the trade's own cost.
A round trip pays the taker rate twice, and the sizer already accounts for it. A
price stale enough to be wrong by more than that is a price that changes the
answer rather than merely delaying it; a price wrong by less is inside the noise
the trade was always going to pay.

Run:
    .venv/bin/python measurements/2026-08-24-reference-price-staleness/measure_price_drift_by_age.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_payload, read_tape_index, tape_paths_for  # noqa: E402
from runtime.venues.adapter_registry import load_venue_adapter  # noqa: E402

SECOND_NS = 1_000_000_000
CANDIDATE_AGE_SECONDS = (1, 2, 5, 10, 15, 30, 45, 60, 120, 300, 600, 1_800, 3_360)


def read_price_series(tape_root: pathlib.Path, venue_id: str, symbol: str, day: str, stride: int):
    """(venue_time_ns, price) for every stride-th trade the tape holds for a symbol."""
    index_path, blob_path = tape_paths_for(tape_root, venue_id, symbol, day)
    if not index_path.exists():
        return []
    adapter = load_venue_adapter(venue_id)
    index = read_tape_index(index_path)
    series = []
    with open(blob_path, "rb") as blob:
        for record in index[::stride]:
            blob.seek(int(record["blob_offset"]))
            for trade in adapter.read_trades(blob.read(int(record["blob_length"]))):
                series.append((int(trade.venue_time_ns), float(trade.price)))
    series.sort()
    return series


def drift_at_age(series, age_seconds: int, anchors: int):
    """|p(t + age)/p(t) - 1| sampled across the day, as fractions of notional.

    The later price is the last trade at or before t + age, which is exactly what a
    part holding a level would have had: not an interpolation, a real print.
    """
    if len(series) < 2:
        return []
    span_ns = series[-1][0] - series[0][0]
    if span_ns <= age_seconds * SECOND_NS:
        return []
    times = [at for at, _ in series]
    step = max(1, len(series) // anchors)
    drifts = []
    import bisect

    for position in range(0, len(series) - 1, step):
        at, price = series[position]
        if price <= 0:
            continue
        target = at + age_seconds * SECOND_NS
        if target > times[-1]:
            break
        later = bisect.bisect_right(times, target) - 1
        if later <= position:
            continue
        drifts.append(abs(series[later][1] / price - 1.0))
    return drifts


def percentile(values, fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tape-root", default=str(
        pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
    ))
    parser.add_argument("--venue", default="binance-usdm")
    parser.add_argument("--day", default="2026-08-23")
    parser.add_argument("--symbols", nargs="+", default=[
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "ENAUSDT", "ADAUSDT", "1000PEPEUSDT",
    ])
    parser.add_argument("--stride", type=int, default=40,
                        help="read every Nth tape record, to keep a day's decode bounded")
    parser.add_argument("--anchors", type=int, default=4_000)
    parser.add_argument("--round-trip-cost-fraction", type=float, default=2 * 0.00055,
                        help="the taker rate twice, from settings taker_fee_rate")
    arguments = parser.parse_args()

    tape_root = pathlib.Path(arguments.tape_root)
    measured = {}
    for symbol in arguments.symbols:
        series = read_price_series(
            tape_root, arguments.venue, symbol, arguments.day, arguments.stride
        )
        if len(series) < 100:
            measured[symbol] = {"trades_read": len(series), "note": "too little tape to measure"}
            continue
        per_age = {}
        for age in CANDIDATE_AGE_SECONDS:
            drifts = drift_at_age(series, age, arguments.anchors)
            if not drifts:
                continue
            per_age[age] = {
                "samples": len(drifts),
                "median": statistics.median(drifts),
                "p95": percentile(drifts, 0.95),
                "p99": percentile(drifts, 0.99),
            }
        measured[symbol] = {
            "trades_read": len(series),
            "seconds_spanned": (series[-1][0] - series[0][0]) / SECOND_NS,
            "drift_by_age_seconds": per_age,
        }
        print(f"{symbol}: {len(series)} sampled trades", flush=True)
        for age, statistic in per_age.items():
            print(
                f"  age {age:>5}s  median {statistic['median']:.6%}  "
                f"p95 {statistic['p95']:.6%}  p99 {statistic['p99']:.6%}"
            )

    cost = arguments.round_trip_cost_fraction
    largest_age_within_cost = {}
    for symbol, result in measured.items():
        per_age = result.get("drift_by_age_seconds", {})
        within = [age for age, statistic in per_age.items() if statistic["p95"] <= cost]
        largest_age_within_cost[symbol] = max(within) if within else None

    binding = [age for age in largest_age_within_cost.values() if age is not None]
    verdict = {
        "round_trip_cost_fraction": cost,
        "largest_age_whose_p95_drift_stays_within_one_round_trip": largest_age_within_cost,
        "binding_across_symbols": min(binding) if binding else None,
    }
    print("\n" + json.dumps(verdict, indent=2))

    output = pathlib.Path(__file__).with_name("price-drift-by-age.json")
    output.write_text(json.dumps(
        {"venue": arguments.venue, "day": arguments.day, "stride": arguments.stride,
         "measured": measured, "verdict": verdict},
        indent=2, default=float,
    ))
    print(f"\nwritten {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
