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
    path = cache_path_for(instrument_key, from_date, to_date)
    if use_cache:
        remembered = cached_document(path)
        if remembered is not None:
            return adapter.read_historical_candles(
                instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL, remembered,
            )
    url = adapter.historical_candle_url(
        instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL,
        from_date=from_date, to_date=to_date,
    )
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
                f"Upstox refused history for {instrument_key} "
                f"{from_date}..{to_date}: HTTP {refusal.code}"
                + (
                    f", still refusing after {RETRIES_ON_A_RATE_LIMIT} waits"
                    if refusal.code == TOO_MANY_REQUESTS else ""
                )
            ) from refusal
    if use_cache:
        remember_document(path, document)
    return adapter.read_historical_candles(
        instrument_key, HISTORICAL_UNIT, HISTORICAL_INTERVAL, document,
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


__all__ = [
    "HISTORICAL_INTERVAL",
    "HISTORICAL_UNIT",
    "NoBrokerToken",
    "historical_candles",
    "historical_prints",
    "prints_from_candles",
    "upstox_access_token",
]
