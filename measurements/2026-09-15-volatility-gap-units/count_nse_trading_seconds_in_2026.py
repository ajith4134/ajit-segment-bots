"""How many seconds NSE's cash and F&O market trades in 2026, from NSE's own holiday list.

The denominator that turns an annualised implied volatility into a volatility over a
horizon measured in trading seconds (volatility-gap-detector, 2026-09-15).
"""
import datetime
import json
import sys

sys.path.insert(0, ".")
from parts.stock_market_news_data.market_session_calendar import HOLIDAY_URL  # noqa: E402
from runtime.nse_public_data import NsePublicData, open_browser_session  # noqa: E402

document = NsePublicData(session=open_browser_session(), timeout_seconds=20).read_json(HOLIDAY_URL)
holidays = set()
for segment, rows in document.items():
    if segment not in ("CM", "FO"):
        continue
    for row in rows:
        day = datetime.datetime.strptime(row["tradingDate"], "%d-%b-%Y").date()
        if day.year == 2026 and day.weekday() < 5:
            holidays.add((segment, day))
fo_holidays = {day for segment, day in holidays if segment == "FO"}
weekdays = sum(
    1 for offset in range(365)
    if (datetime.date(2026, 1, 1) + datetime.timedelta(offset)).weekday() < 5
)
session_seconds = (15 * 3600 + 30 * 60) - (9 * 3600 + 15 * 60)  # 09:15-15:30 IST, NSE's normal market
trading_days = weekdays - len(fo_holidays)
print(json.dumps({
    "weekdays_2026": weekdays,
    "fo_weekday_holidays_2026": len(fo_holidays),
    "trading_days_2026": trading_days,
    "session_seconds": session_seconds,
    "trading_seconds_2026": trading_days * session_seconds,
}, indent=1))
