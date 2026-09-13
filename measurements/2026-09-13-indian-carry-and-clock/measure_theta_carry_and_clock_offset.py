"""What a held NSE option pays to be held, and how far this box's clock sits from Upstox's.

docs/settings-fitted-to-crypto-the-guard-cannot-see.md, "Crypto mechanisms". Two of its
settings need a number from this market before the mechanism they gate can be judged:

1. **Carry.** bear_maximum_carry_fraction_of_horizon and
   bear_invalidation_carry_fraction_of_expected_move bound *funding*. A bought option
   pays no funding; it pays theta. Upstox's option-greeks stream states theta in premium
   points per day, so carry per day as a fraction of premium is |theta| / premium, the
   premium being the contract's last distinct trade at or before the greeks update.
   Reported per day, and over the horizons the entry detectors hand the bear bot
   (mean_reversion_horizon and momentum_burst_horizon 300s, volatility_gap_horizon and
   zero_to_hero_horizon_seconds 3600s), on calendar time -- a stated assumption: theta
   is quoted per calendar day and how it accrues inside a session is not measured here.

2. **Clock offset.** clock-skew-monitor's offset is this machine's receive time minus
   the broker's stamp on a market-data update. The tape index keeps both for every
   record (received_at_ns, broker_time_ns), so the distribution the monitor would see is
   on disk. It includes network transit and Upstox's own stamping delay, which is what
   the monitor sees too.

Days 2026-09-07 and -08, in session only.

Run:  .venv/bin/python measurements/2026-09-13-indian-carry-and-clock/measure_theta_carry_and_clock_offset.py
"""

from __future__ import annotations

import bisect
import gzip
import json
import pathlib
import sys

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_payload, read_tape_index

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
DAYS = ("2026-09-07", "2026-09-08")
SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60
SESSION_CLOSE_UTC_SECONDS = 10 * 3600
HORIZONS_SECONDS = (300.0, 3600.0)
SECONDS_PER_DAY = 86400.0
QUANTILES = (0.05, 0.20, 0.50, 0.80, 0.95, 0.99)
GREEKS_SAMPLE_EVERY = 5
OFFSET_SAMPLE_EVERY = 50


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def describe(label, values, scale=1.0, units=""):
    if not values:
        print(f"  {label:48} none")
        return
    quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q) * scale:.4g}" for q in QUANTILES)
    print(f"  {label:48} n={len(values):>8,}  {quantiles} {units}")


def weighted_median(values, weights):
    order = numpy.argsort(values)
    values, weights = numpy.asarray(values)[order], numpy.asarray(weights, dtype=float)[order]
    return float(values[numpy.searchsorted(numpy.cumsum(weights) / weights.sum(), 0.5)])


def clock_offsets(sample_contracts: int = 300) -> int:
    """Every in-session record of a fixed random sample of contracts, per half hour.

    The first version took every 50th record of every contract, which over-weights
    whatever burst of records a backlog produced; pooled over both days it read p50
    0.65s, where 2026-09-08's whole population reads 0.019s (2026-09-07's reads 1.16s,
    in half hours this system was behind). Half-hour medians separate a clock (every
    half hour offset alike) from this system falling behind (a few half hours far off,
    many records sharing one old broker stamp).
    """
    import collections
    import random

    random.seed(1)
    print("-- clock offset: received_at_ns - broker_time_ns, every in-session record --")
    for day in DAYS:
        directories = [p for p in TAPE.glob("NSE_FO*") if (p / f"{day}.index").exists()]
        offsets, by_half_hour, stamp_uses = [], collections.defaultdict(list), collections.Counter()
        for directory in random.sample(directories, min(sample_contracts, len(directories))):
            for record in read_tape_index(directory / f"{day}.index"):
                received_ns, broker_ns = int(record[0]), int(record[1])
                if not is_in_session(received_ns) or broker_ns <= 0:
                    continue
                offset = (received_ns - broker_ns) / 1e9
                offsets.append(offset)
                by_half_hour[(received_ns // 1_000_000_000) % 86400 // 1800].append(offset)
                stamp_uses[broker_ns] += 1
        describe(f"{day}, {sample_contracts} contracts", offsets, units="s")
        print("    half-hour medians (UTC): " + ", ".join(
            f"{slot // 2:02d}:{'30' if slot % 2 else '00'} {numpy.median(v):.3f}s"
            for slot, v in sorted(by_half_hour.items())
        ))
    return 0


def main() -> int:
    if "--clock-only" in sys.argv:
        return clock_offsets()
    expiry_ms = {
        row["instrument_key"]: row.get("expiry")
        for row in json.load(gzip.open(MASTER)) if row.get("segment") == "NSE_FO"
    }
    carry_per_day, carry_expiry_day, per_contract, contract_weights, offsets = [], [], [], [], []
    for day in DAYS:
        for directory in sorted(TAPE.glob("NSE_FO*")):
            trade_index = directory / f"{day}.index"
            greeks_index = directory / f"{day}.option_greeks.index"
            if not trade_index.exists():
                continue
            trade_blob = directory / f"{day}.blob"
            times, prices, last = [], [], None
            for position, record in enumerate(read_tape_index(trade_index)):
                received_ns, broker_ns = int(record[0]), int(record[1])
                if position % OFFSET_SAMPLE_EVERY == 0 and is_in_session(received_ns) and broker_ns > 0:
                    offsets.append((received_ns - broker_ns) / 1e9)
                if not greeks_index.exists():
                    continue
                try:
                    payload = json.loads(read_payload(trade_blob, record))
                except Exception:
                    continue
                price, traded_ms = payload.get("last_traded_price"), payload.get("last_traded_time_ms")
                if not price or not traded_ms or (traded_ms, price) == last:
                    continue
                last = (traded_ms, price)
                times.append(int(traded_ms) * 1_000_000)
                prices.append(float(price))
            if not greeks_index.exists() or not times:
                continue
            expiry = expiry_ms.get(directory.name)
            greeks_blob = directory / f"{day}.option_greeks.blob"
            mine = []
            for position, record in enumerate(read_tape_index(greeks_index)):
                if position % GREEKS_SAMPLE_EVERY or not is_in_session(int(record[0])):
                    continue
                try:
                    theta = json.loads(read_payload(greeks_blob, record)).get("theta")
                except Exception:
                    continue
                at = bisect.bisect_right(times, int(record[0])) - 1
                if theta is None or at < 0 or prices[at] <= 0:
                    continue
                fraction = abs(float(theta)) / prices[at]
                is_expiry_day = expiry is not None and 0 <= (expiry - int(record[0]) / 1e6) < SECONDS_PER_DAY * 1000
                (carry_expiry_day if is_expiry_day else carry_per_day).append(fraction)
                mine.append(fraction)
            if mine:
                per_contract.append(float(numpy.median(mine)))
                contract_weights.append(len(times))

    print(f"days {', '.join(DAYS)}, in session\n")
    print("-- theta carry: |theta| / premium, per calendar day --")
    describe("contracts not expiring within a day", carry_per_day)
    describe("contracts expiring within a day", carry_expiry_day)
    describe("per contract-day median", per_contract)
    print(f"  trade-weighted median per contract-day: {weighted_median(per_contract, contract_weights):.4%}")
    for horizon in HORIZONS_SECONDS:
        describe(f"  over {horizon:.0f}s, not expiring within a day", carry_per_day, horizon / SECONDS_PER_DAY)
        describe(f"  over {horizon:.0f}s, expiring within a day", carry_expiry_day, horizon / SECONDS_PER_DAY)
    print()
    return clock_offsets()


if __name__ == "__main__":
    raise SystemExit(main())
