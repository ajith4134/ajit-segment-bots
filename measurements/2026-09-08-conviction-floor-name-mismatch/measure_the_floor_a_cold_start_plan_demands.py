"""What conviction floor a cold-start plan actually demands, from this project's own tape.

Re-derives, per real instrument, the number `bull-opinion-composer` judged every
conviction against on 2026-09-08 -- when it stood down 100% of its opinions with
`last_floor: 1.0`, a floor no probability can cross.

The arithmetic is the running system's own (runtime/edge_arithmetic.py):

    stop_fraction = live range over the claimed horizon x cold-start multiple
    round_trip_cost_in_risk_units = 2 x per-side cost / stop_fraction
    floor = (1 + cost) / (1 + reward_to_risk)     with the margin at 0

`reward_to_risk` is structurally constant at 1.8 in the cold start, because
`_cold_start_targets` places every target at a multiple of the stop, so the stop
fraction cancels out of the weighted reward: 1x0.4 + 2x0.4 + 3x0.2.

Run it against a captured day to see which symbols the floor is uncrossable for
and which it is not:

    .venv/bin/python measurements/2026-09-08-conviction-floor-name-mismatch/\
measure_the_floor_a_cold_start_plan_demands.py 2026-09-08
"""

from __future__ import annotations

import json
import pathlib
import random
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import StreamKind, read_tape_index, tape_paths_for  # noqa: E402

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
VENUE = "upstox"

# Every one of these is the deployed setting's own value, named here so a reader
# can see which number the verdict below moves with.
PER_SIDE_TRADING_COST_FRACTION = 0.004266   # per_side_trading_cost_fraction
COLD_START_STOP_RANGE_MULTIPLE = 1.5        # bull_cold_start_stop_range_multiple
COLD_START_MINIMUM_PRINTS = 20              # bull_cold_start_minimum_prints
MARGIN_OVER_BREAK_EVEN = 0.0                # bull_conviction_margin_over_break_even
# 1x0.4 + 2x0.4 + 3x0.2, from bull_cold_start_reward_multiples against
# bull_exit_target_fractions. Constant by construction, not a chosen number.
COLD_START_REWARD_TO_RISK = 1.8
# What the bull conviction model actually reports: challenger.positives /
# challenger.observations was 48,202 / 97,420 on the live spine, 2026-09-08.
CONVICTION_THE_MODEL_ACTUALLY_REPORTS = 0.50

DETECTOR_HORIZONS_SECONDS = (60.0, 300.0, 3600.0)


def prints_for(symbol: str, day: str) -> list[tuple[int, float]]:
    """Every trade print the feed recorded for one symbol on one day."""
    index_path, blob_path = tape_paths_for(TAPE_ROOT, VENUE, symbol, day, StreamKind.TRADE)
    if not index_path.exists():
        return []
    records = read_tape_index(index_path)
    if len(records) == 0:
        return []
    prints = []
    with open(blob_path, "rb") as handle:
        for record in records:
            handle.seek(int(record["blob_offset"]))
            try:
                row = json.loads(handle.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = row.get("last_traded_price")
            if price:
                prints.append((int(row.get("broker_time_ns") or record["venue_time_ns"]), float(price)))
    prints.sort()
    return prints


def range_fraction_over(prints, seconds: float) -> float | None:
    """Exactly `BullExitPlanProposer.live_range_fraction`, ending at the last print."""
    if not prints:
        return None
    ends_at = prints[-1][0]
    inside = [price for at_ns, price in prints if at_ns >= ends_at - int(seconds * 1e9)]
    if len(inside) < COLD_START_MINIMUM_PRINTS:
        return None
    highest, lowest, last = max(inside), min(inside), inside[-1]
    if last <= 0 or highest <= lowest:
        return None
    return (highest - lowest) / last


def floor_for_stop(stop_fraction: float) -> float:
    """The conviction floor `ConvictionFloor.for_plan` computes for this stop."""
    cost = 2.0 * PER_SIDE_TRADING_COST_FRACTION / stop_fraction
    break_even = (1.0 + cost) / (1.0 + COLD_START_REWARD_TO_RISK)
    return min(1.0, break_even + MARGIN_OVER_BREAK_EVEN)


def report(symbols, day: str, label: str) -> None:
    for horizon in DETECTOR_HORIZONS_SECONDS:
        ranges, floors = [], []
        for symbol in symbols:
            fraction = range_fraction_over(prints_for(symbol, day), horizon)
            if fraction is None:
                continue
            ranges.append(fraction)
            floors.append(floor_for_stop(fraction * COLD_START_STOP_RANGE_MULTIPLE))
        if not floors:
            print(f"  {label:22s} {horizon:6.0f}s   no symbol reached {COLD_START_MINIMUM_PRINTS} prints")
            continue
        pinned = sum(1 for floor in floors if floor >= 1.0)
        crossable = sum(1 for floor in floors if floor <= CONVICTION_THE_MODEL_ACTUALLY_REPORTS)
        print(
            f"  {label:22s} {horizon:6.0f}s  n={len(floors):4d}  "
            f"range p50 {statistics.median(ranges):7.2%}  floor p50 {statistics.median(floors):6.1%}  "
            f"uncrossable {pinned:3d}  crossable at p={CONVICTION_THE_MODEL_ACTUALLY_REPORTS:.2f} {crossable:3d}"
        )


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-09-08"
    sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    directories = [path.name for path in (TAPE_ROOT / VENUE).iterdir() if path.is_dir()]
    options = [name for name in directories if name.startswith("NSE_FO|")]
    equities = [name for name in directories if not name.startswith("NSE_")]
    print(f"{day}: {len(options)} option contracts and {len(equities)} equities on the tape\n")

    random.seed(11)
    report(random.sample(options, min(sample_size, len(options))), day, "option contracts")
    report(random.sample(equities, min(sample_size, len(equities))), day, "equities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
