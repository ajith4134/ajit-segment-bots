"""Nothing is still held when a contract stops existing.

The hazard these tests are about is not a mis-sized trade, it is a category
change: an Indian single-stock option that reaches expiry still open stops being
a premium and becomes a delivery obligation for strike x lot size.

Two of these tests are about NOT acting, and they matter as much as the rest:
`expiry-day-zero-to-hero-detector` exists to trade the expiry-day move, so a
closer that refused to hold a contract on its expiry day would forbid the one
detector written for that day.
"""

import datetime
import zoneinfo

import pytest

from parts.risk_capital_allocation.pre_expiry_position_closer import (
    EXPIRES_TODAY, PreExpiryPositionCloser, describe_closing,
)
from runtime.market_conditions import EXCHANGE_TIMEZONE
from runtime.trading_types import Position

IST = zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
CLOSES_AT = datetime.time(15, 30)
EXPIRY_DAY = datetime.date(2026, 9, 8)


def at(hour, minute, day=EXPIRY_DAY):
    return int(
        datetime.datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=IST
        ).timestamp() * 1e9
    )


class _Listing:
    def __init__(self, instrument_key, trading_symbol, expiry_ms):
        self.instrument_key = instrument_key
        self.trading_symbol = trading_symbol
        self.expiry_ms = expiry_ms


class _Session:
    def __init__(self, as_of_date, is_tradeable=True):
        self.as_of_date = as_of_date
        self.is_tradeable = is_tradeable


def an_option(instrument_key="NSE_FO|84221", symbol="NIFTY 24150 CE 08 SEP 26",
              expires=EXPIRY_DAY):
    noon = datetime.datetime(
        expires.year, expires.month, expires.day, 12, 0, tzinfo=IST,
    )
    return _Listing(instrument_key, symbol, int(noon.timestamp() * 1000))


def a_position(symbol="NIFTY 24150 CE 08 SEP 26", quantity=75.0):
    return Position(
        venue_id="upstox", symbol=symbol, quantity=quantity,
        average_entry_price=68.25, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=at(10, 0), updated_at_ns=at(10, 0),
    )


def a_closer(now_ns, minutes_before=15.0):
    return PreExpiryPositionCloser(
        minutes_before_the_close=minutes_before,
        session_closes_at=CLOSES_AT,
        timezone=IST,
        repeat_after_seconds=5.0,
        quantity_increment=1.0,
        now_ns=now_ns,
    )


class _Clock:
    def __init__(self, at_ns):
        self.at_ns = at_ns

    def __call__(self):
        return self.at_ns

    def advance_seconds(self, seconds):
        self.at_ns += int(seconds * 1e9)


# ---- when it must not act ----------------------------------------------------


def test_a_contract_expiring_today_is_left_alone_until_the_window_opens():
    """The expiry-day move is what expiry-day-zero-to-hero-detector trades. A
    closer that acted at the open would forbid the detector's whole reason to
    exist."""
    clock = _Clock(at(10, 0))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    assert closer.exits_to_place() == ()
    assert closer.positions_expiring_today == 1, "it knows, it is simply not acting yet"
    assert closer.ticks_before_the_window == 1


def test_a_contract_expiring_later_is_never_closed_even_inside_the_window():
    clock = _Clock(at(15, 25))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option(expires=datetime.date(2026, 9, 15)))
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    assert closer.exits_to_place() == ()
    assert closer.positions_expiring_today == 0


def test_nothing_happens_without_a_session_to_judge_the_date_against():
    """The date comes from the calendar, never from this machine's clock:
    judging expiry against a different date closes a day early or not at all."""
    clock = _Clock(at(15, 25))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_position(a_position())

    assert closer.exits_to_place() == ()
    assert closer.refused_no_session >= 1


def test_where_an_exit_is_sent_is_never_guessed():
    clock = _Clock(at(15, 25))
    closer = a_closer(clock)
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    assert closer.exits_to_place() == ()
    assert closer.placer.standing.refused_no_money_mode >= 1


# ---- when it must act --------------------------------------------------------


def test_a_position_in_a_contract_expiring_today_is_closed_inside_the_window():
    clock = _Clock(at(15, 15))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    exits = closer.exits_to_place()

    assert len(exits) == 1
    order = exits[0]
    assert order.symbol == "NIFTY 24150 CE 08 SEP 26"
    assert order.side == "sell"
    assert order.quantity == 75.0
    assert order.order_type == "market"
    assert EXPIRES_TODAY in order.reason
    assert order.client_order_id.startswith("pre-expiry-")


def test_the_reason_says_expiry_and_never_reads_like_a_human_asked():
    """What a record says about why a trade ended is what every learner here
    trains on -- a forced expiry close and a human's close-positions must never
    be the same lesson."""
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    reason = closer.exits_to_place()[0].reason

    assert "expires today" in reason
    assert "human" not in reason and "override" not in reason


def test_a_position_is_resolved_by_instrument_key_as_well_as_trading_symbol():
    """Some parts open a position under the instrument_key and some under the
    trading_symbol; a contract identified by only one of them would be a
    position this part silently could not protect."""
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position(symbol="NSE_FO|84221"))

    assert len(closer.exits_to_place()) == 1


def test_a_short_option_is_bought_back_rather_than_sold_again():
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position(quantity=-75.0))

    assert closer.exits_to_place()[0].side == "buy"


# ---- what it cannot judge ----------------------------------------------------


def test_a_position_with_no_listing_is_reported_never_assumed_safe():
    """Absence of evidence is its own state (Rule 8): 'this is not an option'
    and 'I could not find out what this is' must not read the same."""
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position(symbol="SOMETHING-NOBODY-LISTED"))

    assert closer.exits_to_place() == ()
    assert closer.positions_with_no_listing == 1
    assert closer.positions_that_are_not_options == 0


def test_an_underlying_is_known_to_be_not_an_option_rather_than_unknown():
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(_Listing("NSE_INDEX|Nifty 50", "NIFTY", expiry_ms=None))
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position(symbol="NIFTY"))

    assert closer.exits_to_place() == ()
    assert closer.positions_that_are_not_options == 1
    assert closer.positions_with_no_listing == 0


# ---- it must not oversell ----------------------------------------------------


def test_the_exit_is_not_repeated_before_the_book_could_have_answered():
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    assert len(closer.exits_to_place()) == 1
    clock.advance_seconds(1.0)
    assert closer.exits_to_place() == ()


def test_an_unanswered_exit_is_never_asked_for_twice_over():
    """The 2026-08-30 bound: allowance is what is held now less what is already
    asked and unanswered, so a repeat can never sell the position again."""
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    first = closer.exits_to_place()
    clock.advance_seconds(60.0)
    closer.observe_position(a_position())  # still open, nothing filled
    again = closer.exits_to_place()

    assert len(first) == 1
    assert again == (), "the whole position is already asked for"
    assert closer.placer.standing.positions_at_the_cap == 1
    assert closer.placer.standing.quantity_asked_beyond_the_position == 0.0


def test_a_partly_filled_exit_asks_only_for_what_is_left():
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position(quantity=75.0))
    assert closer.exits_to_place()[0].quantity == 75.0

    clock.advance_seconds(60.0)
    closer.observe_position(a_position(quantity=50.0))  # 25 filled
    closer.observe_position(a_position(quantity=50.0))
    rest = closer.exits_to_place()

    assert rest == (), "25 filled answers 25 of the 75 asked; 50 is still working"
    assert closer.placer.standing.quantity_asked_beyond_the_position == 0.0


# ---- what it reports ---------------------------------------------------------


def test_a_window_of_zero_minutes_is_refused():
    with pytest.raises(ValueError) as refusal:
        a_closer(_Clock(at(15, 20)), minutes_before=0.0)
    assert "closing before" in str(refusal.value)


def test_the_standing_separates_the_ways_it_can_do_nothing():
    closer = a_closer(_Clock(at(10, 0)))
    reported = describe_closing(closer)

    assert reported["part_id"] == "pre-expiry-position-closer"
    for name in (
        "positions_expiring_today", "positions_with_no_listing",
        "positions_that_are_not_options", "ticks_before_the_window",
        "refused_no_session", "closed_because_of_expiry",
        "quantity_asked_beyond_the_position",
    ):
        assert reported[name] == 0, name


def test_a_session_reading_nobody_restated_stops_being_believed():
    """A calendar that died on Friday must not still assert Friday's session on
    Monday: closing against the wrong date closes a day early, or not at all."""
    clock = _Clock(at(15, 20))
    closer = a_closer(clock)
    closer.observe_money_mode("paper")
    closer.observe_listing(an_option())
    closer.observe_session(_Session(EXPIRY_DAY))
    closer.observe_position(a_position())

    closer.forget_the_session()

    assert closer.exits_to_place() == ()
    assert closer.session_readings_too_old == 1
    assert closer.refused_no_session >= 1
    assert describe_closing(closer)["session_readings_too_old"] == 1
