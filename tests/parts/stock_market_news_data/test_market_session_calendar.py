"""The holiday document is the live response from
www.nseindia.com/api/holiday-master?type=trading, captured 2026-09-02: keyed by
segment ("FO" among twelve -- CBM, CD, CM, CMOT, COM, EGR, FO, IRD, MF, NDM,
NTRP, SLBS), each row carrying tradingDate/weekDay/description."""

import datetime
import zoneinfo

from runtime.market_conditions import SessionKind
from parts.stock_market_news_data.market_session_calendar import (
    MarketSessionCalendar,
    read_clock_time,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

HOLIDAY_DOCUMENT = {
    "FO": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday",
         "description": "Republic Day", "morning_session": None,
         "evening_session": None, "Sr_no": 2},
    ],
    "CM": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday",
         "description": "Republic Day", "morning_session": None,
         "evening_session": None, "Sr_no": 2},
    ],
}


def _calendar(segment="FO"):
    calendar = MarketSessionCalendar(
        segment=segment, opens_at=datetime.time(9, 15), closes_at=datetime.time(15, 30),
        timezone=IST,
    )
    calendar.observe_holidays(HOLIDAY_DOCUMENT)
    return calendar


def test_a_weekday_inside_session_hours_is_open():
    moment = datetime.datetime(2026, 9, 2, 10, 0, tzinfo=IST)  # a Wednesday
    assert _calendar().session_at(moment).kind is SessionKind.OPEN


def test_before_the_open_is_closed_not_open():
    """09:00-09:15 is the cash-segment pre-open auction. An order sent into it
    behaves differently from a normal-market order and this project has no
    pre-open order type, so it is deliberately not counted as open."""
    moment = datetime.datetime(2026, 9, 2, 9, 0, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_the_close_itself_is_already_closed():
    moment = datetime.datetime(2026, 9, 2, 15, 30, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_after_the_close_is_closed():
    """The failure this stops: paper-fill-simulator filling an overnight order
    at the last price it saw and journalling it as a trade."""
    moment = datetime.datetime(2026, 9, 2, 18, 0, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_a_weekend_is_closed_even_inside_session_hours():
    moment = datetime.datetime(2026, 9, 5, 11, 0, tzinfo=IST)  # a Saturday
    state = _calendar().session_at(moment)
    assert state.kind is SessionKind.CLOSED
    assert state.reason == "weekend"


def test_a_holiday_is_a_holiday_not_merely_closed_and_says_which_one():
    """Closed and holiday are different facts: one ends at 09:15 tomorrow, the
    other is the exchange not trading at all that day."""
    moment = datetime.datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    state = _calendar().session_at(moment)
    assert state.kind is SessionKind.HOLIDAY
    assert state.reason == "Republic Day"


def test_a_holiday_outside_session_hours_is_still_a_holiday():
    """The holiday outranks the clock: at 20:00 on Republic Day the honest
    answer is 'the exchange did not trade today', not 'after the close'."""
    moment = datetime.datetime(2026, 1, 26, 20, 0, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.HOLIDAY


def test_only_this_segment_s_holidays_are_read():
    """holiday-master carries twelve segments. A commodity holiday is not an
    equity-derivatives holiday, and reading them all would close the market on
    days it trades."""
    moment = datetime.datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    assert _calendar(segment="COM").session_at(moment).kind is not SessionKind.HOLIDAY


def test_a_session_is_never_reported_open_before_any_holiday_list_arrived():
    """Absence of evidence is its own state (Rule 8). An empty calendar means
    'not measured', and reporting OPEN off it would let the bot trade into a
    holiday because a fetch had not happened yet."""
    calendar = MarketSessionCalendar(
        segment="FO", opens_at=datetime.time(9, 15), closes_at=datetime.time(15, 30),
        timezone=IST,
    )
    moment = datetime.datetime(2026, 9, 2, 10, 0, tzinfo=IST)
    state = calendar.session_at(moment)
    assert state.kind is SessionKind.CLOSED
    assert "no holiday list" in state.reason


def test_a_moment_in_another_timezone_is_read_in_ist():
    """The spine's clock is UTC. 04:45 UTC is 10:15 IST, inside the session --
    reading it as UTC would call the market closed for the entire morning."""
    utc = zoneinfo.ZoneInfo("UTC")
    moment = datetime.datetime(2026, 9, 2, 4, 45, tzinfo=utc)
    assert _calendar().session_at(moment).kind is SessionKind.OPEN


def test_the_holiday_count_is_reported_so_an_empty_list_is_visible():
    assert _calendar().holidays_known == 1


def test_a_clock_time_is_read_from_the_setting_s_own_string():
    assert read_clock_time("09:15") == datetime.time(9, 15)
    assert read_clock_time("15:30") == datetime.time(15, 30)
