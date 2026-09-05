"""equity-opportunity-profiler: which ordinary shares still need today's window,
and what one answered window turns into.

`average_true_range` and the rotation logic are pure and tested directly on
constructed bars/listings, the same way `broker_history_reader`'s own tests
construct a bare `Listing` rather than a captured fixture for its session and
rotation logic -- the captured-data rule (RL-063) is about not faking market
data to validate a strategy, not about banning a hand-built bar for testing
arithmetic.
"""

from __future__ import annotations

import datetime

import pytest

from parts.market_data_feed.equity_opportunity_profiler import (
    PART_DECLARATION,
    PART_ID,
    EquityOpportunityProfiler,
    average_true_range,
)
from runtime.part_declaration import load_declaration_from_blueprint

DAY = datetime.date(2026, 9, 5)


class Bar:
    def __init__(self, high, low, close, volume):
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class Listing:
    def __init__(self, key, symbol, segment="NSE_EQ", instrument_type="EQ", security_type="NORMAL"):
        self.instrument_key = key
        self.trading_symbol = symbol
        self.segment = segment
        self.instrument_type = instrument_type
        self.security_type = security_type


def admits_ordinary_shares(listing) -> bool:
    return (
        listing.segment == "NSE_EQ"
        and listing.instrument_type == "EQ"
        and listing.security_type == "NORMAL"
    )


def a_profiler(**kwargs):
    settings = dict(
        equity_admits=admits_ordinary_shares, days_back=365,
        atr_window_days=14, most_symbols_per_sweep=2, now_ns=lambda: 1_800_000_000_000_000_000,
    )
    settings.update(kwargs)
    return EquityOpportunityProfiler(**settings)


def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_average_true_range_needs_at_least_two_bars():
    assert average_true_range([Bar(101, 99, 100, 1000)], window_days=14) is None


def test_average_true_range_over_a_short_series():
    bars = [
        Bar(high=100, low=98, close=99, volume=1000),
        Bar(high=103, low=97, close=101, volume=1200),   # TR = max(6, 4, 2) = 6
        Bar(high=104, low=101, close=102, volume=900),    # TR = max(3, 3, 0) = 3
    ]
    assert average_true_range(bars, window_days=14) == pytest.approx((6 + 3) / 2)


def test_average_true_range_uses_only_the_trailing_window():
    # high-low is 1.0 on every bar, but each bar gaps 0.5 above the prior
    # close, so the true range (which also checks the gap) is 1.5 on every
    # bar after the first -- constant, so the trailing window and the whole
    # series must agree.
    bars = [Bar(100 + i, 99 + i, 99.5 + i, 1000) for i in range(20)]
    full = average_true_range(bars, window_days=1000)
    windowed = average_true_range(bars, window_days=3)
    assert windowed == pytest.approx(1.5)
    assert full == pytest.approx(1.5)


def test_only_ordinary_shares_become_candidates():
    profiler = a_profiler()
    profiler.observe_listings([
        Listing("NSE_EQ|1", "RELIANCE"),
        Listing("NSE_EQ|2", "SDLBOND", instrument_type="SG"),
        Listing("BSE_EQ|3", "SOMEBSE", segment="BSE_EQ"),
    ])
    assert profiler.standing.candidates_known == 1


def test_requests_due_is_capped_per_sweep_and_ordered_by_key():
    profiler = a_profiler(most_symbols_per_sweep=2)
    profiler.observe_listings([
        Listing("NSE_EQ|3", "TCS"),
        Listing("NSE_EQ|1", "RELIANCE"),
        Listing("NSE_EQ|2", "INFY"),
    ])
    requests = profiler.requests_due(today=DAY)
    assert [r.instrument_key for r in requests] == ["NSE_EQ|1", "NSE_EQ|2"]


def test_a_symbol_profiled_today_is_not_asked_for_again_today():
    profiler = a_profiler(most_symbols_per_sweep=10)
    profiler.observe_listings([Listing("NSE_EQ|1", "RELIANCE")])
    first = profiler.requests_due(today=DAY)
    assert len(first) == 1
    profiler._windows_read.add(first[0].window_key)
    second = profiler.requests_due(today=DAY)
    assert second == ()


def test_a_new_day_asks_again():
    profiler = a_profiler(most_symbols_per_sweep=10)
    profiler.observe_listings([Listing("NSE_EQ|1", "RELIANCE")])
    first = profiler.requests_due(today=DAY)
    profiler._windows_read.add(first[0].window_key)
    tomorrow = profiler.requests_due(today=DAY + datetime.timedelta(days=1))
    assert len(tomorrow) == 1


def test_days_back_below_two_is_refused():
    with pytest.raises(ValueError):
        a_profiler(days_back=1)


def test_a_sweep_of_zero_symbols_is_refused():
    with pytest.raises(ValueError):
        a_profiler(most_symbols_per_sweep=0)
