"""How many past sessions of one-minute option history can this project read?

The fourth goal (docs/goal.md, 2026-09-16) counts a pattern only if it is fitted
on earlier sessions and scored on later ones. For the option segments that needs
intraday option prices across many sessions, and the depth has never been
measured -- CLAUDE.md says Upstox serves "every currently listed option" from
January 2022, which cannot be true of a contract that listed three months ago.

Two routes, both asked, both reported:

1. **Listed contracts** through `operate/historical_prints.historical_candles`
   (the ordinary, cached history endpoint), walked back one month per request --
   Upstox's own limit for one-minute bars -- until a month comes back empty.
2. **Expired contracts** through Upstox's expired-instruments API. Upstox's own
   page says it needs the Plus plan; whether this account has it is asked, not
   assumed, and the status is printed verbatim.

Exact endpoint syntax was read from Upstox's raw documentation page on
2026-09-16, not from a summariser, which gave a different and wrong path:

    GET /v2/expired-instruments/expiries?instrument_key=...
    GET /v2/expired-instruments/option/contract?instrument_key=...&expiry_date=YYYY-MM-DD
    GET /v2/expired-instruments/historical-candle/{key}|{DD-MM-YYYY}/{interval}/{to}/{from}

Run: .venv/bin/python measurements/2026-09-16-how-deep-option-history-goes/measure_option_history_depth.py
"""

from __future__ import annotations

import collections
import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from operate.historical_prints import (  # noqa: E402
    _wait_our_turn,
    historical_candles,
    upstox_access_token,
)
from operate.replay_a_captured_session import instruments_by_key  # noqa: E402
from runtime.brokers.broker_http_request import build_broker_request  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TODAY = datetime.datetime.now(IST).date()
# How far back to look. Longer than any monthly contract's listed life, so an
# empty month is the contract's start, not the probe's edge.
LOOKBACK_MONTHS = 8
# A session counts only with most of its bars: a contract that printed twice in a
# day is not a day a detector could have been run on.
MINIMUM_BARS_FOR_A_SESSION = 60
UNDERLYINGS = ("NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK", "INFY", "TATAMOTORS", "SBIN")
EXPIRIES_PER_UNDERLYING = 3
API = "https://api.upstox.com/v2"


def month_windows(months: int):
    """(from, to) date strings, newest month first, ending yesterday."""
    end = TODAY - datetime.timedelta(days=1)
    for _ in range(months):
        start = (end.replace(day=1))
        yield start.isoformat(), end.isoformat()
        end = start - datetime.timedelta(days=1)


def sessions_of(key: str, token: str):
    """{date: bar count} for one instrument, walking back until a month is empty."""
    bars_by_day: collections.Counter = collections.Counter()
    for start, end in month_windows(LOOKBACK_MONTHS):
        try:
            candles = historical_candles(key, start, end, token)
        except RuntimeError as refusal:
            print(f"      {start}..{end}: {refusal}")
            break
        if not candles:
            break
        for candle in candles:
            day = datetime.datetime.fromtimestamp(candle.bar_time_ms / 1000, IST).date()
            bars_by_day[day] += 1
    return bars_by_day


def spot_from_tape(underlying: str) -> float | None:
    """The underlying's latest captured price, to pick strikes near the money."""
    from runtime.tape import read_payload, read_tape_index

    directory = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox" / underlying
    indexes = sorted(p for p in directory.glob("*.index") if p.name.count(".") == 1)
    for index in reversed(indexes):
        records = read_tape_index(index)
        if len(records):
            blob = index.with_name(index.name.replace(".index", ".blob"))
            price = json.loads(read_payload(blob, records[-1])).get("last_traded_price")
            if price:
                return float(price)
    return None


def listed_contracts(master: dict):
    """(underlying, expiry date, row) for the nearest-the-money call of each listed expiry."""
    for underlying in UNDERLYINGS:
        chain = [
            row for row in master.values()
            if row.get("underlying_symbol") == underlying
            and row.get("instrument_type") == "CE"
            and row.get("segment") == "NSE_FO"
            and row.get("expiry") and row.get("strike_price")
        ]
        if not chain:
            print(f"  {underlying}: no listed calls in the master")
            continue
        spot = spot_from_tape(underlying)
        expiries = sorted({row["expiry"] for row in chain})[:EXPIRIES_PER_UNDERLYING]
        for expiry in expiries:
            at_expiry = [row for row in chain if row["expiry"] == expiry]
            if spot is None:
                strikes = sorted(row["strike_price"] for row in at_expiry)
                reference = strikes[len(strikes) // 2]
            else:
                reference = spot
            row = min(at_expiry, key=lambda r: abs(r["strike_price"] - reference))
            yield underlying, datetime.datetime.fromtimestamp(expiry / 1000, IST).date(), row


def ask(url: str, token: str):
    """(HTTP status, parsed body or error text). A refusal is reported, never hidden."""
    _wait_our_turn()
    request = build_broker_request(url, access_token=token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refusal:
        return refusal.code, refusal.read().decode(errors="replace")[:300]


def probe_expired_route(master: dict, token: str) -> None:
    index_key = next(
        (row["underlying_key"] for row in master.values()
         if row.get("underlying_symbol") == "NIFTY" and row.get("underlying_key")),
        None,
    )
    print("\nEXPIRED CONTRACTS (Upstox expired-instruments API)")
    if index_key is None:
        print("  no NIFTY underlying key in the master; route not asked")
        return
    quoted = urllib.parse.quote(index_key, safe="")
    status, body = ask(f"{API}/expired-instruments/expiries?instrument_key={quoted}", token)
    print(f"  expiries for {index_key}: HTTP {status}")
    if status != 200:
        print(f"    {body}")
        return
    expiries = sorted(body.get("data") or [])
    print(f"    {len(expiries)} expiries, earliest {expiries[:1]}, latest {expiries[-1:]}")
    if not expiries:
        return
    expiry = expiries[-1]
    status, body = ask(
        f"{API}/expired-instruments/option/contract?instrument_key={quoted}&expiry_date={expiry}",
        token,
    )
    print(f"  contracts for {expiry}: HTTP {status}")
    if status != 200 or not body.get("data"):
        print(f"    {body if status != 200 else 'empty'}")
        return
    contract = body["data"][len(body["data"]) // 2]
    expired_key = contract.get("instrument_key")
    print(f"    {len(body['data'])} contracts; asking one: {contract.get('trading_symbol')} {expired_key}")
    day = datetime.date.fromisoformat(expiry)
    path_key = urllib.parse.quote(expired_key, safe="")
    status, body = ask(
        f"{API}/expired-instruments/historical-candle/{path_key}/1minute/"
        f"{day.isoformat()}/{(day - datetime.timedelta(days=30)).isoformat()}",
        token,
    )
    print(f"  one-minute candles: HTTP {status}")
    if status == 200:
        candles = (body.get("data") or {}).get("candles") or []
        days = {c[0][:10] for c in candles if isinstance(c, list) and c}
        print(f"    {len(candles)} bars across {len(days)} sessions")
    else:
        print(f"    {body}")


def main() -> int:
    token = upstox_access_token()
    master = instruments_by_key()
    print(f"LISTED CONTRACTS (history endpoint, one month per request), measured {TODAY}")
    print(f"  a session = a day with at least {MINIMUM_BARS_FOR_A_SESSION} one-minute bars\n")
    depth = []
    for underlying, expiry, row in listed_contracts(master):
        bars = sessions_of(row["instrument_key"], token)
        sessions = sorted(day for day, count in bars.items() if count >= MINIMUM_BARS_FOR_A_SESSION)
        first = sessions[0] if sessions else None
        depth.append(len(sessions))
        print(f"  {row['trading_symbol']:<32} expires {expiry}  "
              f"sessions {len(sessions):>3}  first {first}  "
              f"(days with any bar {len(bars)})")
    if depth:
        print(f"\n  deepest listed contract: {max(depth)} sessions; "
              f"median across {len(depth)}: {sorted(depth)[len(depth) // 2]}")
    probe_expired_route(master, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
