"""What the zero-to-hero sweep costs, before and after scoping it by expiry day.

The live part could not answer this on 2026-09-04: it ran for ten minutes with
`listings_seen` at zero, because `broker-instrument-catalogue-reader` restates the
master on its own long interval and the detector's process had been restarted
after the last burst. So the cost is measured here instead, directly, against the
real instrument master the broker sent -- the same file
`tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json`, read by
the venue's own adapter, repeated to the 102,940 rows the live catalogue carries.

The two sweeps are the real ones: `walk_every_instrument` is what `tick` did until
this date, and `walk_todays_expiries` is what it does now.

    .venv/bin/python measurements/2026-09-04-what-burns-cpu-with-no-trades/measure_the_expiry_sweep.py
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from parts.opportunity_scanner.expiry_day_zero_to_hero_detector import (  # noqa: E402
    IST,
    ZeroToHeroDetector,
)
from runtime.brokers.upstox import UpstoxAdapter  # noqa: E402
from runtime.market_signal import SignalCalibrator  # noqa: E402

CAPTURED = ROOT / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
LIVE_CATALOGUE_ROWS = 102_940


def real_listings() -> tuple:
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    return tuple(UpstoxAdapter.read_instrument_listings(adapter, json.loads(CAPTURED.read_text())))


ONE_DAY_MS = 24 * 60 * 60 * 1000


def a_catalogue_of(rows: int, distinct_expiries: int) -> tuple:
    """The real master, repeated under distinct keys and spread over N expiry dates.

    Spreading matters and is the whole reason this takes a parameter. The captured
    slice is one underlying's ladder across two expiries, so repeating it verbatim
    puts 89% of a 102,940-row catalogue on a single date -- and then scoping the
    sweep to "today" saves nothing, which is what the first version of this script
    reported. The live NSE master is not shaped like that: it carries weekly index
    expiries and monthly stock expiries for many underlyings at once, so any one
    day is a slice of it.

    How large a slice is exactly what this script does not know and does not
    guess. It reports the curve instead, and the reader picks the row matching
    whatever the live master turns out to hold.
    """
    real = real_listings()
    out = []
    copy = 0
    while len(out) < rows:
        for listing in real:
            shifted = listing
            if listing.expiry_ms is not None:
                shifted = dataclasses.replace(
                    listing,
                    expiry_ms=listing.expiry_ms + (copy % distinct_expiries) * ONE_DAY_MS,
                )
            out.append(
                dataclasses.replace(
                    shifted, instrument_key=f"{listing.instrument_key}#{copy}"
                )
            )
            if len(out) >= rows:
                break
        copy += 1
    return tuple(out)


def a_detector() -> ZeroToHeroDetector:
    return ZeroToHeroDetector(
        maximum_premium=5.0,
        maximum_abs_delta=0.10,
        horizon_seconds=3600.0,
        calibrator=SignalCalibrator(
            prior_hit_rate=0.1, prior_weight=10.0,
            half_life_observations=50.0, minimum_observations=5,
        ),
    )


def walk_every_instrument(detector, keys, now_ns) -> int:
    """What `tick` did until 2026-09-04: judge every instrument, every tick."""
    return sum(
        1
        for key in sorted(keys)
        for candidate, _ in (detector.detect(key, now_ns=now_ns),)
        if candidate is not None
    )


def walk_todays_expiries(detector, _keys, now_ns) -> int:
    """What it does now: only contracts whose expiry date is today."""
    return sum(
        1
        for key in detector.expiring_on(now_ns)
        for candidate, _ in (detector.detect(key, now_ns=now_ns),)
        if candidate is not None
    )


def time_sweep(sweep, detector, keys, now_ns, rounds=3) -> float:
    sweep(detector, keys, now_ns)  # warm
    started = time.perf_counter()
    for _ in range(rounds):
        sweep(detector, keys, now_ns)
    return (time.perf_counter() - started) / rounds


def main() -> int:
    print(f"catalogue {LIVE_CATALOGUE_ROWS:,} instruments, built from the real NSE master")
    print("a tick is one a second, so seconds per sweep reads as cores\n")
    print(f"{'live expiries':>14}{'walked':>12}{'every instrument':>20}{'todays expiries':>18}")
    for distinct in (1, 2, 5, 10, 20, 40):
        listings = a_catalogue_of(LIVE_CATALOGUE_ROWS, distinct)
        detector = a_detector()
        for listing in listings:
            detector.observe_listing(listing)
        keys = [listing.instrument_key for listing in listings]

        day = sorted(
            {
                datetime.datetime.fromtimestamp(listing.expiry_ms / 1000, tz=IST).date()
                for listing in listings
                if listing.expiry_ms is not None
            }
        )[0]
        now_ns = int(
            datetime.datetime(day.year, day.month, day.day, 12, 0, tzinfo=IST).timestamp() * 1e9
        )
        walked = len(detector.expiring_on(now_ns))
        before = time_sweep(walk_every_instrument, detector, keys, now_ns)
        after = time_sweep(walk_todays_expiries, detector, keys, now_ns)
        print(f"{distinct:>14}{walked:>12,}{before:>17.3f} c{after:>16.3f} c")
    print("\nThe left column is how many expiry dates are live at once in the master.")
    print("The saving is exactly the fraction of it that does not expire today.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
