"""cash-equity-shortlist-ranker: turning listings, a historical profile,
liquidity, price and candle readings into today's shortlist.

Constructed inputs, the same reasoning as `test_equity_opportunity_profiler.py`
gives for its own hand-built `Listing`/`Bar` -- the thing under test is the
part's own bookkeeping (session-day resets, key-to-symbol translation, the
None-shortlist-while-empty rule), not one day's real market data.
"""

from __future__ import annotations

import datetime

import pytest

from parts.market_data_feed.cash_equity_shortlist_ranker import (
    PART_DECLARATION,
    PART_ID,
    CashEquityShortlistRanker,
    ist_date_of,
)
from runtime.cash_equity_shortlist import ShortlistWeights
from runtime.part_declaration import load_declaration_from_blueprint

EQUAL_WEIGHTS = ShortlistWeights(
    momentum_weight=1 / 6, volume_weight=1 / 6, week_52_weight=1 / 6,
    gap_weight=1 / 6, vwap_weight=1 / 6, atr_weight=1 / 6,
)


class Listing:
    def __init__(self, key, symbol):
        self.instrument_key = key
        self.trading_symbol = symbol
        self.segment = "NSE_EQ"
        self.instrument_type = "EQ"
        self.security_type = "NORMAL"


def admits_all(_listing) -> bool:
    return True


class Profile:
    def __init__(self, symbol, previous_close=100.0, week_52_high=120.0,
                 week_52_low=80.0, average_daily_volume=1_000_000.0, average_true_range=5.0):
        self.symbol = symbol
        self.previous_close = previous_close
        self.week_52_high = week_52_high
        self.week_52_low = week_52_low
        self.average_daily_volume = average_daily_volume
        self.average_true_range = average_true_range


class Grade:
    def __init__(self, symbol, spread_fraction=0.001):
        self.symbol = symbol
        self.spread_fraction = spread_fraction


class PriceLevel:
    def __init__(self, instrument_key, price):
        self.instrument_key = instrument_key
        self.price = price


class PriceFrame:
    def __init__(self, levels):
        self.levels = levels


class Candle:
    def __init__(self, symbol, open_time_ns, open, volume, quote_volume):
        self.symbol = symbol
        self.open_time_ns = open_time_ns
        self.open = open
        self.volume = volume
        self.quote_volume = quote_volume


DAY_ONE_OPEN_NS = 1_800_000_000_000_000_000  # an arbitrary but fixed instant
DAY_TWO_OPEN_NS = DAY_ONE_OPEN_NS + 24 * 3600 * 1_000_000_000


def a_ranker(**kwargs):
    settings = dict(
        equity_admits=admits_all, shortlist_size=1, liquidity_pool_size=2,
        weights=EQUAL_WEIGHTS, now_ns=lambda: 1_900_000_000_000_000_000,
    )
    settings.update(kwargs)
    return CashEquityShortlistRanker(**settings)


def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_ist_date_crosses_midnight_before_utc_does():
    # 19:00 UTC is already 00:30 IST the next day.
    evening_utc_ns = int(
        datetime.datetime(2026, 9, 4, 19, 0, tzinfo=datetime.timezone.utc).timestamp()
        * 1_000_000_000
    )
    assert ist_date_of(evening_utc_ns) == datetime.date(2026, 9, 5)


def test_no_candidates_yields_no_shortlist():
    ranker = a_ranker()
    assert ranker.rank() is None


def test_a_full_pipeline_produces_a_shortlist():
    ranker = a_ranker(shortlist_size=1, liquidity_pool_size=2)
    ranker.observe_listings([Listing("NSE_EQ|1", "MOVER"), Listing("NSE_EQ|2", "FLAT")])
    ranker.observe_profile(Profile("MOVER"))
    ranker.observe_profile(Profile("FLAT"))
    ranker.observe_liquidity_grade(Grade("MOVER"))
    ranker.observe_liquidity_grade(Grade("FLAT"))
    ranker.observe_candle(Candle("MOVER", DAY_ONE_OPEN_NS, open=100.0, volume=1000, quote_volume=100_000.0))
    ranker.observe_candle(Candle("FLAT", DAY_ONE_OPEN_NS, open=100.0, volume=1000, quote_volume=100_000.0))
    ranker.observe_price_frame(PriceFrame([PriceLevel("NSE_EQ|1", 110.0), PriceLevel("NSE_EQ|2", 100.0)]))

    shortlist = ranker.rank()

    assert shortlist is not None
    assert shortlist.symbols == ("MOVER",)
    assert shortlist.candidates_considered == 2


def test_a_new_session_day_resets_the_cumulative_volume_and_open():
    ranker = a_ranker()
    ranker.observe_listings([Listing("NSE_EQ|1", "X")])
    ranker.observe_candle(Candle("X", DAY_ONE_OPEN_NS, open=100.0, volume=1000, quote_volume=100_000.0))
    assert ranker._cumulative_volume_by_symbol["X"] == 1000
    ranker.observe_candle(Candle("X", DAY_ONE_OPEN_NS + 60_000_000_000, open=100.0, volume=500, quote_volume=50_000.0))
    assert ranker._cumulative_volume_by_symbol["X"] == 1500

    ranker.observe_candle(Candle("X", DAY_TWO_OPEN_NS, open=105.0, volume=200, quote_volume=21_000.0))
    assert ranker._cumulative_volume_by_symbol["X"] == 200
    assert ranker._session_open_by_symbol["X"] == 105.0


def test_a_candle_for_an_untracked_symbol_is_ignored():
    ranker = a_ranker()
    ranker.observe_listings([Listing("NSE_EQ|1", "X")])
    ranker.observe_candle(Candle("NIFTY 24000 CE", DAY_ONE_OPEN_NS, open=100.0, volume=10, quote_volume=1000.0))
    assert "NIFTY 24000 CE" not in ranker._session_open_by_symbol
