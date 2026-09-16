"""How closely does a premium-inverted implied volatility match the one Upstox states?

Every contract on the captured tape for the day, sampled at each Upstox greeks record:
the contract's last premium and its underlying's last price at that moment, inverted
with runtime/implied_volatility_from_premium.py, compared with the record's own
implied_volatility. Chooses implied_volatility_agreement_tolerance, and shows how often
the search ceiling refuses.

Run: .venv/bin/python measurements/2026-09-16-implied-volatility-from-premium/measure_inversion_against_upstox.py 2026-09-15 [rate]

result-2026-09-15.txt holds the first run, at rate 0.0, before the carry rate existed.
"""
import bisect, collections, statistics, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.runtime.test_implied_volatility_from_premium import (  # noqa: E402
    TAPE, _number, _series, _seconds_to_expiry_close)
from runtime.implied_volatility_from_premium import implied_volatility  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
TOLERANCE = _number("implied_volatility_search_tolerance")
CEILING = _number("implied_volatility_search_ceiling")
SECONDS_PER_YEAR = _number("option_delta_seconds_per_year")
RATE = float(sys.argv[2]) if len(sys.argv) > 2 else _number("implied_volatility_annual_carry_rate")
errors, stated_all, signed = [], [], collections.defaultdict(list)
refused = collections.Counter()
by_staleness = collections.defaultdict(list)
spot_cache = {}
for directory in sorted(TAPE.iterdir()):
    parts = directory.name.split()
    if len(parts) < 4 or parts[2] not in ("CE", "PE"):
        continue
    if parts[0] not in spot_cache:
        spot_cache[parts[0]] = _series(TAPE / parts[0] / DAY, "last_traded_price")
    st, sp = spot_cache[parts[0]]
    gt, iv = _series(directory / f"{DAY}.option_greeks", "implied_volatility")
    tt, pr = _series(directory / DAY, "last_traded_price")
    if not st or not gt or not tt:
        continue
    for at, stated in list(zip(gt, iv))[:: max(1, len(iv) // 20)]:
        p = bisect.bisect_right(tt, at) - 1
        s = bisect.bisect_right(st, at) - 1
        if p < 0 or s < 0:
            continue
        ours = implied_volatility(pr[p], sp[s], float(parts[1]), parts[2],
                                  _seconds_to_expiry_close(at, " ".join(parts[3:]), "15:30"),
                                  SECONDS_PER_YEAR, RATE, TOLERANCE, CEILING)
        if ours is None:
            refused["no volatility prices the premium"] += 1
            continue
        e = abs(ours - stated)
        errors.append(e); stated_all.append(stated)
        signed[parts[2]].append(ours - stated)
        age = (at - tt[p]) / 1e9
        by_staleness["premium <= 5s old" if age <= 5 else "premium > 5s old"].append(e)
q = statistics.quantiles(errors, n=20)
print(f"{DAY} at rate {RATE}: {len(errors)} paired records, refused {dict(refused)}")
print(f"  |ours - upstox|  median {statistics.median(errors):.4f}  p75 {q[14]:.4f}  p90 {q[17]:.4f}  p95 {q[18]:.4f}")
print(f"  upstox stated    median {statistics.median(stated_all):.4f}  max {max(stated_all):.4f}")
for k, v in sorted(signed.items()):
    print(f"  {k} signed (ours - upstox) median {statistics.median(v):+.4f}  n {len(v)}")
for k, v in sorted(by_staleness.items()):
    print(f"  {k:<20} n {len(v):>6}  median error {statistics.median(v):.4f}")
