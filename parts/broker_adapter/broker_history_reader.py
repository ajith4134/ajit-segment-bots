"""broker-history-reader: prices for the hours the market is shut.

Phase A paper-trades on live prices while the market is open and on history
while it is not (docs/proposals/broker-history-reader.md, and the user's own
words 2026-09-02). The deciding half never learns which it is reading: the bars
go onto `candle`, the same wire the live bridge publishes to, and no part asks
where a price came from (T-4).

**It publishes `candle`, never `broker-candle`, and that is the design.**
`broker-candle` is what `broker-market-tape-writer` records as live capture.
History published there would write replayed bars into the tape, and every
measurement taken over that tape afterwards would be quietly wrong with nothing
saying so. `candle` reaches `kline-window-builder`, `historical-bar-store` and
`feed-jump-detector` -- exactly the parts that should see history -- and no
recorder of live capture. History joins the circuit *after* the tape.

**It runs only while the session is not open**, which is what keeps it on the
right side of RL-071: a replay that never stands in for a market it could be
reading is not a replay standing in for one. An unknown session is not "shut" --
a part that assumed it would fetch history straight through the opening bell.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from runtime.brokers.upstox import UPSTOX_BROKER_ID
from runtime.market_conditions import SessionKind
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.venues.venue_adapter import NormalisedCandle

PART_ID = "broker-history-reader"

PART_DECLARATION = PartDeclaration(
    part_id="broker-history-reader",
    consumes=("broker-instrument-listing", "broker-token-standing", "market-session-state"),
    produces=("candle", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

MILLISECONDS_TO_NANOSECONDS = 1_000_000
SECONDS_TO_NANOSECONDS = 1_000_000_000
MINUTES_TO_SECONDS = 60
HOURS_TO_SECONDS = 3600
DAYS_TO_SECONDS = 86400

# How long one bar of each unit lasts. Upstox states the unit and the interval
# separately, and a duration is what a close time is computed from -- the
# broker publishes no close time of its own.
UNIT_DURATION_SECONDS = {
    "minutes": MINUTES_TO_SECONDS,
    "hours": HOURS_TO_SECONDS,
    "days": DAYS_TO_SECONDS,
}


@dataclass(frozen=True)
class HistoryRequest:
    """One window of one instrument's history, as this part asks for it."""

    instrument_key: str
    unit: str
    interval: int
    from_date: str
    to_date: str

    @property
    def window_key(self) -> tuple:
        """What makes two asks the same ask."""
        return (self.instrument_key, self.unit, self.interval, self.from_date, self.to_date)


@dataclass
class HistoryReaderStanding:
    instruments_known: int = 0
    requests_planned: int = 0
    requests_refused: int = 0
    candles_published: int = 0
    windows_already_read: int = 0
    skipped_because_the_market_is_open: int = 0
    skipped_because_the_session_is_unknown: int = 0


class HistoryReader:
    """Decides what history to ask for, and turns an answer into candles."""

    def __init__(
        self,
        interval_unit: str,
        interval: int,
        most_instruments: int,
        most_days_back: int,
        wanted_instrument_types: tuple[str, ...],
    ) -> None:
        if interval_unit not in UNIT_DURATION_SECONDS:
            raise ValueError(
                f"a bar's duration is what its close time is computed from, and this "
                f"part knows the duration of {', '.join(UNIT_DURATION_SECONDS)}; "
                f"got {interval_unit!r}"
            )
        if interval < 1:
            raise ValueError(f"an interval is a count of {interval_unit}; got {interval!r}")
        if most_instruments < 1:
            raise ValueError(
                "a reader that asks about no instrument publishes nothing while looking "
                f"like a working reader; got {most_instruments!r}"
            )
        if most_days_back < 1:
            raise ValueError(f"history is at least one day deep; got {most_days_back!r}")
        self._unit = interval_unit
        self._interval = interval
        self._most_instruments = most_instruments
        self._most_days_back = most_days_back
        self._wanted_types = tuple(wanted_instrument_types)
        self._symbol_by_key: dict[str, str] = {}
        self._session: object | None = None
        self._windows_read: set[tuple] = set()
        self.standing = HistoryReaderStanding()

    def observe_listings(self, listings) -> None:
        """The instrument master, filtered to what this segment actually trades."""
        for listing in listings:
            if getattr(listing, "instrument_type", None) not in self._wanted_types:
                continue
            self._symbol_by_key[listing.instrument_key] = getattr(
                listing, "trading_symbol", ""
            )
        self.standing.instruments_known = len(self._symbol_by_key)

    def observe_session(self, session) -> None:
        self._session = session

    def _there_is_a_live_market(self) -> bool | None:
        """True, False, or None when nothing has said. None is its own answer."""
        if self._session is None:
            return None
        return self._session.kind == SessionKind.OPEN

    def requests_due(self, today: datetime.date) -> tuple[HistoryRequest, ...]:
        """The windows worth asking for right now, or none.

        None while the market is open, because the live feed is the source then.
        None while nothing has said what the session is, because absence of a
        reading is not a reading of "shut" -- that assumption would fetch
        history straight through the opening bell.
        """
        live = self._there_is_a_live_market()
        if live is None:
            self.standing.skipped_because_the_session_is_unknown += 1
            return ()
        if live:
            self.standing.skipped_because_the_market_is_open += 1
            return ()

        requests = []
        for key in sorted(self._symbol_by_key)[: self._most_instruments]:
            request = HistoryRequest(
                instrument_key=key,
                unit=self._unit,
                interval=self._interval,
                from_date=(today - datetime.timedelta(days=self._most_days_back)).isoformat(),
                to_date=today.isoformat(),
            )
            if request.window_key in self._windows_read:
                self.standing.windows_already_read += 1
                continue
            requests.append(request)
        self.standing.requests_planned += len(requests)
        return tuple(requests)

    def observe_history(self, request: HistoryRequest, response) -> tuple[NormalisedCandle, ...]:
        """One answered window, as candles carrying the moment they printed."""
        from runtime.brokers.upstox import UpstoxAdapter

        try:
            bars = UpstoxAdapter().read_historical_candles(
                request.instrument_key, request.unit, request.interval, response
            )
        except ValueError:
            # A refused window is not an empty one. Counted, and the window is
            # left unread so it can be asked for again.
            self.standing.requests_refused += 1
            return ()

        self._windows_read.add(request.window_key)
        symbol = self._symbol_by_key.get(request.instrument_key, request.instrument_key)
        duration_ns = (
            UNIT_DURATION_SECONDS[request.unit] * request.interval * SECONDS_TO_NANOSECONDS
        )
        candles = []
        for bar in bars:
            open_time_ns = bar.bar_time_ms * MILLISECONDS_TO_NANOSECONDS
            candles.append(
                NormalisedCandle(
                    venue_id=UPSTOX_BROKER_ID,
                    symbol=symbol,
                    interval=f"{request.interval}{request.unit}",
                    open_time_ns=open_time_ns,
                    close_time_ns=open_time_ns + duration_ns,
                    open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                    volume=bar.volume,
                    # Upstox states no turnover figure for a historical bar, the
                    # same as for a live one; close*volume is the documented
                    # approximation broker-candle-bridge already publishes.
                    quote_volume=bar.close * bar.volume,
                    # No trade count is stated. None, never a fabricated 0.
                    trades=None,
                    is_closed=True,
                    # The moment the bar was really printed. A replayed bar that
                    # travelled with "now" would tell every consumer that a
                    # month-old price had just arrived.
                    venue_time_ns=open_time_ns,
                )
            )
        self.standing.candles_published += len(candles)
        return tuple(candles)


def describe_history_reading(reader: HistoryReader) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_known": reader.standing.instruments_known,
        "requests_planned": reader.standing.requests_planned,
        "requests_refused": reader.standing.requests_refused,
        "candles_published": reader.standing.candles_published,
        "windows_already_read": reader.standing.windows_already_read,
        "skipped_because_the_market_is_open": reader.standing.skipped_because_the_market_is_open,
        "skipped_because_the_session_is_unknown": (
            reader.standing.skipped_because_the_session_is_unknown
        ),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.input_assembly import Batch, LatestByKey
    from runtime.brokers.upstox import UpstoxAdapter

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    tokens = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    # A level, aged: a calendar that died must not leave its last "closed"
    # standing here forever, or this part would fetch history into a live
    # session -- the one thing it must never do.
    sessions = LatestByKey(
        read=context.bus.reader("market-session-state"),
        key_of=lambda session: session.segment,
        maximum_age_seconds=context.number("market_condition_level_maximum_age_seconds"),
    )
    publish_candles = context.bus.publisher_for("candle")

    reader = HistoryReader(
        interval_unit=str(context.setting("broker_history_interval_unit").value),
        interval=int(context.number("broker_history_interval")),
        most_instruments=int(context.number("broker_history_most_instruments")),
        most_days_back=int(context.number("broker_history_most_days_back")),
        wanted_instrument_types=tuple(
            str(kind) for kind in context.setting("broker_history_instrument_types").value
        ),
    )
    adapter = UpstoxAdapter()
    fetch_interval = context.number("broker_history_fetch_interval_seconds")
    last_fetched_at: list[float | None] = [None]

    def tick() -> None:
        reader.observe_listings(listings.payloads())
        standing_sessions = sessions.values()
        reader.observe_session(standing_sessions[0] if standing_sessions else None)

        now = time.monotonic()
        if last_fetched_at[0] is not None and now - last_fetched_at[0] < fetch_interval:
            return
        held = tokens.values()
        if not held:
            return
        requests = reader.requests_due(today=datetime.date.today())
        if not requests:
            return
        last_fetched_at[0] = now
        # One window per due tick: the month-per-request window and the broker's
        # own rate limit are both real, and a burst of them buys nothing that
        # the next tick would not.
        request = requests[0]
        url = adapter.historical_candle_url(
            request.instrument_key, request.unit, request.interval,
            from_date=request.from_date, to_date=request.to_date,
        )
        candles = reader.observe_history(request, _fetch_json(url, held[0].access_token))
        if candles:
            publish_candles(candles)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_history_reading(reader),
    )


def _fetch_json(url: str, access_token: str):
    """One GET, with the same Chrome impersonation the login flow already needs.

    curl_cffi rather than urllib because Upstox sits behind a Cloudflare front
    that answered stdlib urllib with `Error 1010: browser_signature_banned`
    (2026-09-02) -- a bot-fingerprint block, not an auth failure, and it cost a
    session's diagnosis before it was recognised as one.
    """
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        impersonate="chrome",
        timeout=30,
    )
    if response.status_code != 200:
        return {"status": "error", "http_status": response.status_code}
    return response.json()


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "HistoryReader",
    "HistoryRequest",
    "HistoryReaderStanding",
    "describe_history_reading",
    "start_part",
]
