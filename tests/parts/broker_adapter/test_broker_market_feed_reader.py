import json

from parts.broker_adapter.broker_market_feed_reader import (
    fetch_authorized_stream_url, listing_key_of, plan_additional_subscriptions,
    plan_subscriptions, prioritize_index_option_chain,
    subscribe_the_universe_first,
)
from runtime.brokers.broker_adapter import (
    InstrumentListing, SubscriptionMode, SubscriptionRequest,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.bus import Message
from runtime.input_assembly import LatestByKey
from runtime.symbol_universe import CapturableSymbol


def _listing(key: str) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_EQ", instrument_type="EQ",
        trading_symbol=key, lot_size=1, tick_size=0.05, freeze_quantity=None,
        expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _index_listing(key: str, trading_symbol: str) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_INDEX", instrument_type="INDEX",
        trading_symbol=trading_symbol, lot_size=None, tick_size=None, freeze_quantity=None,
        expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )


def _option_listing(
    key: str, underlying_key: str, expiry_ms: int, strike: float = 20000.0,
) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_FO", instrument_type="CE",
        trading_symbol=key, lot_size=50, tick_size=0.05, freeze_quantity=None,
        expiry_ms=expiry_ms, strike_price=strike, underlying_key=underlying_key,
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


def test_prioritize_index_option_chain_puts_tracked_underlyings_and_their_nearest_expiry_chain_first():
    """Real, confirmed 2026-09-02: plan_subscriptions/plan_additional_subscriptions
    accept whatever fits the cap 'in listing order' (this file's own docstrings),
    and broker-instrument-catalogue-reader's raw listing order has no relationship
    to what this project actually trades -- of 101,393 real Upstox listings, the
    first 2000 in catalogue order essentially never include the NIFTY/BANKNIFTY/
    SENSEX option chain instrument-selector needs a delta for. Confirmed live:
    instrument-selector refused 14/14 trade-intents no-instrument-is-listed-for-
    this-symbol, symbols_with_listed_instruments stuck at 0, because no option
    contract for any tracked underlying was ever subscribed at all -- so
    broker-option-greeks never carried a delta and AtmStrikeTracker.atm_call_for
    could never resolve. This reorders listings so the tracked underlyings and
    their nearest-expiry chain come first, before the cap is ever reached."""
    nifty = _index_listing("NSE_INDEX|Nifty 50", "NIFTY")
    other_index = _index_listing("NSE_INDEX|Nifty Fin Service", "FINNIFTY")  # untracked
    near_call = _option_listing("NSE_FO|NIFTY|near|CE", "NSE_INDEX|Nifty 50", expiry_ms=2000)
    near_put = _option_listing("NSE_FO|NIFTY|near|PE", "NSE_INDEX|Nifty 50", expiry_ms=2000)
    far_call = _option_listing("NSE_FO|NIFTY|far|CE", "NSE_INDEX|Nifty 50", expiry_ms=9000)
    unrelated = _listing("NSE_EQ|RANDOM")

    listings = (unrelated, far_call, near_put, other_index, nifty, near_call)
    ordered = prioritize_index_option_chain(
        listings, tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    )

    priority_keys = {listing.instrument_key for listing in ordered[:3]}
    assert priority_keys == {nifty.instrument_key, near_call.instrument_key, near_put.instrument_key}
    # far expiry, the untracked index, and the unrelated equity all land after the priority set
    remaining_keys = [listing.instrument_key for listing in ordered[3:]]
    assert set(remaining_keys) == {far_call.instrument_key, other_index.instrument_key, unrelated.instrument_key}
    # nothing lost, nothing duplicated
    assert len(ordered) == len(listings)
    assert len(set(listing.instrument_key for listing in ordered)) == len(listings)


def test_prioritize_index_option_chain_excludes_expired_contracts():
    nifty = _index_listing("NSE_INDEX|Nifty 50", "NIFTY")
    expired_call = _option_listing("NSE_FO|NIFTY|expired|CE", "NSE_INDEX|Nifty 50", expiry_ms=500)
    live_call = _option_listing("NSE_FO|NIFTY|live|CE", "NSE_INDEX|Nifty 50", expiry_ms=5000)

    ordered = prioritize_index_option_chain(
        (expired_call, nifty, live_call),
        tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    )
    priority_keys = {listing.instrument_key for listing in ordered[:2]}
    assert priority_keys == {nifty.instrument_key, live_call.instrument_key}
    assert ordered[2].instrument_key == expired_call.instrument_key


def test_prioritize_index_option_chain_is_a_noop_when_nothing_is_tracked_yet():
    """Before any broker-instrument-listing has arrived for a tracked index
    (real state on first connect), the function must not crash or drop
    listings -- everything just passes through in its original order."""
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(5))
    ordered = prioritize_index_option_chain(
        listings, tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    )
    assert ordered == listings


def test_plan_additional_subscriptions_skips_what_is_already_subscribed():
    """The real 2026-09-02 gap: ensure_connected() only plans once, at first
    connect, and never revisits instrument_listings again -- 4 real
    instruments got locked in forever while the other ~101,389 arrived too
    late to matter. This is the periodic top-up: given what is already
    subscribed, only listings not yet covered should come back, and existing
    ones must never be resent."""
    adapter = UpstoxAdapter()
    existing = (
        SubscriptionRequest(instrument_key="NSE_EQ|0", mode=SubscriptionMode.FULL),
        SubscriptionRequest(instrument_key="NSE_EQ|1", mode=SubscriptionMode.FULL),
    )
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(5))
    additional = plan_additional_subscriptions(
        adapter, existing, listings, mode=SubscriptionMode.FULL,
    )
    assert {request.instrument_key for request in additional} == {
        "NSE_EQ|2", "NSE_EQ|3", "NSE_EQ|4",
    }


def test_plan_additional_subscriptions_stops_at_the_cap_counting_what_already_holds_a_slot():
    adapter = UpstoxAdapter()
    existing = tuple(
        SubscriptionRequest(instrument_key=f"NSE_EQ|{i}", mode=SubscriptionMode.FULL)
        for i in range(1998)
    )
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(2500))  # includes the 1998 existing
    additional = plan_additional_subscriptions(
        adapter, existing, listings, mode=SubscriptionMode.FULL,
    )
    assert len(additional) == 2  # 2000 individual cap - 1998 already held


def test_plan_additional_subscriptions_is_empty_when_nothing_grew():
    adapter = UpstoxAdapter()
    existing = tuple(
        SubscriptionRequest(instrument_key=f"NSE_EQ|{i}", mode=SubscriptionMode.FULL)
        for i in range(5)
    )
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(5))  # identical set
    additional = plan_additional_subscriptions(
        adapter, existing, listings, mode=SubscriptionMode.FULL,
    )
    assert additional == ()


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


def _universe_entry(venue_instrument_id: str, symbol: str) -> CapturableSymbol:
    return CapturableSymbol(
        venue_id="upstox", symbol=symbol, contract_type="INDEX",
        quote_volume_24h=None, price_increment=None,
        venue_instrument_id=venue_instrument_id,
    )


def test_the_universe_is_subscribed_before_anything_the_catalogue_race_happened_to_deliver():
    """The three index rows must not have to win a race against 102,940 messages.

    Measured 2026-09-04: broker-instrument-catalogue-reader restates all 102,940
    listings into a 212,992-byte inbox, and this part received 14,560 of them --
    the three index underlyings the segment is entirely about were not among
    them, twice running. `symbol-universe` is the bounded, already-selected set,
    and broker-symbol-universe-bridge receives the whole catalogue with zero
    input loss because its tick is cheap. Subscribing that first is what makes
    the underlyings certain rather than lucky.

    The listings still fill the rest of the connection: this adds a guarantee,
    it does not narrow what the tape records.
    """
    universe = (_universe_entry("NSE_INDEX|Nifty 50", "NIFTY"),)
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(2500))

    ordered = subscribe_the_universe_first(universe, listings)

    assert ordered[0].instrument_key == "NSE_INDEX|Nifty 50"
    # And the catalogue still follows it, rather than being replaced by it.
    assert len(ordered) == 2501


def test_an_instrument_in_both_the_universe_and_the_catalogue_is_subscribed_once():
    universe = (_universe_entry("NSE_EQ|7", "SEVEN"),)
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(10))

    ordered = subscribe_the_universe_first(universe, listings)

    keys = [item.instrument_key for item in ordered]
    assert keys[0] == "NSE_EQ|7"
    assert keys.count("NSE_EQ|7") == 1
    assert len(keys) == 10


def test_a_universe_entry_with_no_venue_instrument_id_is_skipped_not_guessed():
    """A crypto producer of this type names no venue_instrument_id at all.

    Subscribing to the trading symbol instead would send Upstox a key it does
    not recognise, and the whole subscribe frame is one message -- one bad key
    is not one lost instrument.
    """
    nameless = CapturableSymbol(
        venue_id="upstox", symbol="NIFTY", contract_type="INDEX",
        quote_volume_24h=None, price_increment=None,
    )

    ordered = subscribe_the_universe_first((nameless,), (_listing("NSE_EQ|1"),))

    assert [item.instrument_key for item in ordered] == ["NSE_EQ|1"]


def test_the_universe_alone_is_enough_before_any_listing_has_arrived():
    """The bootstrap: the bridge publishes the underlyings without needing a
    price, so this part can subscribe them before the catalogue race resolves."""
    universe = (_universe_entry("NSE_INDEX|Nifty 50", "NIFTY"),)

    ordered = subscribe_the_universe_first(universe, ())

    assert [item.instrument_key for item in ordered] == ["NSE_INDEX|Nifty 50"]
