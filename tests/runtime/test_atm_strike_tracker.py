"""Tests against real Upstox instrument-master shapes (verified this
session: assets.upstox.com/market-quote/instruments/exchange/complete.json.gz,
fetched 2026-09-01 — NSE_INDEX|Nifty 50, trading_symbol NIFTY). Strike-level
option listings (NSE_FO|1001 etc.) follow the same InstrumentListing shape
already verified against Upstox's own documented Options sample
(tests/parts/broker_adapter/test_broker_underlying_price_frame_bridge.py) --
never an invented shape (RL-063)."""

import pytest

from runtime.atm_strike_tracker import AtmStrikeTracker
from runtime.brokers.broker_adapter import BrokerOptionGreeks, InstrumentListing

NIFTY_UNDERLYING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)


def _call(instrument_key, strike, expiry_ms=1_740_000_000_000):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type="CE", trading_symbol=f"NIFTY {strike:g} CE",
        lot_size=75, tick_size=0.05, freeze_quantity=1800.0, expiry_ms=expiry_ms,
        strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _put(instrument_key, strike, expiry_ms=1_740_000_000_000):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type="PE", trading_symbol=f"NIFTY {strike:g} PE",
        lot_size=75, tick_size=0.05, freeze_quantity=1800.0, expiry_ms=expiry_ms,
        strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _greeks(instrument_key, delta, at_ns=1_740_000_000_000_000_000):
    return BrokerOptionGreeks(
        instrument_key=instrument_key, delta=delta, theta=-2.0, gamma=0.001,
        vega=5.0, rho=0.5, implied_volatility=0.15, broker_time_ns=at_ns,
    )


def test_atm_call_for_is_none_before_any_greeks_observed():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0))
    assert tracker.atm_call_for("NIFTY") is None


def test_picks_the_call_closest_to_half_delta():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24400.0))
    tracker.observe_listing(_call("NSE_FO|1002", 24500.0))
    tracker.observe_listing(_call("NSE_FO|1003", 24600.0))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.65))
    tracker.observe_greeks(_greeks("NSE_FO|1002", 0.52))
    tracker.observe_greeks(_greeks("NSE_FO|1003", 0.31))

    choice = tracker.atm_call_for("NIFTY")
    assert choice is not None
    assert choice.instrument_key == "NSE_FO|1002"
    assert choice.strike_price == 24500.0
    assert choice.distance_from_atm == pytest.approx(0.02)


def test_calls_and_puts_are_tracked_separately():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0))
    tracker.observe_listing(_put("NSE_FO|2001", 24500.0))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.51))
    tracker.observe_greeks(_greeks("NSE_FO|2001", -0.49))

    assert tracker.atm_call_for("NIFTY").instrument_key == "NSE_FO|1001"
    assert tracker.atm_put_for("NIFTY").instrument_key == "NSE_FO|2001"


def test_a_later_greeks_reading_replaces_the_earlier_one_for_the_same_contract():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.80))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.51))
    choice = tracker.atm_call_for("NIFTY")
    assert choice.delta == 0.51


def test_only_the_nearest_expiry_is_ever_returned():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0, expiry_ms=2_000_000_000_000))
    tracker.observe_listing(_call("NSE_FO|1002", 24500.0, expiry_ms=1_000_000_000_000))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.50))
    tracker.observe_greeks(_greeks("NSE_FO|1002", 0.60))

    choice = tracker.atm_call_for("NIFTY", now_ms=500_000_000_000)
    assert choice.instrument_key == "NSE_FO|1002"


def test_an_expiry_already_passed_is_never_returned():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0, expiry_ms=1_000_000_000_000))
    tracker.observe_greeks(_greeks("NSE_FO|1001", 0.50))

    assert tracker.atm_call_for("NIFTY", now_ms=1_500_000_000_000) is None


def test_an_unresolved_contract_is_ignored_not_raised():
    tracker = AtmStrikeTracker()
    tracker.observe_greeks(_greeks("NSE_FO|9999", 0.5))
    assert tracker.atm_call_for("NIFTY") is None


def test_underlying_of_resolves_a_known_contract():
    tracker = AtmStrikeTracker()
    tracker.observe_listing(NIFTY_UNDERLYING)
    tracker.observe_listing(_call("NSE_FO|1001", 24500.0))
    assert tracker.underlying_of("NSE_FO|1001") == "NIFTY"


def test_underlying_of_is_none_for_an_unresolved_contract():
    tracker = AtmStrikeTracker()
    assert tracker.underlying_of("NSE_FO|9999") is None
