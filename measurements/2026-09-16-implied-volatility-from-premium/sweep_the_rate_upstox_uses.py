"""Which annual rate makes a premium-inverted volatility agree with Upstox's own?

Without a rate, calls invert high and puts low by the same amount (+0.0097 / -0.0094
on 2026-09-15) -- the signature of a rate the broker includes and this basis leaves
out. This sweeps the rate on the same paired records and prints the error for each.

Run: .venv/bin/python measurements/2026-09-16-implied-volatility-from-premium/sweep_the_rate_upstox_uses.py 2026-09-15
"""
import bisect, math, statistics, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.runtime.test_implied_volatility_from_premium import TAPE, _series, _seconds_to_expiry_close  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
YEAR = 31536000.0

def cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def price(S, K, kind, v, T, r):
    sd = v * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / sd
    c = S * cdf(d1) - K * math.exp(-r * T) * cdf(d1 - sd)
    return c if kind == "CE" else c - S + K * math.exp(-r * T)

def invert(P, S, K, kind, T, r):
    if T <= 0 or P <= 0 or P > price(S, K, kind, 5.0, T, r):
        return None
    lo, hi = 1e-6, 5.0
    if P <= price(S, K, kind, lo, T, r):
        return None
    while hi - lo > 1e-5:
        m = (lo + hi) / 2
        lo, hi = (m, hi) if price(S, K, kind, m, T, r) < P else (lo, m)
    return (lo + hi) / 2

samples, spot_cache = [], {}
for d in sorted(TAPE.iterdir()):
    parts = d.name.split()
    if len(parts) < 4 or parts[2] not in ("CE", "PE"):
        continue
    if parts[0] not in spot_cache:
        spot_cache[parts[0]] = _series(TAPE / parts[0] / DAY, "last_traded_price")
    st, sp = spot_cache[parts[0]]
    gt, iv = _series(d / f"{DAY}.option_greeks", "implied_volatility")
    tt, pr = _series(d / DAY, "last_traded_price")
    if not st or not gt or not tt:
        continue
    for at, stated in list(zip(gt, iv))[:: max(1, len(iv) // 5)]:
        p, s = bisect.bisect_right(tt, at) - 1, bisect.bisect_right(st, at) - 1
        if p >= 0 and s >= 0:
            T = _seconds_to_expiry_close(at, " ".join(parts[3:]), "15:30") / YEAR
            samples.append((pr[p], sp[s], float(parts[1]), parts[2], T, stated))
print(f"{DAY}: {len(samples)} paired records")
for r in (0.0, 0.02, 0.04, 0.05, 0.055, 0.06, 0.065, 0.07, 0.08, 0.10):
    err, sign = [], {"CE": [], "PE": []}
    for P, S, K, kind, T, stated in samples:
        v = invert(P, S, K, kind, T, r)
        if v is not None:
            err.append(abs(v - stated)); sign[kind].append(v - stated)
    print(f"  rate {r:.3f}  median |error| {statistics.median(err):.4f}  "
          f"CE {statistics.median(sign['CE']):+.4f}  PE {statistics.median(sign['PE']):+.4f}  n {len(err)}")
