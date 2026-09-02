"""broker-history-reader, against the bars Upstox actually served (RL-063).

The fixture is one real response: 385 one-minute bars for NSE_FO|42654
(NIFTY 24350 PE 08 SEP 26) across 2026-09-01, fetched 2026-09-02.

What these tests hold in place is the reason the part exists at all. It runs
only when the market is shut, it publishes `candle` and never `broker-candle`
so the live tape stays a record of live capture, and the bars it publishes carry
the moment they were really printed -- not the moment they were replayed.
"""

from __future__ import annotations

import pytest

from parts.broker_adapter.broker_history_reader import (
    PART_DECLARATION,
    PART_ID,
    HistoryReader,
    describe_history_reading,
)
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.part_declaration import load_declaration_from_blueprint

import datetime

SEGMENT = "index-options"
DAY = datetime.date(2026, 9, 1)


def a_session(kind):
    return MarketSessionState(
        segment=SEGMENT, kind=kind, as_of_date=DAY,
        reason="for the test", observed_at_ns=1_788_000_000_000_000_000,
    )


class Listing:
    """A broker-instrument-listing, as this part reads it."""

    def __init__(self, key="NSE_FO|42654", symbol="NIFTY 24350 PE 08 SEP 26",
                 segment="NSE_FO", instrument_type="PE"):
        self.instrument_key = key
        self.trading_symbol = symbol
        self.segment = segment
        self.instrument_type = instrument_type


def a_reader(**kwargs):
    settings = dict(
        interval_unit="minutes", interval=1, most_instruments=2,
        most_days_back=1, wanted_instrument_types=("CE", "PE"),
    )
    settings.update(kwargs)
    return HistoryReader(**settings)


def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_it_asks_for_nothing_while_the_market_is_open():
    """The live feed is the source when there is one. This is the whole reason
    the part is allowed to exist without crossing RL-071: it never stands in for
    a market it could be reading."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.OPEN))

    assert reader.requests_due(today=DAY) == ()
    assert reader.standing.skipped_because_the_market_is_open == 1


def test_it_asks_for_history_while_the_market_is_shut():
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))

    requests = reader.requests_due(today=DAY)

    assert len(requests) == 1
    assert requests[0].instrument_key == "NSE_FO|42654"
    assert requests[0].unit == "minutes" and requests[0].interval == 1


def test_it_asks_for_nothing_before_it_has_heard_what_the_session_is():
    """Absence of a session reading is not 'the market is shut'. A part that
    assumed it would fetch history straight through the opening bell."""
    reader = a_reader()
    reader.observe_listings([Listing()])

    assert reader.requests_due(today=DAY) == ()
    assert reader.standing.skipped_because_the_session_is_unknown == 1


def test_only_the_option_contracts_are_asked_for():
    """The master carries 75,278 instruments; this segment trades options."""
    reader = a_reader()
    reader.observe_listings([
        Listing(key="NSE_FO|42654", instrument_type="PE"),
        Listing(key="NSE_EQ|INE848E01016", segment="NSE_EQ", instrument_type="EQ"),
    ])
    reader.observe_session(a_session(SessionKind.CLOSED))

    keys = [request.instrument_key for request in reader.requests_due(today=DAY)]
    assert keys == ["NSE_FO|42654"]


def test_the_same_window_is_not_asked_for_twice():
    """One month per request and a real rate limit on the other end: asking
    again for a window already answered spends quota to learn nothing."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))

    first = reader.requests_due(today=DAY)
    assert len(first) == 1
    reader.observe_history(first[0], _fixture_response())

    assert reader.requests_due(today=DAY) == ()
    assert reader.standing.windows_already_read == 1


def _fixture_response():
    import json, pathlib
    return json.loads(
        pathlib.Path(
            "tests/captured/upstox/2026-09-01-nifty-option-1min-historical.json"
        ).read_text()
    )


def test_real_bars_become_candles_carrying_the_moment_they_printed():
    """The property the whole design turns on: a replayed bar is a fact about a
    past moment, and it travels with that moment rather than with now."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))
    request = reader.requests_due(today=DAY)[0]

    candles = reader.observe_history(request, _fixture_response())

    assert len(candles) == 385, len(candles)
    assert [c.open_time_ns for c in candles] == sorted(c.open_time_ns for c in candles)
    first, last = candles[0], candles[-1]
    assert first.symbol == "NIFTY 24350 PE 08 SEP 26"
    assert first.venue_id == "upstox"
    # 09:15 IST on 2026-09-01 is 03:45:00Z.
    assert first.open_time_ns == 1_788_234_300_000_000_000
    assert first.close_time_ns > first.open_time_ns
    assert first.is_closed is True, "a historical bar is finished by construction"
    assert last.open_time_ns > first.open_time_ns
    assert reader.standing.candles_published == 385


def test_a_refused_response_publishes_no_candles_and_is_counted():
    """No bars and a failed request are different facts."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))
    request = reader.requests_due(today=DAY)[0]

    candles = reader.observe_history(request, {"status": "error", "errors": [{"m": "no"}]})

    assert candles == ()
    assert reader.standing.requests_refused == 1
    assert reader.standing.candles_published == 0


def test_a_holiday_is_shut_too():
    """CLOSED and HOLIDAY are different facts about why, and the same fact about
    whether there is a live market to read."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.HOLIDAY))

    assert len(reader.requests_due(today=DAY)) == 1


def test_what_it_says_about_itself_is_countable():
    reader = a_reader()
    standing = describe_history_reading(reader)
    assert standing["part_id"] == PART_ID
    for name, value in standing.items():
        if name != "part_id":
            assert isinstance(value, (int, float)), (name, value)
