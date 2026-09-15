"""NIFTY 15 SEP 26 premiums across the chain on its own expiry day, from NSE's intraday chart.

Answers where the zero-to-hero detector's territory (premium <= Rs5, |delta| <= 0.10) sits
on a real expiry day, and how many strikes wide it is, so the subscription reaches it.
"""
import datetime
import sys
import time

sys.path.insert(0, ".")
from operate.nse_intraday_option_prices import fetch_chart, identifier_for, open_browser_session, prints_from_chart  # noqa: E402

session = open_browser_session()
expiry = datetime.date(2026, 9, 15)
spot = float(sys.argv[1])
for option_type, strikes in (("CE", range(23300, 24301, 50)), ("PE", range(22300, 23401, 50))):
    for strike in strikes:
        identifier = identifier_for("NIFTY", expiry, option_type, float(strike), True)
        try:
            points = prints_from_chart(fetch_chart(identifier, session=session))
        except Exception as failure:  # noqa: BLE001 -- a measurement script reports and moves on
            print(option_type, strike, "unread", type(failure).__name__)
            continue
        last = points[-1][1] if points else None
        distance = (strike - spot) / spot
        print(f"{option_type} {strike}  distance {distance:+.2%}  last {last}  points {len(points)}", flush=True)
        time.sleep(0.4)
