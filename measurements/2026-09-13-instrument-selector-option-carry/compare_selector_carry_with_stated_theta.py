"""Does instrument-selector's option carry agree with the theta Upstox states?

instrument_selector.carry_over prices an option's carry as

    premium_fraction x (1 - sqrt(1 - held)),   held = horizon / seconds_to_expiry

where premium_fraction = option price / underlying spot (_refresh_atm_instruments). The
result is added to round_trip_cost_fraction -- 0.008532, a fraction of the PREMIUM
traded -- and compared against instrument_maximum_cost_fraction. If the carry is a
fraction of spot while the cost is a fraction of premium, the carry is scaled down by
spot/premium before it is weighed.

For each near-the-money contract (0.4 <= |delta| <= 0.6, what the selector registers as
ATM) on the 2026-09-07/08 tape, at each sampled greeks update in session, over a horizon:

    code      premium/spot x (1 - sqrt(1 - held))            as instrument-selector computes it
    model     (1 - sqrt(1 - held))                           the same decay, as a fraction of premium
    stated    |theta| x horizon/86400 / premium              Upstox's theta, fraction of premium

Spot is the underlying's last distinct trade at or before the greeks update; premium the
contract's. Horizons: 300s and 3600s (the detectors' mean_reversion/momentum 300s and
volatility_gap/zero_to_hero 3600s).

Run:  .venv/bin/python measurements/2026-09-13-instrument-selector-option-carry/compare_selector_carry_with_stated_theta.py
"""

from __future__ import annotations

import bisect
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
HORIZONS = (300.0, 3600.0)
ROUND_TRIP = 0.008532
QUANTILES = (0.20, 0.50, 0.80)


def series(key, day):
    trades = cadence.distinct_trades(cadence.TAPE / key, day)
    return [t for t, _, _ in trades], [p for _, p, _ in trades]


def main() -> int:
    options = {r["instrument_key"]: r for r in json.load(gzip.open(cadence.MASTER))
               if r.get("segment") == "NSE_FO" and r.get("instrument_type") in ("CE", "PE")}
    rows = {h: {"code": [], "model": [], "stated": []} for h in HORIZONS}
    spot_cache = {}
    for day in DAYS:
        for directory in sorted(cadence.TAPE.glob("NSE_FO*")):
            row = options.get(directory.name)
            greeks = directory / f"{day}.option_greeks.index"
            if row is None or not greeks.exists() or not row.get("expiry"):
                continue
            under = (row["underlying_key"], day)
            if under not in spot_cache:
                spot_cache[under] = series(row["underlying_key"], day)
            spot_times, spot_prices = spot_cache[under]
            if not spot_times:
                continue
            times, prices = series(directory.name, day)
            if not times:
                continue
            blob = directory / f"{day}.option_greeks.blob"
            for position, record in enumerate(read_tape_index(greeks)):
                at = int(record[0])
                if position % 10 or not cadence.is_in_session(at):
                    continue
                try:
                    payload = json.loads(read_payload(blob, record))
                except Exception:
                    continue
                delta, theta = payload.get("delta"), payload.get("theta")
                if delta is None or theta is None or not 0.4 <= abs(float(delta)) <= 0.6:
                    continue
                i, j = bisect.bisect_right(times, at) - 1, bisect.bisect_right(spot_times, at) - 1
                if i < 0 or j < 0 or prices[i] <= 0 or spot_prices[j] <= 0:
                    continue
                premium, spot = prices[i], spot_prices[j]
                seconds_to_expiry = (row["expiry"] - at / 1e6) / 1000.0
                if seconds_to_expiry <= 0:
                    continue
                for horizon in HORIZONS:
                    held = min(1.0, horizon / seconds_to_expiry)
                    decay = 1.0 - math.sqrt(1.0 - held)
                    rows[horizon]["code"].append(premium / spot * decay)
                    rows[horizon]["model"].append(decay)
                    rows[horizon]["stated"].append(abs(float(theta)) * horizon / 86400.0 / premium)
    print(f"near-the-money contracts, {', '.join(DAYS)}; round trip for scale {ROUND_TRIP}\n")
    for horizon in HORIZONS:
        n = len(rows[horizon]["code"])
        print(f"-- horizon {horizon:.0f}s, {n:,} greeks samples --")
        for name in ("code", "model", "stated"):
            values = rows[horizon][name]
            quantiles = "  ".join(f"p{int(q * 100)} {numpy.quantile(values, q):.6f}" for q in QUANTILES)
            print(f"  {name:7} {quantiles}")
        code, stated = numpy.asarray(rows[horizon]["code"]), numpy.asarray(rows[horizon]["stated"])
        ratio = numpy.median(stated / numpy.where(code > 0, code, numpy.nan))
        print(f"  median stated / code: {ratio:,.1f}x; stated carry above the round trip on "
              f"{(stated > ROUND_TRIP).mean():.1%} of samples, code carry on {(code > ROUND_TRIP).mean():.1%}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
