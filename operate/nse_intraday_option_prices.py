"""Free **intraday** option prices, from NSE, with no account at all.

This is the source that was missing. Yahoo carries no NSE option chain, and
`nse_fo_bhavcopy` is one OHLC per contract per session -- so until this, the only
free way to walk an option minute by minute was Upstox, whose history endpoint
has a daily quota. Both options segments are Phase A, so "intraday option prices
cost a quota" was a real bound on what could be replayed.

NSE serves its own intraday chart for any contract, free and unauthenticated.
Measured 2026-09-06 for the session of 2026-09-04:

    OPTIDXNIFTY08-09-2026CE23900.00      2,037 points   09:15 .. 15:40 IST
    OPTIDXNIFTY08-09-2026PE23900.00      2,037 points
    OPTSTKRELIANCE29-09-2026CE1320.00    1,723 points

Index options and stock options both, which is both Phase A segments.

**It serves the most recent session only.** There is no date parameter: the
endpoint answers with whatever the last trading day was. So this is the source
for "replay the session that just happened", and `nse_fo_bhavcopy` remains the
one with history (about two years, daily), and Upstox the one that has both at
the cost of a quota. Naming that plainly is the point -- a source that quietly
returned the wrong day would be worse than none.

**The timestamps are IST wall-clock encoded as an epoch.** NSE sends
1788513301000 for 09:15 IST, which read as UTC is 09:15 UTC -- 14:45 IST, after
the close. Measured against this project's own captured tape for the same
contract: read as UTC, 52 shared minutes at a 3.88% median difference; shifted
back 5:30, 69 shared minutes at **0.30%**, which is tick-against-tick noise on a
150-rupee option. The project already carries this warning for Upstox's
historical rows (`read_historical_candles`: "15:39 IST is 10:09 UTC. Read as
UTC, every bar of the Indian session lands outside it") and it is the same trap
on a different endpoint.
"""

from __future__ import annotations

import datetime
import json
import pathlib

NSE_HOME = "https://www.nseindia.com"
# Warmed through the option-chain page rather than the homepage alone: this
# endpoint is what that page calls, and a session that has not been there is
# refused.
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
CHART_URL = "https://www.nseindia.com/api/chart-databyindex"

INDEX_OPTION_PREFIX = "OPTIDX"
STOCK_OPTION_PREFIX = "OPTSTK"
# What NSE's stamps really are: Indian Standard Time, written as though UTC.
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
MILLISECONDS_TO_NANOSECONDS = 1_000_000

CACHE = pathlib.Path.home() / ".local/share/ajit-segment-bots/history/nse-intraday-options"


def open_browser_session():
    """A warmed session. Two pages, for the reason `NsePublicData` states: the
    cookie every later call is checked against is set by browsing, not by asking."""
    from curl_cffi import requests

    session = requests.Session(impersonate="chrome")
    session.get(NSE_HOME, timeout=20)
    session.get(NSE_OPTION_CHAIN_PAGE, timeout=20)
    return session


def identifier_for(underlying: str, expiry: datetime.date, option_type: str,
                   strike: float, underlying_is_an_index: bool) -> str:
    """NSE's own name for one contract on this endpoint.

    `OPTIDXNIFTY08-09-2026CE23900.00` -- prefix, underlying, DD-MM-YYYY, CE/PE,
    then the strike to two decimals. The strike's decimals are not cosmetic: NSE
    answers 200 with an empty series for a name it does not recognise, so a
    strike written as `23900` rather than `23900.00` reads as a contract nobody
    traded rather than as a request nobody understood.
    """
    prefix = INDEX_OPTION_PREFIX if underlying_is_an_index else STOCK_OPTION_PREFIX
    return f"{prefix}{underlying}{expiry:%d-%m-%Y}{option_type}{strike:.2f}"


def cache_path_for(identifier: str, day: str) -> pathlib.Path:
    return CACHE / day / f"{identifier}.json"


def fetch_chart(identifier: str, session=None, timeout_seconds: float = 25.0) -> dict:
    """One contract's intraday series, as NSE returns it."""
    session = session or open_browser_session()
    response = session.get(
        f"{CHART_URL}?index={identifier}&indices=false", timeout=timeout_seconds,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"NSE refused the intraday chart for {identifier}: HTTP {response.status_code}"
        )
    return response.json()


def prints_from_chart(document: dict) -> list[tuple[int, float]]:
    """The series as (at_ns, price), oldest first, in true UTC.

    Empty when NSE recognised the request and has nothing for it -- an untraded
    strike, or a name it does not know. Both are honestly "no prices", and the
    caller distinguishes them by whether the contract traded in the bhavcopy.
    """
    points = document.get("grapthData") or []
    prints: list[tuple[int, float]] = []
    for point in points:
        try:
            at_ms, price = point[0], point[1]
        except (IndexError, TypeError):
            continue
        if not price:
            continue
        # Written as IST wall-clock; shifted back to the instant it really was.
        at = datetime.datetime.fromtimestamp(at_ms / 1000, datetime.UTC) - IST_OFFSET
        prints.append((int(at.timestamp() * 1e9), float(price)))
    prints.sort()
    return prints


def intraday_prints(identifier: str, day: str, session=None,
                    use_cache: bool = True) -> list[tuple[int, float]]:
    """One contract's most recent session, cached under the day it was fetched for.

    Cached because the endpoint has no date parameter: once a session is over,
    what it returned for that session is all it will ever return, and refetching
    it after the next open would silently hand back a different day.
    """
    path = cache_path_for(identifier, day)
    if use_cache:
        try:
            return [(int(at), float(price)) for at, price in json.loads(path.read_text())]
        except (OSError, ValueError, TypeError):
            pass
    prints = prints_from_chart(fetch_chart(identifier, session=session))
    if use_cache and prints:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            being_written = path.with_suffix(".writing")
            being_written.write_text(json.dumps(prints))
            being_written.replace(path)
        except OSError:
            pass
    return prints


def the_session_this_serves(document: dict) -> str | None:
    """Which day the returned series is actually for, read from the series.

    Asked rather than assumed: the endpoint takes no date, so a caller that
    believed it had asked for one would replay whatever the last session was and
    call it the day it wanted.
    """
    prints = prints_from_chart(document)
    if not prints:
        return None
    at = datetime.datetime.fromtimestamp(prints[0][0] / 1e9, datetime.UTC) + IST_OFFSET
    return at.strftime("%Y-%m-%d")


__all__ = [
    "CACHE",
    "CHART_URL",
    "INDEX_OPTION_PREFIX",
    "IST_OFFSET",
    "STOCK_OPTION_PREFIX",
    "cache_path_for",
    "fetch_chart",
    "identifier_for",
    "intraday_prints",
    "open_browser_session",
    "prints_from_chart",
    "the_session_this_serves",
]
