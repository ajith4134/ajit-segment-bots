"""Real Indian market prices for any past date, from the broker's own history.

The tape is the truth about what this project *saw*, and it is three days deep:
capture started on 2026-09-02, and only for the instruments the feed happened to
be subscribed to. That is a hard bound on what a replay can prove -- one Friday,
on whatever the subscription held.

Upstox serves one-minute candles from **January 2022**, for equities, indices and
every currently listed option contract. Verified against the live API on
2026-09-06 with this project's own adapter and token:

    RELIANCE  2022-01-10..14   1,875 bars      NIFTY 50 index  2025-09-01..05  1,875 bars
    RELIANCE  2025-09-01..05   1,875 bars      NIFTY 24000 CE  2026-09-01..05  1,540 bars

so a replay can run on months of real Indian sessions rather than on the three
days this machine happens to have captured. That is the operator's own
suggestion, made 2026-09-06.

**A bar close is not a trade print, and this never pretends otherwise.** The
prints it returns are the close of each one-minute bar at that bar's own
timestamp, which is exactly what `broker-history-reader` already publishes as
`market-data` (its own `trades_from`, carrying
`TradeFidelity.HISTORICAL_BAR_CLOSE`). Within a minute the replay therefore sees
one price rather than every print, so a stop and a target that sit inside one
minute's range both look reachable and only the bar close decides. A tape replay
is finer-grained and stays the better evidence where it exists; this is what
makes a replay possible at all for a date the tape never covered.

Nothing here is a second implementation of the broker: the URL and the parsing
are `UpstoxAdapter`'s own, and the request goes through
`runtime/brokers/broker_http_request.py` -- which is what stops Cloudflare
answering it with `Error 1010` (2026-09-06).
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runtime.brokers.broker_http_request import build_broker_request
from runtime.brokers.upstox import UpstoxAdapter

BROKER_TOKENS = pathlib.Path.home() / ".local/share/ajit-segment-bots/broker_tokens"
# Fetched history, kept on disk. **A past session never changes**, so refetching
# one is spending a quota to be told the same thing again -- and the quota is
# real: Upstox answered HTTP 429 partway through selecting three contracts a
# segment on 2026-09-06, and went on refusing through four backoffs because the
# day's probing had already used it up. Cached, a replay of a given day is free
# to repeat, which is what makes history usable as the default source rather
# than as a one-shot.
HISTORY_CACHE = pathlib.Path.home() / ".local/share/ajit-segment-bots/history"
MILLISECONDS_TO_NANOSECONDS = 1_000_000
# Upstox's own documented shapes for this endpoint: minute data is served one
# month per request for 1-15 minute intervals.
HISTORICAL_UNIT = "minutes"
HISTORICAL_INTERVAL = 1

# Upstox answers a burst of history requests with HTTP 429, measured 2026-09-06:
# selecting three contracts a segment across three segments refused partway
# through. Its published per-second limit for this endpoint is not something this
# project has verified against primary documentation, so the pacing here is
# deliberately conservative rather than tuned to a number nobody checked -- a
# replay that takes a minute longer is free, and one that is refused halfway
# through has fetched nothing.
SECONDS_BETWEEN_REQUESTS = 0.35
# A 429 is a request to wait, not a failure. Each retry waits longer than the
# last so a burst that overran the limit settles instead of hammering it.
RETRIES_ON_A_RATE_LIMIT = 4
# Long enough to outlast a per-minute quota rather than a per-second burst:
# 5, 15, 45, 135 seconds. A short ladder was measured refusing all four times on
# 2026-09-06, which is a wait that achieved nothing.
FIRST_BACKOFF_SECONDS = 5.0
BACKOFF_MULTIPLE = 3.0
TOO_MANY_REQUESTS = 429

_last_request_at = [0.0]


def _wait_our_turn() -> None:
    """Hold the minimum gap between requests, however many callers there are."""
    since = time.monotonic() - _last_request_at[0]
    if since < SECONDS_BETWEEN_REQUESTS:
        time.sleep(SECONDS_BETWEEN_REQUESTS - since)
    _last_request_at[0] = time.monotonic()


class NoBrokerToken(RuntimeError):
    """No Upstox access token on this machine, so no history can be fetched."""


def upstox_access_token(tokens_root: pathlib.Path | None = None) -> str:
    """The operator's own token, as the token refresher last wrote it."""
    path = (tokens_root or BROKER_TOKENS) / "upstox.json"
    try:
        return json.loads(path.read_text())["access_token"]
    except (OSError, ValueError, KeyError) as missing:
        raise NoBrokerToken(
            f"no Upstox access token at {path}. Historical prices are fetched with the "
            f"operator's own credential and there is no anonymous route to them"
        ) from missing


def cache_path_for(instrument_key: str, from_date: str, to_date: str) -> pathlib.Path:
    """Where one instrument's fetched range lives. The key carries a pipe and a
    space, so it is the directory name and never part of a filename."""
    return HISTORY_CACHE / instrument_key / f"{from_date}_{to_date}.json"


def cached_document(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def remember_document(path: pathlib.Path, document: dict) -> None:
    """Written next to the read, so a half-written file is never read as data."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        being_written = path.with_suffix(".writing")
        being_written.write_text(json.dumps(document))
        being_written.replace(path)
    except OSError:
        # A cache that cannot be written is a slower replay, never a wrong one.
        pass


def fetch_upstox_document(url: str, access_token: str, cache_path: pathlib.Path | None,
                          describing: str, timeout_seconds: float = 30.0) -> dict:
    """One Upstox REST answer, paced, retried on 429, and cached when a path is given.

    Every history route here shares this, so the pacing and the backoff ladder are
    one fact rather than a copy per endpoint that could drift apart.
    """
    if cache_path is not None:
        remembered = cached_document(cache_path)
        if remembered is not None:
            return remembered
    request = build_broker_request(url, access_token=access_token)
    backoff = FIRST_BACKOFF_SECONDS
    for attempt in range(RETRIES_ON_A_RATE_LIMIT + 1):
        _wait_our_turn()
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                document = json.loads(response.read())
            break
        except urllib.error.HTTPError as refusal:
            if refusal.code == TOO_MANY_REQUESTS and attempt < RETRIES_ON_A_RATE_LIMIT:
                time.sleep(backoff)
                backoff *= BACKOFF_MULTIPLE
                continue
            # A refusal is a fact about the request, never an empty market.
            # Returning () for both would make a rate limit read as a quiet
            # session -- which is the shape this project keeps finding.
            raise RuntimeError(
                f"Upstox refused {describing}: HTTP {refusal.code}"
                + (
                    f", still refusing after {RETRIES_ON_A_RATE_LIMIT} waits"
                    if refusal.code == TOO_MANY_REQUESTS else ""
                )
            ) from refusal
    if cache_path is not None:
        remember_document(cache_path, document)
    return document


def historical_candles(
    instrument_key: str, from_date: str, to_date: str, access_token: str,
    timeout_seconds: float = 30.0, use_cache: bool = True,
) -> tuple:
    """One instrument's one-minute bars for a date range, oldest first.

    Parsed by `UpstoxAdapter.read_historical_candles`, which is where the rows
    are reversed into time order and the +05:30 stamps are read as IST rather
    than as UTC -- both of which a caller doing its own parsing would get wrong
    in a way that looks like data rather than like a bug.
    """
    adapter = UpstoxAdapter()
    url = adapter.historical_candle_url(
        instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL,
        from_date=from_date, to_date=to_date,
    )
    document = fetch_upstox_document(
        url, access_token,
        cache_path_for(instrument_key, from_date, to_date) if use_cache else None,
        f"history for {instrument_key} {from_date}..{to_date}",
        timeout_seconds,
    )
    return adapter.read_historical_candles(
        instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL, document,
    )


# Upstox's expired-instruments routes, read from its raw documentation page on
# 2026-09-16 (a summariser gave a different, wrong path). They answer for
# contracts the ordinary history endpoint refuses with HTTP 400 once expired.
# The account needs Upstox's Plus plan; this one answered 200
# (measurements/2026-09-16-how-deep-option-history-goes/).
UPSTOX_API_ROOT = "https://api.upstox.com/v2"
EXPIRED_CANDLE_INTERVAL = "1minute"


def _quoted(text: str) -> str:
    return urllib.parse.quote(text, safe="")


def expired_expiries(underlying_key: str, access_token: str) -> tuple[str, ...]:
    """Every expiry Upstox holds expired contracts for, "YYYY-MM-DD", oldest first.

    Cached per calendar day: the list grows as contracts expire, so yesterday's
    answer is not today's.
    """
    today = time.strftime("%Y-%m-%d")
    document = fetch_upstox_document(
        f"{UPSTOX_API_ROOT}/expired-instruments/expiries?instrument_key={_quoted(underlying_key)}",
        access_token,
        HISTORY_CACHE / underlying_key / f"expired-expiries-as-of-{today}.json",
        f"expired expiries for {underlying_key}",
    )
    return tuple(sorted(document.get("data") or ()))


def expired_option_contracts(underlying_key: str, expiry: str,
                             access_token: str) -> tuple[dict, ...]:
    """Every option contract of one expired expiry, as Upstox lists it.

    Rows carry `trading_symbol`, `instrument_type`, `strike_price`, `lot_size`,
    `freeze_quantity` and an `instrument_key` of the form `NSE_FO|42650|08-09-2026`.
    A past expiry's list never changes, so it is cached for good.
    """
    document = fetch_upstox_document(
        f"{UPSTOX_API_ROOT}/expired-instruments/option/contract"
        f"?instrument_key={_quoted(underlying_key)}&expiry_date={expiry}",
        access_token,
        HISTORY_CACHE / underlying_key / f"expired-option-contracts-{expiry}.json",
        f"expired option contracts for {underlying_key} {expiry}",
    )
    return tuple(document.get("data") or ())


def expired_candles(expired_instrument_key: str, from_date: str, to_date: str,
                    access_token: str) -> tuple:
    """One expired contract's one-minute bars for a date range, oldest first.

    The route takes the dates as `/{to}/{from}`, newest first, as Upstox documents
    it. The answer has the ordinary history shape, so the same adapter parses it.
    """
    document = fetch_upstox_document(
        f"{UPSTOX_API_ROOT}/expired-instruments/historical-candle/"
        f"{_quoted(expired_instrument_key)}/{EXPIRED_CANDLE_INTERVAL}/{to_date}/{from_date}",
        access_token,
        cache_path_for(expired_instrument_key, from_date, to_date),
        f"expired history for {expired_instrument_key} {from_date}..{to_date}",
    )
    return UpstoxAdapter().read_historical_candles(
        expired_instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL, document,
    )


def prints_from_candles(candles) -> list[tuple[int, float]]:
    """Each bar as one price at its own time: (at_ns, close).

    The close rather than the open, high or low, because the close is the one
    price in a bar that is certainly a real trade at a known moment. The high and
    low happened somewhere inside the minute and nothing says when, so replaying
    them would place prices at times they did not occur -- and a stop walked
    against an invented time is an exit the market never offered.
    """
    return [
        (int(candle.bar_time_ms) * MILLISECONDS_TO_NANOSECONDS, float(candle.close))
        for candle in candles
        if candle.close and candle.bar_time_ms
    ]


def historical_prints(
    instrument_key: str, from_date: str, to_date: str, access_token: str,
) -> list[tuple[int, float]]:
    """One instrument's real prices across a past date range, oldest first."""
    return prints_from_candles(
        historical_candles(instrument_key, from_date, to_date, access_token)
    )


def prints_for_instrument(row: dict, day: str, session=None,
                          nse_session=None) -> tuple[list[tuple[int, float]], str]:
    """One instrument's prices for one session, from whichever free source has them.

    Returns the prints and **the name of the source that served them**, because a
    replay whose prices came from somewhere else than it thinks is a replay whose
    result means something else.

    The order is by cost, not by preference:

        equity, index      Yahoo         free, no account, unlimited
        option, last session  NSE intraday   free, no account, that session only
        anything else      Upstox        one minute, back to 2022, DAILY QUOTA

    Upstox is last on purpose. It is the only source that can serve an arbitrary
    past option session, and it is the only one that runs out -- so spending it on
    a price Yahoo or NSE would have given for nothing is spending the thing that
    cannot be replaced.

    Empty prints with a stated source is an honest answer: the caller knows which
    source was asked and can say so, rather than reporting a quiet market.
    """
    instrument_type = (row.get("instrument_type") or "").strip()
    trading_symbol = (row.get("trading_symbol") or "").strip()

    if instrument_type in ("EQ", "INDEX"):
        from operate import yahoo_finance_prints as yahoo

        if yahoo.ticker_for(trading_symbol, instrument_type) is not None:
            try:
                prints = yahoo.historical_prints(
                    trading_symbol, instrument_type, day, session=session,
                )
                if prints:
                    return prints, "yahoo"
            except Exception:
                # A free source that is down is a reason to try the next one,
                # never a reason to stop: the replay still has Upstox.
                pass

    if instrument_type in ("CE", "PE"):
        prints, source = option_prints_from_nse(row, day, session=nse_session)
        if prints:
            return prints, source

    token = upstox_access_token()
    return historical_prints(row["instrument_key"], day, day, token), "upstox"


def option_prints_from_nse(row: dict, day: str, session=None
                           ) -> tuple[list[tuple[int, float]], str]:
    """One option's intraday session from NSE, when NSE is serving that session.

    NSE's chart endpoint takes no date -- it answers with whatever the last
    trading day was -- so the day it served is read back from the series and
    checked. Believing it had answered the day that was asked for is how a replay
    of 2025 would quietly be a replay of last Friday.
    """
    import datetime

    from operate import nse_intraday_option_prices as nse

    expiry_ms = row.get("expiry")
    strike = row.get("strike_price")
    underlying = row.get("underlying_symbol")
    option_type = (row.get("instrument_type") or "").strip()
    if not (expiry_ms and strike and underlying):
        return [], "nse-intraday"
    expiry = datetime.datetime.fromtimestamp(expiry_ms / 1000, datetime.UTC).date()
    identifier = nse.identifier_for(
        underlying, expiry, option_type, float(strike),
        underlying_is_an_index=(row.get("segment") or "").endswith("_FO")
        and underlying in nse_index_underlyings(),
    )
    try:
        cached = nse.cache_path_for(identifier, day)
        if cached.exists():
            return nse.intraday_prints(identifier, day, session=session), "nse-intraday"
        document = nse.fetch_chart(identifier, session=session)
    except Exception:
        return [], "nse-intraday"
    if nse.the_session_this_serves(document) != day:
        # It has a session, but not the one asked for. Saying so beats replaying
        # the wrong day.
        return [], "nse-intraday"
    prints = nse.prints_from_chart(document)
    if prints:
        try:
            path = nse.cache_path_for(identifier, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(prints))
        except OSError:
            pass
    return prints, "nse-intraday"


def nse_index_underlyings() -> frozenset[str]:
    """The underlyings NSE names with OPTIDX rather than OPTSTK.

    NSE's own two prefixes, and there is no rule to derive the split from -- an
    index is not a listed security. Written down once, from NSE's own option
    chain.
    """
    return frozenset(
        {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    )


__all__ = [
    "HISTORICAL_INTERVAL",
    "nse_index_underlyings",
    "option_prints_from_nse",
    "prints_for_instrument",
    "HISTORICAL_UNIT",
    "NoBrokerToken",
    "historical_candles",
    "historical_prints",
    "prints_from_candles",
    "upstox_access_token",
]
