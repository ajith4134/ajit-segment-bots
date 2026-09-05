"""market-session-calendar: state which trading session the market is in now.

Two inputs, one fetched and one from the clock: NSE's own holiday list
(www.nseindia.com/api/holiday-master?type=trading, verified live 2026-09-02 --
keyed by segment, twelve of them, each row carrying tradingDate/description),
and the published session hours from settings.

**Closed and holiday are different facts.** Closed ends at the next open;
holiday is the exchange not trading that day at all. A consumer deciding
whether to wait or to stand down for the day needs to know which -- so the
holiday outranks the clock, and 20:00 on Republic Day reads as HOLIDAY rather
than as "after the close".

**An empty holiday list reports CLOSED, never OPEN** (Rule 8). Absence of
evidence is its own state, and the alternative is a bot that trades into a
holiday because a fetch had not happened yet -- the display failure this project
already refuses, one layer down in the thing being displayed.

Every moment is read in IST regardless of the clock it arrives on. The spine
runs on a UTC machine, and 04:45 UTC is 10:15 IST: read as UTC, the market
would be closed for its entire morning.
"""

from __future__ import annotations

import datetime

from runtime.market_conditions import (
    EXCHANGE_TIMEZONE, MarketSessionState, SessionKind, read_clock_time, read_nse_date,
)
from runtime.nse_public_data import NSE_API_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "market-session-calendar"

PART_DECLARATION = PartDeclaration(
    # Written out rather than PART_ID: the part monitor reads this declaration
    # statically, without importing the module, and refuses a computed value --
    # so a part naming itself by reference reads as having no verifiable wiring
    # and its whole block paints red while the part runs (2026-09-02).
    part_id="market-session-calendar",
    consumes=(),
    produces=("market-session-state", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HOLIDAY_URL = f"{NSE_API_HOST}/api/holiday-master?type=trading"
SATURDAY = 5
NO_HOLIDAY_LIST = "no holiday list has been read yet"
NANOSECONDS_PER_SECOND = 1_000_000_000


class MarketSessionCalendar:
    """Which session one segment is in, from its holidays plus its hours."""

    def __init__(self, segment: str, opens_at, closes_at, timezone) -> None:
        self._segment = segment
        self._opens_at = opens_at
        self._closes_at = closes_at
        self._timezone = timezone
        self._holiday_reason_by_date: dict[datetime.date, str] = {}
        self._has_a_holiday_list = False

    def observe_holidays(self, document) -> None:
        rows = document.get(self._segment, [])
        self._holiday_reason_by_date = {
            read_nse_date(row["tradingDate"]): row.get("description", "")
            for row in rows
        }
        self._has_a_holiday_list = True

    def session_at(self, moment: datetime.datetime) -> MarketSessionState:
        local = moment.astimezone(self._timezone)
        day = local.date()
        observed_at_ns = int(moment.timestamp() * NANOSECONDS_PER_SECOND)
        if not self._has_a_holiday_list:
            return self._state(SessionKind.CLOSED, day, NO_HOLIDAY_LIST, observed_at_ns)
        holiday_reason = self._holiday_reason_by_date.get(day)
        if holiday_reason is not None:
            return self._state(SessionKind.HOLIDAY, day, holiday_reason, observed_at_ns)
        if local.weekday() >= SATURDAY:
            return self._state(SessionKind.CLOSED, day, "weekend", observed_at_ns)
        if self._opens_at <= local.time() < self._closes_at:
            return self._state(
                SessionKind.OPEN, day, "within stated session hours", observed_at_ns
            )
        return self._state(
            SessionKind.CLOSED, day, "outside stated session hours", observed_at_ns
        )

    def _state(self, kind, day, reason, observed_at_ns) -> MarketSessionState:
        return MarketSessionState(
            segment=self._segment, kind=kind, as_of_date=day, reason=reason,
            observed_at_ns=observed_at_ns,
        )

    @property
    def holidays_known(self) -> int:
        return len(self._holiday_reason_by_date)


def describe_calendar(calendar: MarketSessionCalendar, moment) -> dict:
    state = calendar.session_at(moment)
    return {
        "part_id": PART_ID,
        "segment": state.segment,
        "session": str(state.kind),
        "reason": state.reason,
        "holidays_known": calendar.holidays_known,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time
    import zoneinfo

    from runtime.level_publishing import LevelPublisher, without_observation_time
    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    timezone = zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
    calendar = MarketSessionCalendar(
        segment=str(context.setting("market_session_segment").value),
        opens_at=read_clock_time(str(context.setting("market_session_opens_at_ist").value)),
        closes_at=read_clock_time(str(context.setting("market_session_closes_at_ist").value)),
        timezone=timezone,
    )
    publisher = LevelPublisher(
        publish=context.bus.publisher_for("market-session-state"),
        refresh_interval_seconds=context.number(
            "instrument_restriction_refresh_interval_seconds"
        ),
        identity_of=without_observation_time,
    )
    poll_seconds = context.number("market_session_poll_seconds")
    last_polled_at: list[float | None] = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is None or now - last_polled_at[0] >= poll_seconds:
            last_polled_at[0] = now
            calendar.observe_holidays(nse.read_json(HOLIDAY_URL))
        publisher.publish_level(
            (calendar.session_at(datetime.datetime.now(tz=timezone)),)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_calendar(
            calendar, datetime.datetime.now(tz=timezone)
        ) | nse.standing(),
    )


__all__ = [
    "EXCHANGE_TIMEZONE",
    "HOLIDAY_URL",
    "MarketSessionCalendar",
    "NO_HOLIDAY_LIST",
    "PART_DECLARATION",
    "PART_ID",
    "describe_calendar",
    "read_clock_time",
    "start_part",
]
