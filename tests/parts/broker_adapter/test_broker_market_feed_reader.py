import json

import pytest

from parts.broker_adapter.broker_market_feed_reader import (
    fetch_authorized_stream_url, listing_key_of, plan_additional_subscriptions,
    only_what_the_segments_trade, plan_subscriptions,
    subscribe_the_universe_first,
)
from runtime.brokers.broker_adapter import (
    InstrumentListing, SubscriptionMode, SubscriptionRequest,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.bus import Message
from runtime.input_assembly import LatestByKey
from runtime.symbol_universe import CapturableSymbol

from tests.conftest import upstox_listings_by_key


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


def test_only_the_tracked_underlyings_and_their_nearest_expiry_chain_are_subscribed():
    """Real, confirmed 2026-09-02: plan_subscriptions accepts whatever fits the
    cap 'in listing order', and the catalogue's raw order has no relationship to
    what this project trades -- instrument-selector refused 14/14 trade-intents
    no-instrument-is-listed-for-this-symbol because no option contract for any
    tracked underlying was ever subscribed.

    Made a filter rather than an ordering on 2026-09-06, after the reordering
    alone proved not to be enough: measured on the real subscription, 1,707 of
    1,915 delivered instruments were options on SILVERM, MIDCPNIFTY, USDINR,
    GOLD and JPYINR -- nothing any segment trades -- and only 26 of them had
    their own underlying subscribed. A slot is spent for good: the connection
    caps at 2,000 and nothing evicts."""
    nifty = _index_listing("NSE_INDEX|Nifty 50", "NIFTY")
    other_index = _index_listing("NSE_INDEX|Nifty Fin Service", "FINNIFTY")  # untracked
    near_call = _option_listing("NSE_FO|NIFTY|near|CE", "NSE_INDEX|Nifty 50", expiry_ms=2000)
    near_put = _option_listing("NSE_FO|NIFTY|near|PE", "NSE_INDEX|Nifty 50", expiry_ms=2000)
    far_call = _option_listing("NSE_FO|NIFTY|far|CE", "NSE_INDEX|Nifty 50", expiry_ms=9000)
    unrelated = _listing("NSE_EQ|RANDOM")

    listings = (unrelated, far_call, near_put, other_index, nifty, near_call)
    wanted, left_to_the_universe = only_what_the_segments_trade(
        listings, tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    )

    # Underlyings only since 2026-09-12. The nearest-expiry contracts are real
    # and wanted, but this function holds no prices and so cannot rank them by
    # distance from the money; `broker-symbol-universe-bridge` does, and
    # `subscribe_the_universe_first` puts its ranked chains ahead of everything.
    # At the operator's 220-underlying universe the unranked remainder is 13,738
    # contracts against a 2,000-key connection that never evicts.
    assert {listing.instrument_key for listing in wanted} == {nifty.instrument_key}
    assert left_to_the_universe == 2, "both nearest-expiry contracts, counted not dropped silently"
    # The far expiry, the untracked index and the unrelated equity are not
    # deprioritized -- they are not subscribed at all, so their slots stay free
    # for the chain the bots are actually waiting on.
    assert len(wanted) == 1


def test_a_tracked_ordinary_share_gets_its_chain_too_not_only_an_index():
    """The INDEX-only filter of 2026-09-02 was written when index options were
    the only segment. `underlyings_every_built_segment_trades` returns 3 indices
    and 14 ordinary shares shared by stock-options and cash-equity-intraday, and
    the filter silently dropped all 14 -- so the stock-options bot's chains were
    never prioritised and cash equity never got its spot prices. Measured
    against the real master: INDEX-only selects 3 underlyings and 723 contracts;
    every tracked type selects 15 and 1,528."""
    reliance = _listing("NSE_EQ|INE002A01018")
    reliance = InstrumentListing(
        instrument_key="NSE_EQ|INE002A01018", exchange="NSE", segment="NSE_EQ",
        instrument_type="EQ", trading_symbol="RELIANCE", lot_size=1, tick_size=0.05,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    call = _option_listing("NSE_FO|RELIANCE|near|CE", "NSE_EQ|INE002A01018", expiry_ms=2000)

    wanted, left_to_the_universe = only_what_the_segments_trade(
        (reliance, call), tracked_trading_symbols=("NIFTY", "RELIANCE"), now_ms=1000,
    )
    # The share itself, whatever its instrument type -- which is what this test
    # has always been about. Its chain now comes from the universe, ranked.
    assert {listing.instrument_key for listing in wanted} == {reliance.instrument_key}
    assert left_to_the_universe == 1


def test_the_same_name_on_two_admitted_segments_keeps_both_listings():
    """A symbol-keyed dict kept whichever row came last and silently dropped the
    other listing's chain.

    Real names, read off the master 2026-09-16: MID150, ENERGY, INFRA and METAL
    are each a BSE index *and* an NSE share. Both are things an option can
    settle against, so both have to survive the lookup -- this is what a set of
    keys buys that a dict keyed by trading symbol does not. It is a different
    case from one share listed on two exchanges, which
    `test_a_shares_second_exchange_is_not_subscribed_as_an_underlying` covers:
    there the two rows are the same asset and only one carries a chain.
    """
    share = InstrumentListing(
        instrument_key="NSE_EQ|INE00WC01019", exchange="NSE", segment="NSE_EQ",
        instrument_type="EQ", trading_symbol="ENERGY", lot_size=1, tick_size=0.05,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    index = InstrumentListing(
        instrument_key="BSE_INDEX|ENERGY", exchange="BSE", segment="BSE_INDEX",
        instrument_type="INDEX", trading_symbol="ENERGY", lot_size=None, tick_size=None,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    share_call = _option_listing("NSE_FO|E|CE", "NSE_EQ|INE00WC01019", expiry_ms=2000)
    index_call = _option_listing("BSE_FO|E|CE", "BSE_INDEX|ENERGY", expiry_ms=2000)

    wanted, left_to_the_universe = only_what_the_segments_trade(
        (share, index, share_call, index_call),
        tracked_trading_symbols=("ENERGY",), now_ms=1000,
    )
    assert {listing.instrument_key for listing in wanted} == {
        share.instrument_key, index.instrument_key,
    }
    assert left_to_the_universe == 2


def test_no_option_the_broker_lists_settles_against_a_shares_second_exchange():
    """The measurement the exchange filter rests on, re-read from the master.

    `UNDERLYING_EXCHANGE_SEGMENTS` drops a share's BSE line because no option
    settles against it. That is a fact about the broker's master, not about this
    code, so it is asserted against the master itself: if NSE or BSE ever lists
    a stock option settling against a `BSE_EQ` key, this fails and the filter is
    what has to change (RL-063 -- a fixture here would only restate the belief).
    """
    listings = upstox_listings_by_key()
    if not listings:
        pytest.skip("no instrument master on this machine to read")
    options = [
        listing for listing in listings.values()
        if listing.instrument_type in ("CE", "PE") and listing.underlying_key
    ]
    assert options, "the master carries no option listings at all"
    settling_against_a_second_exchange = [
        listing for listing in options
        if listing.underlying_key.startswith("BSE_EQ|")
    ]
    assert settling_against_a_second_exchange == []
    assert any(
        listing.underlying_key.startswith("NSE_EQ|") for listing in options
    ), "no stock option settles against NSE_EQ either -- the master is not what this assumes"


def test_an_expired_contract_is_excluded_not_merely_deprioritized():
    """A same-day-expired contract is real, current data and would otherwise
    still win a nearest-expiry comparison against tomorrow's real chain."""
    nifty = _index_listing("NSE_INDEX|Nifty 50", "NIFTY")
    expired_call = _option_listing("NSE_FO|NIFTY|expired|CE", "NSE_INDEX|Nifty 50", expiry_ms=500)
    live_call = _option_listing("NSE_FO|NIFTY|live|CE", "NSE_INDEX|Nifty 50", expiry_ms=5000)

    wanted, left_to_the_universe = only_what_the_segments_trade(
        (expired_call, nifty, live_call),
        tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    )
    assert {listing.instrument_key for listing in wanted} == {nifty.instrument_key}
    # The live one is left to the universe; the expired one is not counted at
    # all, because it is excluded rather than deprioritized.
    assert left_to_the_universe == 1


def test_nothing_is_subscribed_before_a_tracked_listing_has_arrived():
    """The behaviour this deliberately changed on 2026-09-06. It used to pass
    everything through, which is how the connection filled with instruments no
    segment trades before the real chain had even been delivered. An empty
    result is correct: the feed still connects on `symbol-universe`, which
    `subscribe_the_universe_first` puts ahead of this, and an empty slot can
    still be filled while a wrongly-spent one cannot be recovered."""
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(5))
    assert only_what_the_segments_trade(
        listings, tracked_trading_symbols=("NIFTY", "BANKNIFTY", "SENSEX"), now_ms=1000,
    ) == ((), 0)


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


def test_a_full_connection_gives_up_what_left_the_universe_for_what_joined_it():
    """The subscription only ever grew, so a full connection could never take a newcomer.

    2026-09-15: the feed filled all 2,000 keys at connect, before any implied volatility
    existed and so before broker-symbol-universe-bridge had chosen NIFTY's expiry-day far
    strikes. The bridge then published ten of them -- and six stock contracts yielded their
    keys for them -- but the connection stayed full of what it first took, and not one far
    strike was ever recorded.
    """
    from parts.broker_adapter.broker_market_feed_reader import plan_evictions

    full = SubscriptionMode.FULL
    subscribed = tuple(SubscriptionRequest(f"NSE_FO|{n}", full) for n in range(10))
    # Two stock contracts left the universe; three far strikes joined it.
    universe = [f"NSE_FO|{n}" for n in range(8)] + ["NSE_FO|far-1", "NSE_FO|far-2", "NSE_FO|far-3"]

    evicted = plan_evictions(subscribed, universe, capacity=10)
    assert {one.instrument_key for one in evicted} == {"NSE_FO|8", "NSE_FO|9"}

    # With room, nothing is evicted; nothing in the universe is ever evicted.
    assert plan_evictions(subscribed, universe, capacity=13) == ()
    everything = [one.instrument_key for one in subscribed] + ["NSE_FO|far-1"]
    assert plan_evictions(subscribed, everything, capacity=10) == ()


def test_a_shares_second_exchange_is_not_subscribed_as_an_underlying():
    """One share, two listings, and only one of them is what an option settles
    against.

    Real, measured on the instrument master 2026-09-16: all 27,012 NSE_FO
    stock-option contracts name an `NSE_EQ` underlying key and none names a
    `BSE_EQ` one. Both lines were subscribed anyway, and every bridge names an
    instrument by its `trading_symbol`, so the two exchanges' prints for one
    share were published as one symbol -- 14 shares that day, disagreeing by
    0.02%-0.09% at the median and up to 0.33%, with the BSE line carrying more
    of the prints than the NSE line for three of them.
    """
    nse = InstrumentListing(
        instrument_key="NSE_EQ|INE002A01018", exchange="NSE", segment="NSE_EQ",
        instrument_type="EQ", trading_symbol="RELIANCE", lot_size=1, tick_size=0.05,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    bse = InstrumentListing(
        instrument_key="BSE_EQ|INE002A01018", exchange="BSE", segment="BSE_EQ",
        instrument_type="EQ", trading_symbol="RELIANCE", lot_size=1, tick_size=0.05,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    chain = _option_listing(
        "NSE_FO|RELIANCE|near|CE", "NSE_EQ|INE002A01018", expiry_ms=2000, strike=1400.0,
    )

    wanted, _left_to_the_universe = only_what_the_segments_trade(
        (bse, nse, chain), tracked_trading_symbols=("RELIANCE",), now_ms=1000,
    )

    assert [listing.instrument_key for listing in wanted] == ["NSE_EQ|INE002A01018"]


def test_an_index_listed_on_either_exchange_is_still_an_underlying():
    """The filter is about a share's second exchange, not about BSE.

    BSE writes options on its own indices (4,170 contracts on the 2026-09-16
    master, every one naming a `BSE_INDEX` underlying), and `index-options`
    derives its universe from every index with an option. Dropping BSE outright
    would have taken those chains' underlyings with it.
    """
    sensex = InstrumentListing(
        instrument_key="BSE_INDEX|SENSEX", exchange="BSE", segment="BSE_INDEX",
        instrument_type="INDEX", trading_symbol="SENSEX", lot_size=None, tick_size=None,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )

    wanted, _left = only_what_the_segments_trade(
        (sensex,), tracked_trading_symbols=("SENSEX",), now_ms=1000,
    )

    assert [listing.instrument_key for listing in wanted] == ["BSE_INDEX|SENSEX"]
