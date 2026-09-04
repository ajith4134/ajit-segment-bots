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
    # Upstox states 5.0 and means five paise. NSE's tick for a cash equity is
    # 0.05 rupees, and rupees is what every price on this feed is quoted in.
    assert listing.tick_size == 0.05
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
    assert listing.tick_size == 0.05


def test_a_declared_tick_matches_what_prices_on_the_real_master_actually_do():
    """The unit that cost every trade this segment tried to open.

    Read against the captured NSE instrument master for 2026-09-04. Every option
    row in it declares `tick_size` 5.0 -- including contracts trading under two
    rupees, which a five-rupee tick could not quote at all -- while the prices
    Upstox streams for those same contracts move in 0.01 to 0.05.

    Taken as rupees, `position-sizer` snapped an entry and its stop onto the same
    multiple of 5 and refused the result as a stop on the wrong side of its
    entry: 707 of 843 actionable intents, with both numbers printed identically.
    """
    import json
    import pathlib

    master = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[3]
            / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
        ).read_text()
    )
    options = [row for row in master if row.get("tick_size") is not None]
    assert options, "the captured master carries no tick_size to check"
    assert {row["tick_size"] for row in options} == {5.0}, "the master's own declaration"

    listings = UpstoxAdapter().read_instrument_listings(options)
    assert {listing.tick_size for listing in listings} == {0.05}

    # And the tick has to be able to express the cheapest contract in the file.
    cheapest = min(
        row["strike_price"] for row in options if row.get("strike_price")
    )
    assert cheapest > 0
    for listing in listings:
        assert listing.tick_size < 1.0


def test_an_absent_tick_stays_absent_rather_than_becoming_zero():
    """A zero would read as "no minimum increment" and be divided by."""
    from runtime.brokers.upstox import tick_size_in_rupees

    assert tick_size_in_rupees(None) is None


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
    assert decoded["data"]["mode"] == "full"
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


# ------------------------------------------------------------- order placement

from runtime.brokers.upstox import OrderRequest  # noqa: E402


def test_build_order_request_payload_matches_upstox_own_sample_shape():
    # Source: upstox.com/developer/api-documentation/place-order, request
    # sample, fetched 2026-09-01. Field name is instrument_token in this one
    # request body, not instrument_key -- Upstox's own inconsistency,
    # preserved rather than "fixed" here.
    order = OrderRequest(
        instrument_key="NSE_EQ|INE669E01016", quantity=1, product="D",
        order_type="MARKET", transaction_type="BUY", validity="DAY",
        price=0, tag="string", disclosed_quantity=0, trigger_price=0,
        is_amo=False, market_protection=0,
    )
    adapter = UpstoxAdapter()
    payload = adapter.build_order_request_payload(order)
    assert payload == {
        "quantity": 1, "product": "D", "validity": "DAY", "price": 0,
        "tag": "string", "instrument_token": "NSE_EQ|INE669E01016",
        "order_type": "MARKET", "transaction_type": "BUY",
        "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
        "market_protection": 0,
    }


def test_order_endpoint_is_the_low_latency_host():
    adapter = UpstoxAdapter()
    assert adapter.order_endpoint_url() == "https://api-hft.upstox.com/v2/order/place"


def test_read_order_result_from_upstox_own_sample():
    # Source: same page, response sample.
    response = {"status": "success", "data": {"order_id": "1644490272000"}}
    adapter = UpstoxAdapter()
    result = adapter.read_order_result(response)
    assert result.order_id == "1644490272000"


def test_read_order_result_refuses_a_failed_response():
    import pytest
    from runtime.brokers.upstox import OrderPlacementRefused

    response = {"status": "error", "errors": [{"errorCode": "UDAPI1026", "message": "x"}]}
    adapter = UpstoxAdapter()
    with pytest.raises(OrderPlacementRefused):
        adapter.read_order_result(response)


# --------------------------------------------------------------- margin quote

from runtime.brokers.upstox import MarginQuoteRequest  # noqa: E402


def test_margin_endpoint_is_the_regular_host():
    adapter = UpstoxAdapter()
    assert adapter.margin_endpoint_url() == "https://api.upstox.com/v2/charges/margin"


def test_build_margin_quote_request_payload_matches_upstox_own_sample_shape():
    # Source: upstox.com/developer/api-documentation/margin, request sample,
    # fetched 2026-09-01.
    requests = [
        MarginQuoteRequest(
            instrument_key="NSE_EQ|INE669E01016", quantity=1,
            transaction_type="BUY", product="D",
        )
    ]
    adapter = UpstoxAdapter()
    payload = adapter.build_margin_quote_request_payload(requests)
    assert payload == {
        "instruments": [{
            "instrument_key": "NSE_EQ|INE669E01016", "quantity": 1,
            "product": "D", "transaction_type": "BUY",
        }]
    }


def test_build_margin_quote_request_payload_refuses_more_than_twenty():
    import pytest

    requests = [
        MarginQuoteRequest(
            instrument_key=f"NSE_EQ|{i}", quantity=1,
            transaction_type="BUY", product="D",
        )
        for i in range(21)
    ]
    adapter = UpstoxAdapter()
    with pytest.raises(ValueError):
        adapter.build_margin_quote_request_payload(requests)


def test_stream_authorize_url_is_the_documented_v3_path():
    # upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3,
    # fetched 2026-09-02 (<endpoint-path>/feed/market-data-feed/authorize).
    adapter = UpstoxAdapter()
    assert adapter.stream_authorize_url() == (
        "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    )


def test_parse_authorized_stream_url_from_upstox_own_sample():
    # Real bug, 2026-09-02: broker-market-feed-reader connected straight to
    # a fixed wss:// URL with a Bearer header and never got a real
    # connection -- Upstox's V3 feed requires calling this authorize
    # endpoint first and connecting to the signed, single-use URL it
    # returns. Source: same doc page, its own 200 response sample.
    response = {
        "status": "success",
        "data": {
            "authorized_redirect_uri": (
                "wss://xyz.upstox.com/market-data-feeder/v3/upstox-developer-api/"
                "feeds?requestId=2f646f57-a097-4402-bb36-c44085c5f8e7&"
                "code=9355b100-25cf-4fa7-b038-06d27ddb4823"
            ),
        },
    }
    adapter = UpstoxAdapter()
    url = adapter.parse_authorized_stream_url(response)
    assert url.startswith("wss://xyz.upstox.com/market-data-feeder/v3/")
    assert "requestId=" in url and "code=" in url


def test_parse_authorized_stream_url_refuses_a_response_with_no_uri():
    import pytest

    adapter = UpstoxAdapter()
    with pytest.raises(ValueError):
        adapter.parse_authorized_stream_url({"status": "success", "data": {}})


def test_read_margin_quotes_from_upstox_own_sample():
    # Source: same page, EQ response sample, fetched 2026-09-01.
    response = {
        "status": "success",
        "data": {"margins": [{
            "span_margin": 0, "exposure_margin": 0, "equity_margin": 33.6,
            "net_buy_premium": 0, "additional_margin": 0,
        }]},
    }
    adapter = UpstoxAdapter()
    quotes = adapter.read_margin_quotes(response, instrument_keys=["NSE_EQ|INE669E01016"])
    assert quotes["NSE_EQ|INE669E01016"].equity_margin == 33.6
    assert quotes["NSE_EQ|INE669E01016"].span_margin == 0


# ---- historical candles ------------------------------------------------------
# Source: upstox.com/developer/api-documentation/v3/get-historical-candle-data,
# fetched 2026-09-02, plus one real response taken from the live API the same
# day for NSE_FO|42654 (NIFTY 24350 PE 08 SEP 26) over 2026-09-01.

def test_the_historical_candle_url_is_the_documented_v3_path():
    """Upstox's own documented order: to_date before from_date, and the
    instrument key percent-encoded because it contains a pipe."""
    url = UpstoxAdapter().historical_candle_url(
        instrument_key="NSE_FO|42654", unit="minutes", interval=1,
        from_date="2026-09-01", to_date="2026-09-02",
    )
    assert url == (
        "https://api.upstox.com/v3/historical-candle/"
        "NSE_FO%7C42654/minutes/1/2026-09-02/2026-09-01"
    )


def test_a_unit_upstox_does_not_publish_is_refused():
    """Guessing a unit name gets a 400 at best and silence at worst."""
    import pytest

    with pytest.raises(ValueError):
        UpstoxAdapter().historical_candle_url(
            instrument_key="NSE_FO|42654", unit="fortnights", interval=1,
            from_date="2026-09-01", to_date="2026-09-02",
        )


def test_reads_real_historical_candles_the_live_api_returned():
    """A real response, fetched 2026-09-02 for a live nearest-expiry contract.

    Two facts this pins, both of which have cost this project before: the rows
    arrive **newest first**, and the stamp is **+05:30**, not UTC. 15:39 IST is
    10:09 UTC -- a reader treating the stamp as UTC would place every bar of the
    Indian session outside it, which is the trap market-session-calendar was
    written against.
    """
    response = {
        "status": "success",
        "data": {
            "candles": [
                ["2026-09-01T15:39:00+05:30", 376.45, 376.45, 375.0, 375.95, 3835, 200395],
                ["2026-09-01T15:38:00+05:30", 375.45, 376.5, 373.85, 376.5, 1365, 200460],
                ["2026-09-01T15:37:00+05:30", 376.0, 376.7, 375.25, 375.45, 1040, 200460],
            ]
        },
    }
    candles = UpstoxAdapter().read_historical_candles(
        "NSE_FO|42654", "minutes", 1, response
    )

    assert [c.bar_time_ms for c in candles] == sorted(c.bar_time_ms for c in candles), (
        "Upstox returns newest first; a bar series read in that order is a series "
        "running backwards through time"
    )
    first = candles[0]
    assert first.instrument_key == "NSE_FO|42654"
    assert (first.open, first.high, first.low, first.close) == (376.0, 376.7, 375.25, 375.45)
    assert first.volume == 1040
    # 2026-09-01T15:37:00+05:30 is 2026-09-01T10:07:00Z, which is 1788257220000 ms.
    assert first.bar_time_ms == 1788257220000
    assert first.is_closed is True, (
        "a historical bar is closed by construction -- unlike the live feed's OHLC, "
        "which states nothing and is therefore None"
    )


def test_a_historical_response_that_failed_is_refused_rather_than_read_as_empty():
    """No candles and a failure are different facts; reading one as the other is
    how a feed that stopped looks like a market that went quiet."""
    import pytest

    with pytest.raises(ValueError):
        UpstoxAdapter().read_historical_candles(
            "NSE_FO|42654", "minutes", 1, {"status": "error", "errors": [{"message": "bad"}]}
        )
