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
                 segment="NSE_FO", instrument_type="PE", expiry_ms=1_788_892_199_000):
        self.instrument_key = key
        self.trading_symbol = symbol
        self.segment = segment
        self.instrument_type = instrument_type
        self.expiry_ms = expiry_ms


def a_reader(**kwargs):
    settings = dict(
        interval_unit="minutes", interval=1, most_instruments=2,
        most_days_back=1, wanted_instrument_types=("CE", "PE"), underlying="NIFTY",
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


def test_each_sweep_reaches_the_contracts_the_last_one_did_not():
    """The cap is a rate limit on one sweep, not a horizon.

    Until 2026-09-04 the slice was taken before the already-read check, so every
    sweep re-offered the same first `most_instruments` contracts and, once their
    windows were read, planned nothing ever again. Measured on the live spine
    that day: 2,966 instruments known, 8 windows read, `requests_planned` frozen
    at 38 while `windows_already_read` climbed by 8 a tick -- history replayed
    four instrument-days and stopped, and the price detectors had eight symbols
    to fill a 256-observation window from.
    """
    reader = a_reader(most_instruments=2)
    reader.observe_listings([
        Listing(key=f"NSE_FO|{4260 + n}", symbol=f"NIFTY {n} CE 08 SEP 26",
                instrument_type="CE", expiry_ms=1_788_892_199_000 + n)
        for n in range(5)
    ])
    reader.observe_session(a_session(SessionKind.CLOSED))

    reached = []
    for _ in range(3):
        requests = reader.requests_due(today=DAY)
        reached.extend(request.instrument_key for request in requests)
        for request in requests:
            reader.observe_history(request, _fixture_response())

    assert reached == [f"NSE_FO|{4260 + n}" for n in range(5)], (
        "every contract has to be reached eventually, two at a time"
    )
    assert reader.requests_due(today=DAY) == (), "and then there is nothing left to ask for"
    assert reader.standing.windows_already_read == 5
    assert reader.standing.instruments_awaiting_a_window == 0


def test_one_sweep_never_asks_for_more_than_its_cap():
    """The rate limit still binds -- reaching the rest is not the same as
    asking for everything at once."""
    reader = a_reader(most_instruments=2)
    reader.observe_listings([
        Listing(key=f"NSE_FO|{4260 + n}", symbol=f"NIFTY {n} CE 08 SEP 26",
                instrument_type="CE", expiry_ms=1_788_892_199_000 + n)
        for n in range(5)
    ])
    reader.observe_session(a_session(SessionKind.CLOSED))

    assert len(reader.requests_due(today=DAY)) == 2
    assert reader.standing.instruments_awaiting_a_window == 5


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


# ---- the fill path prices from market-data, not from candle -------------------

def test_each_bar_also_becomes_one_print_the_fill_path_can_use():
    """paper-fill-simulator prices fills from market-data. History that reached
    only `candle` would feed the thinking half and never the filling half, so a
    replay could form an opinion and never act on it."""
    from runtime.tape import TradeFidelity

    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))
    request = reader.requests_due(today=DAY)[0]

    trades = reader.trades_from(reader.observe_history(request, _fixture_response()))

    assert len(trades) == 385
    first = trades[0]
    assert first.symbol == "NIFTY 24350 PE 08 SEP 26"
    assert first.venue_id == "upstox"
    assert first.fidelity == TradeFidelity.HISTORICAL_BAR_CLOSE, (
        "the fill gate reads this to know the bar is evidence of its own session"
    )
    assert first.side is None, "a bar's close has no aggressor to report"


def test_the_print_is_the_bar_s_close_at_the_bar_s_close_time():
    """Not the open, and not now. A bar's close is the last price that really
    traded in that minute, and it is the price at the end of it."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))
    request = reader.requests_due(today=DAY)[0]
    candles = reader.observe_history(request, _fixture_response())

    trades = reader.trades_from(candles)

    assert trades[0].price == candles[0].close
    assert trades[0].quantity == candles[0].volume
    assert trades[0].venue_time_ns == candles[0].close_time_ns
    assert trades[0].venue_time_ns > candles[0].open_time_ns


def test_the_prints_carry_a_sequence_that_advances():
    """A gap detector reads sequence. One that never moved would read as a feed
    that had stalled for the whole replay."""
    reader = a_reader()
    reader.observe_listings([Listing()])
    reader.observe_session(a_session(SessionKind.CLOSED))
    request = reader.requests_due(today=DAY)[0]

    trades = reader.trades_from(reader.observe_history(request, _fixture_response()))

    sequences = [trade.sequence for trade in trades]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_it_asks_about_the_underlying_it_was_told_to_and_the_nearest_expiry_first():
    """Live, 2026-09-02: the reader knew 76,036 CE/PE contracts and took the
    first eight by key string -- arbitrary illiquid strikes that had never
    traded, so every fetch returned an empty series and nothing was published.

    The instruments worth replaying are the ones the segment actually trades:
    one underlying, nearest expiry first, which is where the volume is.
    """
    reader = a_reader(most_instruments=2, underlying="NIFTY")
    reader.observe_listings([
        Listing(key="NSE_FO|1", symbol="BANKNIFTY 50000 CE 08 SEP 26", expiry_ms=1),
        Listing(key="NSE_FO|2", symbol="NIFTY 24350 PE 15 SEP 26", expiry_ms=3),
        Listing(key="NSE_FO|3", symbol="NIFTY 24350 PE 08 SEP 26", expiry_ms=2),
    ])
    reader.observe_session(a_session(SessionKind.CLOSED))

    keys = [request.instrument_key for request in reader.requests_due(today=DAY)]

    assert "NSE_FO|1" not in keys, "BANKNIFTY is a different underlying"
    assert keys[0] == "NSE_FO|3", "the nearest expiry is where the volume is"
    assert keys == ["NSE_FO|3", "NSE_FO|2"]
