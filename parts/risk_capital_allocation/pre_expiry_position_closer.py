"""pre-expiry-position-closer: nothing is still held when a contract stops existing.

The options spec decided this on 2026-09-01, section 2: "Force-close before
expiry, a settings-driven buffer of days rather than held to exercise -- avoids
modelling ITM auto-exercise/assignment in Phase A. Exact buffer is a number to
justify with real data at implementation time (RL-061), not decided here."
Nothing implemented it. Measured 2026-09-05: no part in the tree reasons about
closing a position before its contract expires, and `position-flattener` -- the
only part that places an exit on its own initiative -- acts only on a human's
`close-positions`.

**Stock options are why this cannot wait.** Indian single-stock options are
physically settled: a bought call held through expiry does not quietly become
cash, it becomes a delivery obligation for strike x lot size. A NIFTY option
that expires in the money is cash-settled and merely mis-modelled; a RELIANCE
one is an obligation several hundred times the premium that was paid for it.
Index options are the milder case of the same rule, and both are covered here
rather than in two places.

**Minutes before the close, not a buffer of whole days.** A day-scale buffer was
the spec's own sketch and it is the wrong shape for this project, because
`expiry-day-zero-to-hero-detector` exists precisely to trade the expiry-day move
-- a rule that refused to hold a contract on its expiry day would forbid the one
detector written for that day. So the bound is intraday: a position in a
contract expiring today is closed a settings-named number of minutes before the
session closes, which lets the day be traded and leaves nothing outstanding when
settlement happens.

**A position whose contract cannot be identified is reported, never assumed
safe.** The expiry comes from `broker-subscribed-instrument-listing`, keyed by
both `instrument_key` and `trading_symbol` because a position's `symbol` is one
or the other depending on which part opened it, and a contract this part cannot
resolve is counted in `positions_with_no_listing` rather than silently treated
as having no expiry. Absence of evidence is its own state (Rule 8): "this is not
an option" and "I could not find out what this is" must not read the same.
"""

from __future__ import annotations

import datetime
import time

from runtime.part_declaration import PartDeclaration
from runtime.position_exit_placer import PositionExitPlacer

PART_ID = "pre-expiry-position-closer"

PART_DECLARATION = PartDeclaration(
    part_id="pre-expiry-position-closer",
    consumes=(
        "position", "broker-subscribed-instrument-listing", "market-session-state",
        "money-mode",
    ),
    produces=("order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Why a position was closed, in the words the journal keeps. Never merged with
# the flattener's reason: what a record says about why a trade ended is the raw
# material every learner here trains on, and "a human said close" and "the
# contract was about to expire" are different lessons.
EXPIRES_TODAY = "expires-today"


class PreExpiryPositionCloser:
    """Closes what is held in contracts that expire before the day is out."""

    def __init__(
        self,
        minutes_before_the_close: float,
        session_closes_at: datetime.time,
        timezone: datetime.tzinfo,
        repeat_after_seconds: float,
        quantity_increment: float,
        now_ns=time.time_ns,
    ) -> None:
        if minutes_before_the_close <= 0:
            raise ValueError(
                "closing at the session's close is not closing before it -- an exit needs "
                f"time to be filled; got {minutes_before_the_close!r} minutes"
            )
        self._minutes_before = minutes_before_the_close
        self._session_closes_at = session_closes_at
        self._timezone = timezone
        self._now_ns = now_ns
        self._placer = PositionExitPlacer(
            repeat_after_seconds=repeat_after_seconds,
            quantity_increment=quantity_increment,
            client_order_prefix="pre-expiry",
            now_ns=now_ns,
        )
        # Expiry per contract, learned from the listings, under both names a
        # position's `symbol` might carry.
        self._expiry_date_by_name: dict[str, datetime.date] = {}
        self._is_an_option: dict[str, bool] = {}
        self._session_is_open: bool | None = None
        self._session_date: datetime.date | None = None
        self.listings_seen = 0
        self.positions_with_no_listing = 0
        self.positions_that_are_not_options = 0
        self.positions_expiring_today = 0
        self.closed_because_of_expiry = 0
        self.ticks_before_the_window = 0
        self.refused_no_session = 0
        # Session readings this part stopped believing because nobody had
        # restated them. Counted apart from refused_no_session: never told and
        # told so long ago it cannot be trusted are different faults, and the
        # second one means market-session-calendar has died while this part
        # goes on holding positions it can no longer judge.
        self.session_readings_too_old = 0

    # ---- what it is told ---------------------------------------------------

    def observe_listing(self, listing) -> None:
        """One contract. Indexed under both names, because a position's symbol
        is an instrument_key from some parts and a trading_symbol from others."""
        self.listings_seen += 1
        expiry_ms = getattr(listing, "expiry_ms", None)
        names = [
            name for name in (
                getattr(listing, "instrument_key", None),
                getattr(listing, "trading_symbol", None),
            ) if name
        ]
        if expiry_ms is None:
            # An index, an equity, a future without an expiry in the master --
            # recorded as known-and-not-an-option, which is a different fact
            # from never having been seen.
            for name in names:
                self._is_an_option[name] = False
            return
        expiry = datetime.datetime.fromtimestamp(
            expiry_ms / 1000, tz=datetime.timezone.utc
        ).astimezone(self._timezone).date()
        for name in names:
            self._is_an_option[name] = True
            self._expiry_date_by_name[name] = expiry

    def observe_session(self, session) -> None:
        """Whether the market is open, and which date the session belongs to.

        The date comes from the session rather than from this machine's clock:
        a session's own `as_of_date` is what decided the market is open at all,
        and computing expiry-day against a different date than the calendar used
        is how a position gets closed a day early or not at all.
        """
        if session is None:
            return
        self._session_is_open = bool(getattr(session, "is_tradeable", False))
        self._session_date = getattr(session, "as_of_date", None)

    def forget_the_session(self) -> None:
        """Stop believing a session reading nobody is restating.

        A calendar that died on Friday must not still be asserting Friday's
        session on Monday: closing against the wrong date is closing a day
        early, or not at all. Held apart from never having been told, because
        the two need different fixes.
        """
        if self._session_date is not None:
            self.session_readings_too_old += 1
        self._session_date = None
        self._session_is_open = None

    def observe_money_mode(self, mode) -> None:
        self._placer.observe_money_mode(mode)

    def observe_position(self, position) -> None:
        self._placer.observe_position(position)

    # ---- what it decides ---------------------------------------------------

    def is_inside_the_closing_window(self, at_ns: int | None = None) -> bool:
        """Whether it is late enough in an expiry day to be closing out."""
        if self._session_date is None:
            return False
        at = self._now_ns() if at_ns is None else at_ns
        local = datetime.datetime.fromtimestamp(at / 1e9, tz=self._timezone)
        closes_at = datetime.datetime.combine(
            self._session_date, self._session_closes_at, tzinfo=self._timezone,
        )
        opens_the_window_at = closes_at - datetime.timedelta(
            minutes=self._minutes_before
        )
        return local >= opens_the_window_at

    def positions_due_to_close(self, at_ns: int | None = None) -> tuple:
        """The keys of every open position whose contract expires today.

        Counts, on every call, the two ways a position cannot be judged: no
        listing for its symbol at all, and a listing that is not an option.
        Both are reported rather than assumed harmless.
        """
        if self._session_date is None:
            self.refused_no_session += 1
            return ()
        due = []
        no_listing = 0
        not_options = 0
        for key, held in self._placer.held.items():
            name = held.symbol
            if name not in self._is_an_option:
                no_listing += 1
                continue
            if not self._is_an_option[name]:
                not_options += 1
                continue
            if self._expiry_date_by_name.get(name) == self._session_date:
                due.append(key)
        self.positions_with_no_listing = no_listing
        self.positions_that_are_not_options = not_options
        self.positions_expiring_today = len(due)
        return tuple(due)

    def exits_to_place(self, at_ns: int | None = None) -> tuple:
        """The exits owed right now, or nothing at all.

        Nothing at all is the answer on almost every tick, and the three ways it
        can be are counted apart: outside the window, nothing expiring, and
        everything expiring already asked for.
        """
        due = self.positions_due_to_close(at_ns)
        if not self.is_inside_the_closing_window(at_ns):
            self.ticks_before_the_window += 1
            return ()
        if not due:
            return ()

        def why(held, quantity, repeated):
            again = " again" if repeated else ""
            return (
                f"the contract {held.symbol} expires today and the session closes in "
                f"{self._minutes_before:g} minutes or less; closing {held.direction} "
                f"{quantity:g} of {abs(held.quantity):g} at market{again} rather than "
                f"holding it to settlement ({EXPIRES_TODAY})"
            )

        exits = self._placer.exits_for(due, reason_for=why)
        self.closed_because_of_expiry += len(exits)
        return exits

    @property
    def placer(self) -> PositionExitPlacer:
        return self._placer


def describe_closing(closer: PreExpiryPositionCloser) -> dict:
    """What is held, what expires today, and what could not be judged.

    `positions_with_no_listing` is the one to watch: it is the count of open
    positions this part cannot say anything about, and a position it cannot
    judge is a position it cannot protect.
    """
    placer = closer.placer.standing
    return {
        "part_id": PART_ID,
        "listings_seen": closer.listings_seen,
        "open_positions": placer.open_positions,
        "positions_expiring_today": closer.positions_expiring_today,
        "positions_with_no_listing": closer.positions_with_no_listing,
        "positions_that_are_not_options": closer.positions_that_are_not_options,
        "ticks_before_the_window": closer.ticks_before_the_window,
        "refused_no_session": closer.refused_no_session,
        "session_readings_too_old": closer.session_readings_too_old,
        "closed_because_of_expiry": closer.closed_because_of_expiry,
        "exits_placed": placer.exits_placed,
        "exits_repeated": placer.exits_repeated,
        "waiting_for_a_fill": placer.waiting_for_a_fill,
        "positions_at_the_cap": placer.positions_at_the_cap,
        "quantity_asked_beyond_the_position": placer.quantity_asked_beyond_the_position,
        "refused_no_money_mode": placer.refused_no_money_mode,
        "longest_wait_seconds": placer.longest_wait_seconds,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import zoneinfo

    from runtime.input_assembly import Batch, LatestValue
    from runtime.market_conditions import EXCHANGE_TIMEZONE, read_clock_time
    from runtime.part_process import run_part

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    positions = Batch(read=context.bus.reader("position"))
    # LatestValue carries no age bound of its own -- it exposes when its value
    # was published and leaves the judgement to the reader -- so the bound is
    # applied here rather than left off, which is the unbounded-level trap this
    # project has paid for repeatedly.
    session = LatestValue(read=context.bus.reader("market-session-state"))
    session_maximum_age_ns = int(
        context.number("market_session_reading_maximum_age_seconds") * 1_000_000_000
    )
    money_mode = LatestValue(read=context.bus.reader("money-mode"))
    publish_orders = context.bus.publisher_for("order-request")

    closer = PreExpiryPositionCloser(
        minutes_before_the_close=context.number(
            "close_out_minutes_before_the_expiry_session_closes"
        ),
        # The same named settings market-session-calendar reads, so the moment
        # this part calls the close and the moment that part calls the session
        # over can never drift apart.
        session_closes_at=read_clock_time(
            str(context.setting("market_session_closes_at_ist").value)
        ),
        timezone=zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE),
        repeat_after_seconds=context.number("order_latency_maximum"),
        quantity_increment=context.number("order_quantity_increment"),
    )

    def tick() -> None:
        for listing in listings.payloads():
            closer.observe_listing(listing)
        for position in positions.payloads():
            closer.observe_position(position)
        # `value()` is what drains this assembly; there is no separate take-in.
        current_session = session.value()
        observed_at_ns = session.observed_at_ns
        if current_session is None or observed_at_ns is None:
            pass
        elif time.time_ns() - observed_at_ns > session_maximum_age_ns:
            closer.forget_the_session()
        else:
            closer.observe_session(current_session)
        mode = money_mode.value()
        closer.observe_money_mode(getattr(mode, "mode", None) if mode else None)

        exits = closer.exits_to_place()
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
        read_standing=lambda: describe_closing(closer),
    )


__all__ = [
    "EXPIRES_TODAY",
    "PART_DECLARATION",
    "PART_ID",
    "PreExpiryPositionCloser",
    "describe_closing",
    "start_part",
]
