"""An intraday segment is flat before the session ends, and no other segment is.

The failure this prevents is not a bad price, it is an exit this system did not
make: an MIS position left open is squared off by the broker at whatever the
book offers, and that fill reaches `closed-trade` as a decision nobody here took.

The most important test in this file is the one where nothing happens. Both
options segments hold a bought contract to its own expiry, and a square-off that
read "intraday" from anything other than the segment's own statement would close
every option position every afternoon.
"""

import datetime
import zoneinfo

import pytest

from parts.risk_capital_allocation.intraday_square_off_placer import (
    SEGMENT_IS_INTRADAY, IntradaySquareOffPlacer, describe_squaring_off,
)
from runtime.market_conditions import EXCHANGE_TIMEZONE
from runtime.trading_types import Position

IST = zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
CLOSES_AT = datetime.time(15, 30)
A_TRADING_DAY = datetime.date(2026, 9, 7)


def at(hour, minute):
    return int(
        datetime.datetime(
            A_TRADING_DAY.year, A_TRADING_DAY.month, A_TRADING_DAY.day,
            hour, minute, tzinfo=IST,
        ).timestamp() * 1e9
    )


class _Clock:
    def __init__(self, at_ns):
        self.at_ns = at_ns

    def __call__(self):
        return self.at_ns

    def advance_seconds(self, seconds):
        self.at_ns += int(seconds * 1e9)


class _Session:
    def __init__(self, as_of_date=A_TRADING_DAY):
        self.as_of_date = as_of_date
        self.is_tradeable = True


def a_position(symbol="RELIANCE", quantity=100.0):
    return Position(
        venue_id="upstox", symbol=symbol, quantity=quantity,
        average_entry_price=1402.5, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=at(10, 0), updated_at_ns=at(10, 0),
    )


def a_placer(clock, intraday=True, minutes_before=25.0):
    return IntradaySquareOffPlacer(
        segment_is_intraday=intraday,
        minutes_before_the_close=minutes_before,
        session_closes_at=CLOSES_AT,
        timezone=IST,
        repeat_after_seconds=5.0,
        quantity_increment=1.0,
        now_ns=clock,
    )


def ready(placer):
    placer.observe_money_mode("paper")
    placer.observe_session(_Session())
    placer.observe_position(a_position())
    return placer


# ---- the segment that may hold ----------------------------------------------


def test_a_segment_that_may_hold_overnight_is_never_squared_off():
    """Both options segments hold a bought contract to its own expiry. This is
    the test that stops this part closing every option position each afternoon."""
    clock = _Clock(at(15, 29))
    placer = ready(a_placer(clock, intraday=False))

    assert placer.exits_to_place() == ()
    assert placer.ticks_on_a_segment_that_may_hold == 1
    assert placer.squared_off == 0


def test_a_segment_that_may_hold_reads_as_that_and_not_as_a_part_that_failed():
    """Rule 8: is_intraday 0 with every other counter still is the correct board
    for a segment carrying positions overnight."""
    reported = describe_squaring_off(a_placer(_Clock(at(11, 0)), intraday=False))

    assert reported["is_intraday"] == 0.0
    assert reported["squared_off"] == 0
    assert reported["exits_placed"] == 0


# ---- the intraday segment ----------------------------------------------------


def test_nothing_happens_before_the_window_opens():
    clock = _Clock(at(14, 59))
    placer = ready(a_placer(clock))

    assert placer.exits_to_place() == ()
    assert placer.ticks_before_the_window == 1


def test_everything_held_is_closed_once_the_window_opens():
    clock = _Clock(at(15, 5))
    placer = ready(a_placer(clock))
    placer.observe_position(a_position(symbol="TCS", quantity=50.0))

    exits = placer.exits_to_place()

    assert {order.symbol for order in exits} == {"RELIANCE", "TCS"}
    assert all(order.order_type == "market" for order in exits)
    assert all(order.client_order_id.startswith("square-off-") for order in exits)
    assert placer.squared_off == 2


def test_the_window_opens_ten_minutes_before_the_broker_would_act():
    """25 minutes against a 15:30 close opens at 15:05, and the broker's own
    square-off begins around 15:15 -- the exit must be finished, not started,
    by then."""
    placer = a_placer(_Clock(at(15, 4)))
    placer.observe_session(_Session())
    assert placer.is_inside_the_square_off_window(at(15, 4)) is False
    assert placer.is_inside_the_square_off_window(at(15, 5)) is True
    assert placer.is_inside_the_square_off_window(at(15, 14)) is True


def test_the_reason_says_the_segment_is_intraday_and_nothing_else():
    """Three parts can close a position for three different reasons, and a
    learner reading the journal must be able to tell them apart."""
    clock = _Clock(at(15, 10))
    placer = ready(a_placer(clock))

    reason = placer.exits_to_place()[0].reason

    assert SEGMENT_IS_INTRADAY in reason
    assert "expires today" not in reason
    assert "human" not in reason


def test_a_short_position_is_bought_back():
    clock = _Clock(at(15, 10))
    placer = a_placer(clock)
    placer.observe_money_mode("paper")
    placer.observe_session(_Session())
    placer.observe_position(a_position(quantity=-100.0))

    assert placer.exits_to_place()[0].side == "buy"


# ---- what it refuses to guess ------------------------------------------------


def test_nothing_happens_without_a_session():
    clock = _Clock(at(15, 10))
    placer = a_placer(clock)
    placer.observe_money_mode("paper")
    placer.observe_position(a_position())

    assert placer.exits_to_place() == ()
    assert placer.refused_no_session == 1


def test_a_stale_session_stops_being_believed():
    clock = _Clock(at(15, 10))
    placer = ready(a_placer(clock))
    placer.forget_the_session()

    assert placer.exits_to_place() == ()
    assert placer.session_readings_too_old == 1
    assert placer.refused_no_session == 1


def test_where_an_exit_is_sent_is_never_guessed():
    clock = _Clock(at(15, 10))
    placer = a_placer(clock)
    placer.observe_session(_Session())
    placer.observe_position(a_position())

    assert placer.exits_to_place() == ()
    assert placer.placer.standing.refused_no_money_mode >= 1


def test_a_window_of_zero_minutes_is_refused():
    with pytest.raises(ValueError) as refusal:
        a_placer(_Clock(at(15, 10)), minutes_before=0.0)
    assert "broker" in str(refusal.value)


# ---- it must not oversell ----------------------------------------------------


def test_an_unanswered_square_off_is_never_asked_for_twice_over():
    """The 2026-08-30 bound, inherited from the shared placer."""
    clock = _Clock(at(15, 10))
    placer = ready(a_placer(clock))

    first = placer.exits_to_place()
    clock.advance_seconds(60.0)
    placer.observe_position(a_position())  # still open, nothing filled
    again = placer.exits_to_place()

    assert len(first) == 1
    assert again == ()
    assert placer.placer.standing.positions_at_the_cap == 1
    assert placer.placer.standing.quantity_asked_beyond_the_position == 0.0


def test_a_partly_filled_square_off_asks_only_for_what_is_left():
    clock = _Clock(at(15, 10))
    placer = ready(a_placer(clock))
    assert placer.exits_to_place()[0].quantity == 100.0

    clock.advance_seconds(60.0)
    placer.observe_position(a_position(quantity=40.0))  # 60 filled
    placer.observe_position(a_position(quantity=40.0))

    assert placer.exits_to_place() == ()
    assert placer.placer.standing.quantity_asked_beyond_the_position == 0.0
