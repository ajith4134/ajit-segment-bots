"""How long an authenticated Upstox REST round trip takes from this box.

order_latency_prior was "REST order placement to both venues from this box measured
80 to 200 ms on 2026-08-22" -- Binance and Bybit. An order cannot be placed to
measure it (paper money mode, and an order is not a probe), so this times two
read-only authenticated GETs on the same API host, each on a fresh connection as
urllib opens one: DNS, TCP, TLS, Upstox's auth and a response. It is a lower bound
on an order's round trip -- an order does more work at the broker -- which is the
direction the setting's own rule needs checked ("a paper fill is never faster than a
live one would have been").

Run:  .venv/bin/python measurements/2026-09-13-indian-observation-cadence/measure_upstox_round_trip.py
"""

from __future__ import annotations

import pathlib
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from operate.historical_prints import upstox_access_token
from runtime.brokers.broker_http_request import build_broker_request

CALLS = 40
SPACING_SECONDS = 1.0
ENDPOINTS = {
    "funds": "https://api.upstox.com/v2/user/get-funds-and-margin",
    "ltp": "https://api.upstox.com/v2/market-quote/ltp?"
           + urllib.parse.urlencode({"instrument_key": "NSE_INDEX|Nifty 50"}),
}


def main() -> int:
    token = upstox_access_token()
    for name, url in ENDPOINTS.items():
        seconds, failures = [], 0
        for _ in range(CALLS):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(build_broker_request(url, access_token=token), timeout=10) as response:
                    response.read()
                seconds.append(time.perf_counter() - started)
            except Exception as error:
                failures += 1
                print(f"  {name}: {type(error).__name__}: {error}")
            time.sleep(SPACING_SECONDS)
        if seconds:
            ordered = sorted(seconds)
            print(f"{name:6} {len(seconds)} ok, {failures} failed  "
                  f"p50 {statistics.median(ordered) * 1000:.0f} ms  "
                  f"p90 {ordered[int(0.9 * (len(ordered) - 1))] * 1000:.0f} ms  "
                  f"max {ordered[-1] * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
