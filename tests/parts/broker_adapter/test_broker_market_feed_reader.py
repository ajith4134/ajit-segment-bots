import json

from parts.broker_adapter.broker_market_feed_reader import (
    fetch_authorized_stream_url, listing_key_of, plan_subscriptions,
)
from runtime.brokers.broker_adapter import InstrumentListing, SubscriptionMode
from runtime.brokers.upstox import UpstoxAdapter
from runtime.bus import Message
from runtime.input_assembly import LatestByKey


def _listing(key: str) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_EQ", instrument_type="EQ",
        trading_symbol=key, lot_size=1, tick_size=0.05, freeze_quantity=None,
        expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )


def test_plan_subscriptions_stops_at_the_full_mode_individual_limit():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(2500))  # over the 2000 individual cap
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 2000


def test_plan_subscriptions_covers_every_listing_when_under_the_cap():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(180))
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 180


def test_the_live_spine_crashed_on_this_2026_09_02():
    """instrument_listings' LatestByKey used to key every listing by
    adapter.broker_id -- a constant, not a field of the message -- so all
    101,393 real listings from broker-instrument-catalogue-reader collapsed
    into one overwritten entry instead of one per instrument.
    ensure_connected() then handed that single InstrumentListing (not a
    collection of them) to plan_subscriptions, which crashed the moment it
    tried `for listing in listings` on the live spine the first time a real
    token let it get this far. listing_key_of must key by the field that
    actually identifies which instrument a listing is about."""
    assert listing_key_of(_listing("NSE_FO|1001")) == "NSE_FO|1001"
    assert listing_key_of(_listing("NSE_INDEX|Nifty 50")) == "NSE_INDEX|Nifty 50"


def test_latest_by_key_keeps_every_instrument_not_just_the_last_one():
    """The real regression, reproduced with the actual LatestByKey shape
    start_part builds -- not just the key function in isolation. With the
    old constant key_of, this collapsed to a mapping of length 1."""
    listings = [_listing(f"NSE_EQ|{i}") for i in range(50)]
    messages = tuple(
        Message(
            data_type="broker-instrument-listing", producer_part_id="test",
            sequence=index, published_at_ns=index, payload=listing,
        )
        for index, listing in enumerate(listings)
    )
    tracker = LatestByKey(read=lambda: messages, key_of=listing_key_of)
    mapping = tracker.mapping()
    assert len(mapping) == 50
    assert all(mapping[listing.instrument_key] is listing for listing in listings)


def test_fetch_authorized_stream_url_sends_the_bearer_token_and_returns_the_real_url():
    """Real bug, 2026-09-02: this part used to connect straight to
    stream_endpoint_url() with a Bearer header -- Upstox's V3 feed needs
    this authorize call first, connecting to the signed URL it returns."""
    adapter = UpstoxAdapter()
    sent_calls = []

    def fake_fetch(url, headers):
        sent_calls.append((url, headers))
        return json.dumps({
            "status": "success",
            "data": {"authorized_redirect_uri": "wss://xyz.upstox.com/feeds?code=abc"},
        }).encode()

    url = fetch_authorized_stream_url(adapter, "the-access-token", fetch=fake_fetch)
    assert url == "wss://xyz.upstox.com/feeds?code=abc"
    assert len(sent_calls) == 1
    sent_url, sent_headers = sent_calls[0]
    assert sent_url == "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    assert sent_headers["Authorization"] == "Bearer the-access-token"
    assert sent_headers["Accept"] == "application/json"


def test_fetch_authorized_stream_url_default_fetch_uses_curl_cffi_not_urllib():
    """Real bug, 2026-09-02: stdlib urllib.request got a Cloudflare 403
    ("Error 1010: browser_signature_banned") on this exact endpoint --
    verified against the real API, not a hypothesis. upstox_totp (already
    a dependency, already proven against this same Cloudflare front for
    the login flow) uses curl_cffi's Chrome impersonation; this call needs
    the same technique. Import-level check rather than a live network
    call: proves the fix uses the right library without spending a real
    request against Upstox."""
    import inspect

    source = inspect.getsource(fetch_authorized_stream_url)
    assert "curl_cffi" in source
    assert "impersonate" in source
