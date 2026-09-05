"""intraday-square-off-placer: an intraday segment is flat before the session ends.

Phase A's third bot trades cash equity intraday on the broker's leverage, and an
intraday (MIS) position is not something this system may simply leave open. The
broker squares it off itself, from around fifteen minutes before the close, at
whatever the book offers -- or converts it to delivery with a margin call behind
it. Either way the exit is one this system did not choose, did not price, and
cannot learn from: `closed-trade` would carry a fill nobody here decided.

**This is not `pre-expiry-position-closer` with a different trigger.** That part
closes a *contract that is about to stop existing*, and only that contract; this
one closes *everything this segment holds*, because the segment itself cannot
carry a position overnight. The two answer different questions -- "is this
instrument expiring?" versus "may this segment hold anything at all past
today?" -- and they must stay different in the journal, because what a record
says about why a trade ended is what every learner here trains on.

**Whether a segment is intraday is the segment's own statement**, read from
`positions_are_squared_off_daily` in its settings file. It is true for
cash-equity-intraday and absent for both options segments, which hold a bought
contract to its own expiry. A part that decided this from the segment's *name*
would be guessing, and a new segment added later would inherit the wrong answer
in silence.

**It must finish before the broker starts.** The window is deliberately wider
than the expiry closer's: this part has to place, fill and be done before the
broker's own square-off begins, so the number is set against that deadline and
not against the session close.
"""

from __future__ import annotations

import datetime
import time

from runtime.part_declaration import PartDeclaration
from runtime.position_exit_placer import PositionExitPlacer

PART_ID = "intraday-square-off-placer"

PART_DECLARATION = PartDeclaration(
    part_id="intraday-square-off-placer",
    consumes=("position", "market-session-state", "money-mode"),
    produces=("order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    # `corrupts`, for the same reason as pre-expiry-position-closer: a tick
    # skipped inside the window is a position the broker squares off instead,
    # and no later tick undoes that.
    skipped_tick_effect="corrupts",
)

# Why a position was closed, in the words the journal keeps. Never merged with
# the flattener's or the expiry closer's: three different reasons, three
# different lessons.
SEGMENT_IS_INTRADAY = "segment-is-intraday"


class IntradaySquareOffPlacer:
    """Closes everything an intraday segment holds, before the session ends."""

    def __init__(
        self,
        segments_squared_off_daily: frozenset,
        minutes_before_the_close: float,
        session_closes_at: datetime.time,
        timezone: datetime.tzinfo,
        repeat_after_seconds: float,
        quantity_increment: float,
        now_ns=time.time_ns,
    ) -> None:
        if minutes_before_the_close <= 0:
            raise ValueError(
                "squaring off at the session's close is not squaring off before it, and "
                "the broker's own square-off starts earlier still; got "
                f"{minutes_before_the_close!r} minutes"
            )
        # Which segments may not hold overnight, rather than whether this spine's
        # one segment may (2026-09-05). Three segment bots run here and only cash
        # equity intraday squares off: a boolean read from the spine's segment_id
        # closed either everything or nothing, and with segment_id on
        # index-options it was nothing -- the part ran, reported healthy, and the
        # broker would have squared off bot 3's positions itself.
        self._segments_squared_off_daily = frozenset(segments_squared_off_daily)
        self._minutes_before = minutes_before_the_close
        self._session_closes_at = session_closes_at
        self._timezone = timezone
        self._now_ns = now_ns
        self._placer = PositionExitPlacer(
            repeat_after_seconds=repeat_after_seconds,
            quantity_increment=quantity_increment,
            client_order_prefix="square-off",
            now_ns=now_ns,
        )
        self._session_date: datetime.date | None = None
        self.ticks_on_a_segment_that_may_hold = 0
        self.positions_on_a_segment_that_may_hold = 0
        self.ticks_before_the_window = 0
        self.refused_no_session = 0
        self.session_readings_too_old = 0
        self.squared_off = 0

    # ---- what it is told ---------------------------------------------------

    def observe_session(self, session) -> None:
        if session is None:
            return
        self._session_date = getattr(session, "as_of_date", None)

    def forget_the_session(self) -> None:
        """Stop believing a session reading nobody is restating.

        A calendar that died must not leave this part squaring off against a
        stale date -- which, on the wrong day, is either an unnecessary exit or
        no exit at all on the day one was needed.
        """
        if self._session_date is not None:
            self.session_readings_too_old += 1
        self._session_date = None

    def observe_money_mode(self, mode, segment: str = "") -> None:
        self._placer.observe_money_mode(mode, segment)

    def observe_position(self, position) -> None:
        """Only what a segment that squares off daily holds.

        A position in a segment that may hold overnight is not this part's, and
        observing it would put an option position on the list to be closed every
        afternoon.
        """
        if getattr(position, "segment", "") not in self._segments_squared_off_daily:
            self.positions_on_a_segment_that_may_hold += 1
            return
        self._placer.observe_position(position)

    # ---- what it decides ---------------------------------------------------

    @property
    def is_intraday(self) -> bool:
        """Whether any segment on this spine squares off daily at all."""
        return bool(self._segments_squared_off_daily)

    def is_inside_the_square_off_window(self, at_ns: int | None = None) -> bool:
        if self._session_date is None:
            return False
        at = self._now_ns() if at_ns is None else at_ns
        local = datetime.datetime.fromtimestamp(at / 1e9, tz=self._timezone)
        closes_at = datetime.datetime.combine(
            self._session_date, self._session_closes_at, tzinfo=self._timezone,
        )
        return local >= closes_at - datetime.timedelta(minutes=self._minutes_before)

    def exits_to_place(self, at_ns: int | None = None) -> tuple:
        """Everything still held, once the window opens. Otherwise nothing.

        The three ways of doing nothing are counted apart, because on a board
        they must not look the same: this segment may hold overnight, the window
        has not opened, and there is no session to judge against.
        """
        if not self._segments_squared_off_daily:
            self.ticks_on_a_segment_that_may_hold += 1
            return ()
        if self._session_date is None:
            self.refused_no_session += 1
            return ()
        if not self.is_inside_the_square_off_window(at_ns):
            self.ticks_before_the_window += 1
            return ()

        def why(held, quantity, repeated):
            again = " again" if repeated else ""
            return (
                f"this segment is squared off daily and the session closes in "
                f"{self._minutes_before:g} minutes or less; closing {held.direction} "
                f"{quantity:g} of {abs(held.quantity):g} {held.symbol} at market{again} "
                f"rather than letting the broker square it off ({SEGMENT_IS_INTRADAY})"
            )

        exits = self._placer.exits_for(self._placer.held.keys(), reason_for=why)
        self.squared_off += len(exits)
        return exits

    @property
    def placer(self) -> PositionExitPlacer:
        return self._placer


def describe_squaring_off(placer: IntradaySquareOffPlacer) -> dict:
    """What is held and whether this segment is even allowed to hold it.

    `is_intraday` reading 0 with every other counter still is the correct board
    for a segment that carries positions overnight -- it is not a part that has
    failed to run (Rule 8).
    """
    standing = placer.placer.standing
    return {
        "part_id": PART_ID,
        "is_intraday": 1.0 if placer.is_intraday else 0.0,
        "open_positions": standing.open_positions,
        "squared_off": placer.squared_off,
        "ticks_on_a_segment_that_may_hold": placer.ticks_on_a_segment_that_may_hold,
        "ticks_before_the_window": placer.ticks_before_the_window,
        "refused_no_session": placer.refused_no_session,
        "session_readings_too_old": placer.session_readings_too_old,
        "exits_placed": standing.exits_placed,
        "exits_repeated": standing.exits_repeated,
        "waiting_for_a_fill": standing.waiting_for_a_fill,
        "positions_at_the_cap": standing.positions_at_the_cap,
        "quantity_asked_beyond_the_position": standing.quantity_asked_beyond_the_position,
        "refused_no_money_mode": standing.refused_no_money_mode,
        "longest_wait_seconds": standing.longest_wait_seconds,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import zoneinfo

    from runtime.input_assembly import Batch, LatestByKey, LatestValue
    from runtime.market_conditions import EXCHANGE_TIMEZONE, read_clock_time
    from runtime.part_process import run_part
    from runtime.segment_settings import (
        SegmentSettingMissing, built_segments, read_segment_setting,
    )

    def _the_segment_squares_off_daily(segment: str) -> bool:
        try:
            return bool(
                read_segment_setting(segment, "positions_are_squared_off_daily").value
            )
        except (SegmentSettingMissing, OSError, ValueError):
            return False

    # Every segment this spine trades that may not hold overnight (2026-09-05).
    # A segment that says nothing holds its positions: the options segments say
    # nothing and hold a bought contract to its own expiry, so silence has to
    # mean "may hold" -- squaring those off daily would close every option
    # position every afternoon.
    segments_squared_off_daily = frozenset(
        segment
        for segment in built_segments(context)
        if _the_segment_squares_off_daily(segment)
    )

    positions = Batch(read=context.bus.reader("position"))
    session = LatestValue(read=context.bus.reader("market-session-state"))
    session_maximum_age_ns = int(
        context.number("market_session_reading_maximum_age_seconds") * 1_000_000_000
    )
    money_modes = LatestByKey(
        read=context.bus.reader("money-mode"),
        key_of=lambda mode: mode.segment,
        maximum_age_seconds=context.number("money_mode_maximum_age_seconds"),
    )
    publish_orders = context.bus.publisher_for("order-request")

    placer = IntradaySquareOffPlacer(
        segments_squared_off_daily=segments_squared_off_daily,
        minutes_before_the_close=context.number(
            "intraday_square_off_minutes_before_the_session_closes"
        ),
        session_closes_at=read_clock_time(
            str(context.setting("market_session_closes_at_ist").value)
        ),
        timezone=zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE),
        repeat_after_seconds=context.number("order_latency_maximum"),
        quantity_increment=context.number("order_quantity_increment"),
    )

    def tick() -> None:
        for position in positions.payloads():
            placer.observe_position(position)
        current_session = session.value()
        observed_at_ns = session.observed_at_ns
        if current_session is None or observed_at_ns is None:
            pass
        elif time.time_ns() - observed_at_ns > session_maximum_age_ns:
            placer.forget_the_session()
        else:
            placer.observe_session(current_session)
        for segment, mode in money_modes.mapping().items():
            placer.observe_money_mode(getattr(mode, "mode", None), segment)

        exits = placer.exits_to_place()
        if exits:
            publish_orders(exits)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_squaring_off(placer),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "SEGMENT_IS_INTRADAY",
    "IntradaySquareOffPlacer",
    "describe_squaring_off",
    "start_part",
]
