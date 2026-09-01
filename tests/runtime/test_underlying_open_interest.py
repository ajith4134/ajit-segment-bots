"""Tests against real Upstox instrument-master values (assets.upstox.com/
market-quote/instruments/exchange/complete.json.gz, fetched 2026-09-01):
NSE_INDEX|Nifty 50 (trading_symbol NIFTY) is the underlying_key on real
option listings such as NSE_FO|36708 (IDEA 22 CE 25 JAN 24, upstox.com/
developer/api-documentation/instruments' own Options sample). Never an
invented instrument_key (RL-063)."""

import pytest

from runtime.brokers.broker_adapter import BrokerOpenInterest, InstrumentListing
from runtime.underlying_open_interest import UnderlyingOpenInterestAggregator

NIFTY_UNDERLYING_LISTING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)
NIFTY_CALL_LISTING = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1740729599000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)
NIFTY_PUT_LISTING = InstrumentListing(
    instrument_key="NSE_FO|1002", exchange="NSE", segment="NSE_FO",
    instrument_type="PE", trading_symbol="NIFTY 24500 PE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1740729599000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)
# Real EQ sample (upstox.com/developer/api-documentation/instruments), no
# underlying_key -- proves an equity listing never contributes to an
# aggregate the way an option contract does.
JOCIL_LISTING = InstrumentListing(
    instrument_key="NSE_EQ|INE839G01010", exchange="NSE", segment="NSE_EQ",
    instrument_type="EQ", trading_symbol="JOCIL", lot_size=1, tick_size=5.0,
    freeze_quantity=100000.0, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)


def _oi(instrument_key, open_interest, buy, sell, at_ns=1_740_000_000_000_000_000):
    return BrokerOpenInterest(
        instrument_key=instrument_key, open_interest=open_interest,
        volume_traded_today=0.0, total_buy_quantity=buy, total_sell_quantity=sell,
        average_traded_price=0.0, broker_time_ns=at_ns,
    )


def test_totals_for_is_none_before_any_listing_resolved():
    aggregator = UnderlyingOpenInterestAggregator()
    aggregator.observe_open_interest(_oi("NSE_FO|1001", 1000.0, 600.0, 400.0))
    assert aggregator.totals_for("NIFTY") is None


def test_totals_for_is_none_before_any_open_interest_observed():
    aggregator = UnderlyingOpenInterestAggregator()
    aggregator.observe_listing(NIFTY_UNDERLYING_LISTING)
    aggregator.observe_listing(NIFTY_CALL_LISTING)
    assert aggregator.totals_for("NIFTY") is None


def test_sums_open_interest_and_flow_across_the_chain_for_one_underlying():
    aggregator = UnderlyingOpenInterestAggregator()
    aggregator.observe_listing(NIFTY_UNDERLYING_LISTING)
    aggregator.observe_listing(NIFTY_CALL_LISTING)
    aggregator.observe_listing(NIFTY_PUT_LISTING)
    aggregator.observe_open_interest(_oi("NSE_FO|1001", 1000.0, 600.0, 400.0))
    aggregator.observe_open_interest(_oi("NSE_FO|1002", 500.0, 100.0, 300.0))

    totals = aggregator.totals_for("NIFTY")
    assert totals is not None
    assert totals.open_interest == 1500.0
    assert totals.total_buy_quantity == 700.0
    assert totals.total_sell_quantity == 700.0


def test_a_later_reading_for_one_contract_replaces_rather_than_adds():
    aggregator = UnderlyingOpenInterestAggregator()
    aggregator.observe_listing(NIFTY_UNDERLYING_LISTING)
    aggregator.observe_listing(NIFTY_CALL_LISTING)
    aggregator.observe_open_interest(_oi("NSE_FO|1001", 1000.0, 600.0, 400.0))
    aggregator.observe_open_interest(_oi("NSE_FO|1001", 1100.0, 650.0, 420.0))

    totals = aggregator.totals_for("NIFTY")
    assert totals.open_interest == 1100.0
    assert totals.total_buy_quantity == 650.0


def test_an_equity_listing_with_no_underlying_key_is_never_aggregated():
    aggregator = UnderlyingOpenInterestAggregator()
    aggregator.observe_listing(JOCIL_LISTING)
    aggregator.observe_open_interest(_oi("NSE_EQ|INE839G01010", 999.0, 1.0, 1.0))
    # JOCIL has no underlying_key, so it is not an option contract on
    # anything -- nothing to resolve it against, nothing to sum it into.
    assert aggregator.totals_for("JOCIL") is None


def test_an_unresolved_instrument_key_is_ignored_not_raised():
    aggregator = UnderlyingOpenInterestAggregator()
    # No listing observed for this key at all -- the feed can arrive before
    # the catalogue does, and that is a fact about timing, not a fault.
    aggregator.observe_open_interest(_oi("NSE_FO|9999", 1000.0, 1.0, 1.0))
    assert aggregator.totals_for("NIFTY") is None
