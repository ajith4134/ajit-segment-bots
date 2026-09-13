"""Two figures the cadence measurement could not give honestly.

1. **What rests at the touch against what the exchange traded.** The first pass
   summed last_traded_quantity off the LTP ticker, which restates only the latest
   trade at each update and drops every trade between two updates -- so traded
   volume was understated by an unknown factor and the ratio overstated by it. The
   one-minute candles on the tape carry the exchange's own bar volume. A bar is
   restated many times while open; its last record is its volume.
   Ratio = ask quantity at the best level / (the previous closed minute's volume /
   6), i.e. against one order_slice_interval (10 s) of traded volume.

2. **The touch spread of the shares the frame readers see.** tail-trailing-exit-
   planner trails an underlying's price, so "a few spreads" is a few of the
   underlying's spreads, not an option's. Indices have no book.

Run:  .venv/bin/python measurements/2026-09-13-indian-observation-cadence/measure_touch_against_exchange_volume.py
"""

from __future__ import annotations

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
SLICE_INTERVAL_SECONDS = 10.0
SAMPLE_EVERY = 10
QUANTILES = (0.05, 0.20, 0.50, 0.80, 0.95)


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def minute_volumes(directory: pathlib.Path, day: str) -> dict[int, float]:
    index_path = directory / f"{day}.candle.index"
    if not index_path.exists():
        return {}
    blob = directory / f"{day}.candle.blob"
    volumes = {}
    for record in read_tape_index(index_path):
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        if payload.get("interval") != "I1" or payload.get("bar_time_ms") is None:
            continue
        volumes[int(payload["bar_time_ms"]) // 60_000] = float(payload.get("volume") or 0)
    return volumes


def touches(directory: pathlib.Path, day: str):
    index_path = directory / f"{day}.book.index"
    if not index_path.exists():
        return
    blob = directory / f"{day}.book.blob"
    for position, record in enumerate(read_tape_index(index_path)):
        if position % SAMPLE_EVERY or not is_in_session(int(record[0])):
            continue
        try:
            levels = json.loads(read_payload(blob, record)).get("levels") or []
        except Exception:
            continue
        if levels and levels[0].get("bid_price") and levels[0].get("ask_price"):
            yield int(record[0]), levels[0]


def describe(label, values):
    quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q):.6g}" for q in QUANTILES)
    print(f"  {label:52} n={len(values):>8,}  {quantiles}")


def main() -> int:
    master = json.load(gzip.open(MASTER))
    share_underlyings = {
        row["underlying_key"] for row in master
        if row.get("segment") == "NSE_FO" and row.get("instrument_type") in ("CE", "PE")
        and row.get("underlying_type") == "EQUITY"
    }
    ratios, zero_volume, share_half_spreads, shares_seen = [], 0, [], set()
    for day in DAYS:
        for directory in sorted(TAPE.glob("NSE_FO*")):
            volumes = minute_volumes(directory, day)
            if not volumes:
                continue
            for at_ns, touch in touches(directory, day):
                previous_minute = at_ns // 60_000_000_000 - 1
                volume = volumes.get(previous_minute)
                if volume is None:
                    continue
                if volume <= 0:
                    zero_volume += 1
                    continue
                if touch.get("ask_quantity"):
                    ratios.append(touch["ask_quantity"] / (volume * SLICE_INTERVAL_SECONDS / 60.0))
        for key in sorted(share_underlyings):
            for _, touch in touches(TAPE / key, day):
                bid, ask = touch["bid_price"], touch["ask_price"]
                if ask > bid:
                    share_half_spreads.append((ask - bid) / 2.0 / ((ask + bid) / 2.0))
                    shares_seen.add(key)
    print(f"days {', '.join(DAYS)}; in-session book snapshots, every {SAMPLE_EVERY}th\n")
    print("-- option contracts: touch ask quantity / exchange volume per 10s (previous closed minute) --")
    describe("ratio", ratios)
    print(f"  snapshots whose previous minute traded nothing: {zero_volume:,} "
          f"({zero_volume / (zero_volume + len(ratios)):.1%}) -- excluded, a ratio against zero is undefined")
    print(f"\n-- F&O underlying shares: half touch spread, {len(shares_seen)} shares --")
    describe("half spread", share_half_spreads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
