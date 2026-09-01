"""Tests against Upstox's own documented sample payloads, fetched 2026-09-01
from upstox.com/developer/api-documentation/instruments and .../v3/get-market-data-feed
(cited per-test below), and against real protobuf wire encoding built from
the actual committed schema (runtime/brokers/upstox_market_data_feed.proto).
Never hand-invented field values for a shape Upstox hasn't published
somewhere.
"""

import json

from runtime.brokers import upstox_market_data_feed_pb2 as feed_pb2
from runtime.brokers.broker_adapter import SubscriptionMode, SubscriptionRequest
from runtime.brokers.upstox import UpstoxAdapter


def test_reads_equity_instrument_listing_from_upstox_own_sample():
    # Source: upstox.com/developer/api-documentation/instruments, "EQ" sample,
    # fetched 2026-09-01.
    response = [
        {
            "segment": "NSE_EQ",
            "name": "JOCIL LIMITED",
            "exchange": "NSE",
            "isin": "INE839G01010",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE839G01010",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "exchange_token": "16927",
            "tick_size": 5.0,
            "trading_symbol": "JOCIL",
            "short_name": "JOCIL",
            "security_type": "NORMAL",
            "cas_eligible": True,
        }
    ]
    adapter = UpstoxAdapter()
    listings = adapter.read_instrument_listings(response)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.instrument_key == "NSE_EQ|INE839G01010"
    assert listing.exchange == "NSE"
    assert listing.segment == "NSE_EQ"
    assert listing.instrument_type == "EQ"
    assert listing.lot_size == 1
    assert listing.tick_size == 5.0
    assert listing.expiry_ms is None
    assert listing.strike_price is None


def test_reads_option_instrument_listing_with_strike_and_expiry():
    # Source: same page, "Options" sample, fetched 2026-09-01.
    response = [
        {
            "weekly": False,
            "segment": "NSE_FO",
            "name": "VODAFONE IDEA LIMITED",
            "exchange": "NSE",
            "expiry": 1706207399000,
            "instrument_type": "CE",
            "underlying_symbol": "IDEA",
            "instrument_key": "NSE_FO|36708",
            "lot_size": 80000,
            "freeze_quantity": 1600000.0,
            "exchange_token": "36708",
            "minimum_lot": 80000,
            "underlying_key": "NSE_EQ|INE669E01016",
            "tick_size": 5.0,
            "underlying_type": "EQUITY",
            "trading_symbol": "IDEA 22 CE 25 JAN 24",
            "strike_price": 22.0,
        }
    ]
    adapter = UpstoxAdapter()
    listing = adapter.read_instrument_listings(response)[0]
    assert listing.instrument_type == "CE"
    assert listing.expiry_ms == 1706207399000
    assert listing.strike_price == 22.0
    assert listing.underlying_key == "NSE_EQ|INE669E01016"


def test_decodes_a_market_full_feed_ltpc_and_book_and_open_interest():
    # Built with the ACTUAL generated classes from the committed .proto --
    # real wire format, test-chosen field values (this is the honest boundary
    # of RL-063 before a live account exists: no captured tape yet).
    feed = feed_pb2.Feed()
    feed.fullFeed.marketFF.ltpc.ltp = 219.3
    feed.fullFeed.marketFF.ltpc.ltt = 1740729552723
    feed.fullFeed.marketFF.ltpc.ltq = 75
    feed.fullFeed.marketFF.ltpc.cp = 494.05
    feed.fullFeed.marketFF.marketLevel.bidAskQuote.add(bidQ=75, bidP=225.4, askQ=150, askP=225.7)
    feed.fullFeed.marketFF.oi = 256800
    feed.fullFeed.marketFF.vtt = 919725
    feed.fullFeed.marketFF.tbq = 100.0
    feed.fullFeed.marketFF.tsq = 50.0
    feed.requestMode = feed_pb2.full_d5

    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.live_feed
    response.currentTs = 1740729566039
    response.feeds["NSE_FO|45450"].CopyFrom(feed)
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert decoded.kind == "live_feed"
    assert decoded.broker_time_ns == 1740729566039 * 1_000_000
    assert len(decoded.ltp_updates) == 1
    assert decoded.ltp_updates[0].instrument_key == "NSE_FO|45450"
    assert decoded.ltp_updates[0].last_traded_price == 219.3
    assert len(decoded.book_updates) == 1
    assert decoded.book_updates[0].levels[0].bid_price == 225.4
    assert len(decoded.open_interest) == 1
    assert decoded.open_interest[0].open_interest == 256800


def test_decodes_a_market_info_message():
    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.market_info
    response.currentTs = 1732775008661
    response.marketInfo.segmentStatus["NSE_EQ"] = feed_pb2.NORMAL_OPEN
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert decoded.kind == "market_info"
    assert decoded.market_segment_status == {"NSE_EQ": "NORMAL_OPEN"}
    assert decoded.ltp_updates == ()


def test_decodes_index_full_feed_with_no_book_or_greeks():
    # IndexFullFeed carries only ltpc + OHLC -- indices aren't traded
    # directly, so there's no book or open interest to decode (the oneof's
    # second branch).
    feed = feed_pb2.Feed()
    feed.fullFeed.indexFF.ltpc.ltp = 24500.0
    feed.fullFeed.indexFF.ltpc.ltt = 1740729552723
    feed.fullFeed.indexFF.marketOHLC.ohlc.add(
        interval="1d", open=24400.0, high=24600.0, low=24350.0, close=24500.0,
        vol=0, ts=1740681000000,
    )
    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.live_feed
    response.feeds["NSE_INDEX|Nifty 50"].CopyFrom(feed)
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert len(decoded.ltp_updates) == 1
    assert decoded.ltp_updates[0].last_traded_price == 24500.0
    assert len(decoded.candles) == 1
    assert decoded.candles[0].is_closed is None  # never stated by this feed
    assert decoded.open_interest == ()
    assert decoded.book_updates == ()


def test_encode_subscribe_frame_is_json_bytes_with_one_mode():
    adapter = UpstoxAdapter()
    frame = adapter.encode_subscribe_frame([
        SubscriptionRequest(instrument_key="NSE_EQ|INE002A01018", mode=SubscriptionMode.FULL),
    ])
    decoded = json.loads(frame.decode("utf-8"))
    assert decoded["method"] == "sub"
    assert decoded["data"]["mode"] == "full_d5"
    assert decoded["data"]["instrumentKeys"] == ["NSE_EQ|INE002A01018"]
    assert "guid" in decoded


def test_encode_subscribe_frame_refuses_mixed_modes_in_one_call():
    import pytest

    adapter = UpstoxAdapter()
    with pytest.raises(ValueError):
        adapter.encode_subscribe_frame([
            SubscriptionRequest(instrument_key="A", mode=SubscriptionMode.FULL),
            SubscriptionRequest(instrument_key="B", mode=SubscriptionMode.LTPC),
        ])
