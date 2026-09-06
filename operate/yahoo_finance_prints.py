"""Free historical prices for Indian equities and indices, with no account at all.

Upstox is this project's broker and its history endpoint has a **daily** quota:
after a day of fetching it answers HTTP 429 and keeps refusing through four
backoffs totalling 200 seconds (measured 2026-09-06). A replay that can only run
while that quota lasts is not a replay anybody can rely on, and the operator
asked for sources that do not depend on one broker.

Yahoo's chart endpoint needs no account, no key and no registration, and its
Indian coverage is real. Measured against this project's own captured Upstox
tape for 2026-09-04, minute by minute:

    MARUTI       133 shared minutes   median difference 0.0000%   worst 0.047%
    NATIONALUM   133 shared minutes   median difference 0.0000%   worst 0.080%
    BAJAJ-AUTO    82 shared minutes   median difference 0.0000%   worst 0.067%

The worst case is the difference between a bar's close and the last print inside
that minute, which is what those two things actually are. This is the same data.

**What it covers, measured the same day:**

    1m   5 days per request, roughly a month back      1,875 bars for a full week
    5m   60 days                                       4,500 bars
    1d   10 years and more                             2,474 bars

**What it does not cover: option contracts.** Yahoo carries no NSE options, so
the two options segments cannot be replayed from here -- `operate/nse_fo_bhavcopy.py`
is the free source for those, and Upstox remains the only free source of
*intraday* option prices. This is for the cash-equity segment and for the spot
of an option's underlying, which is what picking a strike needs.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.parse

CHART_HOST = "https://query1.finance.yahoo.com/v8/finance/chart"
# Yahoo's own suffix for an NSE listing. BSE is ".BO"; this project trades NSE.
NSE_SUFFIX = ".NS"
SECONDS_BETWEEN_REQUESTS = 0.25
MILLISECONDS_TO_NANOSECONDS = 1_000_000

# Yahoo's tickers for the indices this project's segments name. An index is not
# a listed security and has no ISIN-style key, so there is no rule to derive
# these from -- they are looked up once and written down, each verified against
# the live endpoint on 2026-09-06.
INDEX_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "MIDCPNIFTY": "^NSEMDCP50",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
}

_last_request_at = [0.0]


def open_browser_session():
    """A real browser TLS fingerprint, as every other outward call here uses."""
    from curl_cffi import requests

    return requests.Session(impersonate="chrome")


def ticker_for(trading_symbol: str, instrument_type: str) -> str | None:
    """Yahoo's name for one instrument, or None when Yahoo cannot carry it.

    None for an option: Yahoo has no NSE option chain, and guessing a ticker
    would produce a confident 404 that reads like a quiet contract.
    """
    if instrument_type in ("CE", "PE"):
        return None
    if instrument_type == "INDEX":
        return INDEX_TICKERS.get(trading_symbol)
    return f"{trading_symbol}{NSE_SUFFIX}"


def _wait_our_turn() -> None:
    since = time.monotonic() - _last_request_at[0]
    if since < SECONDS_BETWEEN_REQUESTS:
        time.sleep(SECONDS_BETWEEN_REQUESTS - since)
    _last_request_at[0] = time.monotonic()


def fetch_chart(ticker: str, interval: str, span: str, session=None,
                timeout_seconds: float = 25.0) -> dict:
    """One chart response, as Yahoo returns it."""
    session = session or open_browser_session()
    url = f"{CHART_HOST}/{urllib.parse.quote(ticker)}?interval={interval}&range={span}"
    _wait_our_turn()
    response = session.get(url, timeout=timeout_seconds)
    if response.status_code != 200:
        raise RuntimeError(
            f"Yahoo refused {ticker} at {interval}/{span}: HTTP {response.status_code}"
        )
    return response.json()


def prints_from_chart(document: dict, only_day: str | None = None) -> list[tuple[int, float]]:
    """Each bar as one price at its own time: (at_ns, close), oldest first.

    The close, for the same reason the Upstox source uses it: the high and low
    happened somewhere inside the bar and nothing says when, so replaying them
    would place prices at times they did not occur.

    A bar whose close is null is dropped rather than carried forward. Yahoo
    leaves a gap where the exchange did not trade, and filling it would invent a
    price for a minute that had none.
    """
    import datetime

    chart = document.get("chart") or {}
    results = chart.get("result") or []
    if chart.get("error") or not results:
        return []
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    prints: list[tuple[int, float]] = []
    for at_seconds, close in zip(stamps, closes):
        if close is None:
            continue
        if only_day is not None:
            at = datetime.datetime.fromtimestamp(at_seconds, datetime.UTC)
            if at.strftime("%Y-%m-%d") != only_day:
                continue
        prints.append((int(at_seconds) * 1_000_000_000, float(close)))
    return prints


def historical_prints(trading_symbol: str, instrument_type: str, day: str,
                      interval: str = "1m", span: str = "5d",
                      session=None) -> list[tuple[int, float]]:
    """One instrument's real prices for one past session, oldest first.

    Empty when Yahoo cannot carry this instrument at all -- an option -- and
    empty when it has no bars for that day, which are different facts the caller
    can tell apart by asking `ticker_for` first.
    """
    ticker = ticker_for(trading_symbol, instrument_type)
    if ticker is None:
        return []
    return prints_from_chart(fetch_chart(ticker, interval, span, session=session), only_day=day)


__all__ = [
    "CHART_HOST",
    "INDEX_TICKERS",
    "NSE_SUFFIX",
    "fetch_chart",
    "historical_prints",
    "open_browser_session",
    "prints_from_chart",
    "ticker_for",
]
