"""Tests against real Upstox instrument-master values (assets.upstox.com/
market-quote/instruments/exchange/complete.json.gz, fetched 2026-09-01):
NSE_INDEX|Nifty 50 (trading_symbol NIFTY), NSE_INDEX|Nifty Bank
(trading_symbol BANKNIFTY), BSE_INDEX|SENSEX (trading_symbol SENSEX), each
instrument_type INDEX. The equity sample (JOCIL) is Upstox's own documented
EQ example (upstox.com/developer/api-documentation/instruments), reused from
tests/runtime/brokers/test_upstox.py. Never an invented instrument_key
(RL-063)."""

import pytest

from runtime.brokers.broker_adapter import InstrumentListing
from parts.broker_adapter.broker_price_level_sampler import BrokerPriceFrame, BrokerPriceLevel
from parts.market_data_feed.broker_underlying_price_frame_bridge import (
    BrokerUnderlyingPriceFrameBridge,
)

NIFTY_LISTING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)
BANKNIFTY_LISTING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty Bank", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="BANKNIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)
JOCIL_LISTING = InstrumentListing(
    instrument_key="NSE_EQ|INE839G01010", exchange="NSE", segment="NSE_EQ",
    instrument_type="EQ", trading_symbol="JOCIL", lot_size=1, tick_size=5.0,
    freeze_quantity=100000.0, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)


def _bridge():
    return BrokerUnderlyingPriceFrameBridge(
        tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX")
    )


def test_refuses_construction_with_no_tracked_symbols():
    with pytest.raises(ValueError):
        BrokerUnderlyingPriceFrameBridge(tracked_trading_symbols=())


def test_ignores_a_listing_whose_trading_symbol_is_not_tracked():
    bridge = _bridge()
    bridge.observe_listing(JOCIL_LISTING)
    assert bridge.standing.listings_seen == 1
    assert bridge.standing.underlyings_resolved == 0


def test_resolves_a_tracked_underlyings_instrument_key():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_LISTING)
    assert bridge.standing.underlyings_resolved == 1


def test_levels_for_matches_only_resolved_instrument_keys():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_LISTING)
    bridge.observe_listing(BANKNIFTY_LISTING)
    frame = BrokerPriceFrame(
        broker_id="upstox",
        levels=(
            BrokerPriceLevel(instrument_key="NSE_INDEX|Nifty 50", price=24500.0,
                              observed_at_ns=1_740_000_000_000_000_000),
            BrokerPriceLevel(instrument_key="NSE_INDEX|Nifty Bank", price=51000.0,
                              observed_at_ns=1_740_000_000_100_000_000),
            # An option contract on the same underlying -- must never be read
            # as the underlying's own price.
            BrokerPriceLevel(instrument_key="NSE_FO|36708", price=22.0,
                              observed_at_ns=1_740_000_000_200_000_000),
        ),
        published_at_ns=1_740_000_000_300_000_000, part_number=1, of_parts=1,
    )
    levels = bridge.levels_for(frame)
    assert len(levels) == 2
    assert {level.symbol for level in levels} == {"NIFTY", "BANKNIFTY"}
    assert bridge.standing.levels_matched == 2


def test_levels_for_returns_nothing_before_any_listing_resolved():
    bridge = _bridge()
    frame = BrokerPriceFrame(
        broker_id="upstox",
        levels=(BrokerPriceLevel(instrument_key="NSE_INDEX|Nifty 50", price=24500.0,
                                  observed_at_ns=1),),
        published_at_ns=2, part_number=1, of_parts=1,
    )
    assert bridge.levels_for(frame) == ()


def test_frame_for_returns_none_for_no_levels():
    assert _bridge().frame_for(()) is None


def test_frame_for_builds_a_real_symbol_price_frame_readable_by_the_existing_consumer():
    # Proves regime-classifier's own reader (runtime.price_frames.levels_in)
    # can actually consume what this part publishes -- the whole reason for
    # reusing SymbolPriceFrame instead of inventing a parallel shape.
    from runtime.price_frames import levels_in

    bridge = _bridge()
    bridge.observe_listing(NIFTY_LISTING)
    price_frame = BrokerPriceFrame(
        broker_id="upstox",
        levels=(BrokerPriceLevel(instrument_key="NSE_INDEX|Nifty 50", price=24500.0,
                                  observed_at_ns=999),),
        published_at_ns=1000, part_number=1, of_parts=1,
    )
    levels = bridge.levels_for(price_frame)
    frame = bridge.frame_for(levels)
    assert frame is not None
    assert frame.venue_id == "upstox"
    assert frame.was_split is False

    read_back = list(levels_in((frame,)))
    assert len(read_back) == 1
    assert read_back[0].symbol == "NIFTY"
    assert read_back[0].price == 24500.0
