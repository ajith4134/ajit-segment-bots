"""The replay's "is the market open" check can answer True.

Until 2026-09-13 `the_market_is_open_now` built a `MarketSessionCalendar` and never
gave it a holiday list, so `session_at` answered CLOSED at every moment and every
run replayed history, market open or not. The holiday document here is NSE's own
response from `holiday-master?type=trading`, the slice
`tests/parts/stock_market_news_data/test_market_session_calendar.py` captured on
2026-09-02.
"""

from __future__ import annotations

import datetime
import importlib.util
import pathlib
import zoneinfo

import pytest

from runtime.market_session_answer import IN_SESSION, OUT_OF_SESSION
from tests.parts.stock_market_news_data.test_market_session_calendar import HOLIDAY_DOCUMENT

REPLAY_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "operate/replay_a_captured_session.py"
IST = zoneinfo.ZoneInfo("Asia/Kolkata")
NO_LIVE_ANSWER = lambda: (None, "no spine running")  # noqa: E731

A_MONDAY_MID_SESSION = datetime.datetime(2026, 9, 7, 10, 0, tzinfo=IST)
A_SUNDAY_MID_DAY = datetime.datetime(2026, 9, 13, 10, 0, tzinfo=IST)
REPUBLIC_DAY_MID_SESSION = datetime.datetime(2026, 1, 26, 10, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("replay_a_captured_session", REPLAY_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_live_spines_answer_is_used_when_there_is_one(replay):
    assert replay.the_market_is_open_now(live_answer=lambda: (IN_SESSION, "calendar")) is True
    assert replay.the_market_is_open_now(live_answer=lambda: (OUT_OF_SESSION, "calendar")) is False


def test_with_no_spine_a_weekday_in_session_hours_is_open(replay):
    """The case that could never happen before: the calendar now has its holidays."""
    assert replay.the_market_is_open_now(
        live_answer=NO_LIVE_ANSWER, fetch_holidays=lambda: HOLIDAY_DOCUMENT, now=A_MONDAY_MID_SESSION,
    ) is True


@pytest.mark.parametrize("moment", [A_SUNDAY_MID_DAY, REPUBLIC_DAY_MID_SESSION])
def test_with_no_spine_a_weekend_or_holiday_is_not_open(replay, moment):
    assert replay.the_market_is_open_now(
        live_answer=NO_LIVE_ANSWER, fetch_holidays=lambda: HOLIDAY_DOCUMENT, now=moment,
    ) is False


def test_when_nothing_can_answer_the_answer_is_none_not_closed(replay):
    def unreachable():
        raise OSError("nseindia.com did not answer")

    assert replay.the_market_is_open_now(
        live_answer=NO_LIVE_ANSWER, fetch_holidays=unreachable, now=A_MONDAY_MID_SESSION,
    ) is None


@pytest.mark.network
def test_with_no_spine_the_real_holiday_list_is_fetched_and_read(replay):
    """The real NSE fetch, on this moment's clock: an answer, not None."""
    assert replay.the_market_is_open_now(live_answer=NO_LIVE_ANSWER) in (True, False)
