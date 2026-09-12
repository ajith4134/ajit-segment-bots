"""broker-market-feed-reader: stream a broker's feed, decomposed into this
project's own record kinds (spec section 5).

WebSocket connection, protobuf-decoded via the adapter, one bundled message
per instrument split into up to five separate record kinds before
publishing -- never republished as one wire carrying several shapes
(docs/proposals/upstox-broker-adapter.md, same reasoning that split
candle/market-data/order-book-snapshot apart for the crypto build).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from runtime.brokers.broker_adapter import (
    BrokerAdapter, BrokerSubscriptionState, DecodedFeedMessage, InstrumentListing,
    SubscriptionMode, SubscriptionRequest,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-market-feed-reader"


def fetch_authorized_stream_url(
    adapter, access_token: str, timeout_seconds: float = 15.0,
    fetch=None,
) -> str:
    """The real, signed wss:// URL to connect to -- adapter.stream_endpoint_url()
    is not one; see UpstoxAdapter.stream_authorize_url's own docstring for
    why this call exists at all.

    curl_cffi with Chrome impersonation, not stdlib urllib -- real bug,
    2026-09-02: urllib.request got a Cloudflare 403 ("Error 1010:
    browser_signature_banned", not an Upstox auth error at all) on this
    exact endpoint. upstox_totp (already a dependency, already proven
    against this same api.upstox.com Cloudflare front for the login flow)
    uses curl_cffi's browser impersonation for exactly this reason; this
    call needed the same technique, not a new one.
    """
    def default_fetch(url, headers):
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            url, headers=headers, impersonate="chrome131", timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.content

    body = (fetch or default_fetch)(
        adapter.stream_authorize_url(),
        {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    return adapter.parse_authorized_stream_url(json.loads(body))

PART_DECLARATION = PartDeclaration(
    part_id="broker-market-feed-reader",
    # In the blueprint's own order: code follows the registry, never the other
    # way round, and this part's declaration had silently disagreed with it
    # since the universe input was added -- no test compared the two for this
    # block until 2026-09-05.
    consumes=("broker-instrument-listing", "broker-token-standing", "symbol-universe"),
    produces=(
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks",
        # What it actually subscribed. Only this part knows: it takes the
        # universe first and fills the rest of the connection from the master,
        # under the adapter's own cap, so nothing downstream can re-derive the
        # set without being free to disagree with it.
        "broker-subscription-state", "part-health",
    ),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


def listing_key_of(listing: InstrumentListing) -> str:
    """One key per instrument, not per broker.

    Real bug, 2026-09-02: start_part used to key instrument_listings'
    LatestByKey by `adapter.broker_id` -- a constant, ignoring the message
    entirely -- so every one of the 101,393 real listings from
    broker-instrument-catalogue-reader overwrote the same single entry.
    ensure_connected() then handed plan_subscriptions one InstrumentListing
    instead of a collection of them, and `for listing in listings` crashed
    the live spine the first time a real token let this part get that far.
    """
    return listing.instrument_key


# What a tracked underlying is listed as in the broker's own master: an index for
# an index-options chain, an ordinary share for a stock-options chain and for
# cash equity. Never a future or an option -- those are the things that HAVE an
# underlying, and treating one as an underlying would subscribe a chain on a
# contract instead of on the thing it settles against.
UNDERLYING_INSTRUMENT_TYPES = ("INDEX", "EQ")


def only_what_the_segments_trade(
    listings: Sequence[InstrumentListing],
    tracked_trading_symbols: Sequence[str],
    now_ms: int,
) -> tuple[InstrumentListing, ...]:
    """The tracked underlyings and their nearest-expiry option chains, nothing else.

    A **filter**, not just an ordering, and that is the change of 2026-09-06.
    Until then this reordered the catalogue and handed the whole of it to
    `plan_subscriptions`, which takes whatever fits the cap in listing order --
    so every slot the priority set did not claim was spent on whatever happened
    to be next in `broker-instrument-catalogue-reader`'s raw order, which has no
    relationship to what this project trades.

    Measured on the real subscription of 2026-09-06 -- 1,915 instruments the
    feed actually delivered, 1,707 of them option contracts:

        underlying     contracts   its own price subscribed
        SILVERM              511   no
        MIDCPNIFTY           205   no
        USDINR               176   no
        GOLD                 121   no
        JPYINR                67   no
        options whose underlying is also subscribed:  26 of 1,707

    Silver, a mid-cap index, two currency pairs and gold. Not one of them is
    something any of the three segment bots trades, and NIFTY -- the
    index-options bot's entire chain -- was not in the top ten. The consequences
    ran all the way down: `implied-vol-reader` could assemble 8,037 option
    quotes and publish no surface at all, because no underlying it held had a
    spot to read at-the-money against.

    **A slot spent on the wrong instrument is spent for good.** The connection
    caps at 2,000 and nothing evicts, so once the catalogue race filled it there
    was no room left for the chain the bots were waiting on, however long the
    connection stayed open or however many growth checks ran. An empty slot
    costs nothing and can still be filled; a wrong one cannot be recovered. So
    this returns only what a segment actually trades and lets the rest of the
    connection stay empty.

    **Every tracked underlying, whatever its instrument type.** This filtered on
    `instrument_type == "INDEX"` from 2026-09-02, when index options were the
    only segment. `underlyings_every_built_segment_trades` returns the union
    across every built segment -- 3 indices for index-options and 14 ordinary
    shares shared by stock-options and cash-equity-intraday -- and the INDEX
    filter silently dropped all 14, so the stock-options bot's chains were never
    prioritised at all and neither were the spot prices cash-equity needs.
    Measured against the real master: INDEX-only selects 3 underlyings and 723
    nearest-expiry contracts; every tracked type selects 15 and 1,528, which
    with the ~40-instrument universe still fits the 2,000 cap with room to
    spare.

    Nearest expiry only (spec section 2), computed here rather than assumed from
    listing order, since Upstox's own catalogue is not expiry-sorted. An expiry
    that has already passed is dropped rather than deprioritized -- a
    same-day-expired contract is real, current data and would otherwise still
    win a nearest-expiry comparison against tomorrow's real chain.

    **Underlyings only since 2026-09-12, and the contracts come from the
    universe instead.** This returned every nearest-expiry contract of every
    tracked underlying, unranked, and `plan_subscriptions` took whatever fit in
    listing order. At 3 indices and 14 shares that was 1,528 contracts against a
    2,000-key connection and the slack absorbed it. The operator's universe of
    that day -- every index NSE or BSE writes options on, and every one of the
    210 shares an option is written on -- makes it **13,738 nearest-expiry
    contracts**, and listing order has no relationship to what is worth
    subscribing.

    This function cannot rank them, because ranking is distance from the money
    and it holds no prices. `broker-symbol-universe-bridge` does hold them, and
    already publishes each underlying's chain capped and sorted nearest the
    money first. So the honest division of labour is: this selects the
    underlyings, whose prices are what make a ranking possible at all, and
    `subscribe_the_universe_first` supplies the ranked contracts ahead of
    everything else.

    The cost is that no option is subscribed until the bridge has priced its
    underlying, which is the bootstrap the caller already documents. The
    alternative is worse and has already happened: **a slot spent on the wrong
    instrument is spent for good**, because the connection caps at 2,000 and
    nothing evicts. An empty slot costs nothing and can still be filled.

    Earlier evidence this exists at all, 2026-09-02: with the raw catalogue
    order, `instrument-selector` refused 14 of 14 trade-intents
    no-instrument-is-listed-for-this-symbol and `symbols_with_listed_instruments`
    stayed at 0 -- no option contract for any tracked underlying was ever
    subscribed, so `broker-option-greeks` never carried a delta.
    """
    tracked = set(tracked_trading_symbols)
    # A set of keys rather than a symbol-keyed dict: the same name is listed on
    # more than one exchange (NSE_EQ and BSE_EQ both list RELIANCE), and a dict
    # would keep whichever came last and silently drop the other's chain.
    tracked_underlying_keys = {
        listing.instrument_key
        for listing in listings
        if listing.trading_symbol in tracked
        and listing.instrument_type in UNDERLYING_INSTRUMENT_TYPES
    }

    nearest_expiry_by_underlying: dict[str, int] = {}
    for listing in listings:
        if (
            listing.underlying_key in tracked_underlying_keys
            and listing.expiry_ms is not None
            and listing.expiry_ms > now_ms
        ):
            current = nearest_expiry_by_underlying.get(listing.underlying_key)
            if current is None or listing.expiry_ms < current:
                nearest_expiry_by_underlying[listing.underlying_key] = listing.expiry_ms

    wanted: list[InstrumentListing] = []
    contracts_dropped = 0
    for listing in listings:
        is_tracked_underlying = listing.instrument_key in tracked_underlying_keys
        is_nearest_expiry_option = listing.expiry_ms is not None and listing.expiry_ms == (
            nearest_expiry_by_underlying.get(listing.underlying_key)
        )
        if is_tracked_underlying:
            wanted.append(listing)
        elif is_nearest_expiry_option:
            contracts_dropped += 1
    return tuple(wanted), contracts_dropped


def state_of_the_subscription(
    broker_id: str,
    subscribed: Sequence[SubscriptionRequest],
    observed_at_ns: int,
) -> BrokerSubscriptionState:
    """What this connection carries right now, as one level.

    Every instrument, not the newly added ones: a reader that started after the
    connection opened has to be able to learn the whole set, and a part that is
    told only about additions can never learn about the ones it missed.
    """
    return BrokerSubscriptionState(
        broker_id=broker_id,
        instrument_keys=tuple(request.instrument_key for request in subscribed),
        observed_at_ns=observed_at_ns,
    )


@dataclass(frozen=True)
class SubscribableInstrument:
    """Something to subscribe to, reduced to the only field planning reads.

    `plan_subscriptions` and `plan_additional_subscriptions` read
    `.instrument_key` and nothing else, so what they order can come from the
    instrument master or from `symbol-universe` without either having to know
    about the other.
    """

    instrument_key: str


def subscribe_the_universe_first(
    universe: Sequence, listings: Sequence[InstrumentListing],
) -> tuple[SubscribableInstrument, ...]:
    """The selected universe ahead of whatever the catalogue race delivered.

    `only_what_the_segments_trade` can only select what already arrived, and
    what arrives is a race this part loses: `broker-instrument-catalogue-reader`
    restates all 102,940 listings into a 212,992-byte inbox that holds a few
    hundred messages. Measured on the live spine 2026-09-04, after the per-tick
    drain raised intake from 1,067 to 14,560 listings, the three index
    underlyings the segment is entirely about were still not among them.

    `symbol-universe` is the bounded, already-selected set, and
    `broker-symbol-universe-bridge` builds it from the whole catalogue with zero
    input loss -- its tick is cheap, so it never falls behind. Taking it first
    is what makes the underlyings certain instead of lucky.

    The catalogue still fills the rest of the connection behind it. This adds a
    guarantee about what is definitely subscribed; it does not narrow what the
    tape records.

    A universe entry naming no `venue_instrument_id` is skipped rather than
    subscribed by its trading symbol: a subscribe frame is one message, so one
    key the venue does not recognise is not one lost instrument.
    """
    ordered: list[SubscribableInstrument] = []
    seen: set[str] = set()
    for entry in universe:
        key = getattr(entry, "venue_instrument_id", None)
        if key is None or key in seen:
            continue
        seen.add(key)
        ordered.append(SubscribableInstrument(instrument_key=key))
    for listing in listings:
        if listing.instrument_key in seen:
            continue
        seen.add(listing.instrument_key)
        ordered.append(SubscribableInstrument(instrument_key=listing.instrument_key))
    return tuple(ordered)


def plan_subscriptions(
    adapter: BrokerAdapter,
    listings: Sequence[InstrumentListing],
    mode: SubscriptionMode,
) -> tuple[SubscriptionRequest, ...]:
    """As many listings as fit one connection at this mode, in listing order.

    Stops rather than errors at the cap -- a universe larger than one
    connection's limit is real (spec section 5's option-chain-width open
    item) and this plans what fits, leaving what doesn't for a second
    connection a later pass adds, not a crash now."""
    accepted: list[SubscriptionRequest] = []
    for listing in listings:
        candidate = SubscriptionRequest(instrument_key=listing.instrument_key, mode=mode)
        if adapter.does_subscription_fit_connection(tuple(accepted), candidate):
            accepted.append(candidate)
        else:
            break
    return tuple(accepted)


def plan_additional_subscriptions(
    adapter: BrokerAdapter,
    existing: Sequence[SubscriptionRequest],
    listings: Sequence[InstrumentListing],
    mode: SubscriptionMode,
) -> tuple[SubscriptionRequest, ...]:
    """The top-up plan_subscriptions can't express: new requests for listings
    not already in `existing`, filling whatever room is left under the
    connection's cap -- never resending one already streaming.

    Real bug, 2026-09-02: ensure_connected() only ever called plan_subscriptions
    once, at first connect, from whatever instrument_listings had accumulated
    by then -- 4 real instruments, a timing race against broker-instrument-
    catalogue-reader's 101,393-listing feed -- then short-circuited on every
    later tick (`if state["connection"] is not None: return True`) before
    ever looking at instrument_listings again. The other ~101,389 real
    instruments, and every one discovered by a later catalogue refresh, were
    never subscribed no matter how long the connection stayed open.
    """
    already = {request.instrument_key for request in existing}
    accepted: list[SubscriptionRequest] = list(existing)
    added: list[SubscriptionRequest] = []
    for listing in listings:
        if listing.instrument_key in already:
            continue
        candidate = SubscriptionRequest(instrument_key=listing.instrument_key, mode=mode)
        if adapter.does_subscription_fit_connection(tuple(accepted), candidate):
            accepted.append(candidate)
            added.append(candidate)
            already.add(listing.instrument_key)
        else:
            break
    return tuple(added)


def dispatch_decoded_message(
    decoded: DecodedFeedMessage,
    publish_ltp: Callable[[Sequence], None],
    publish_candle: Callable[[Sequence], None],
    publish_book: Callable[[Sequence], None],
    publish_oi: Callable[[Sequence], None],
    publish_greeks: Callable[[Sequence], None],
) -> None:
    """One call per record kind, each carrying the whole batch -- never one
    call per item.

    Real bug, 2026-09-02: this used to loop and call e.g. publish_ltp(update)
    once per item in decoded.ltp_updates. bus.publish() takes the whole
    iterable in one call (`for item in items` is its own internal loop), so
    a single LtpUpdate handed to it raised `TypeError: 'LtpUpdate' object is
    not iterable` the instant a real message ever had a non-empty
    ltp_updates. Invisible until this date because decoding never produced
    real feed data before then -- the "full_d5" subscribe-mode bug
    (SubscriptionMode.FULL) meant Upstox silently never sent anything past
    the initial market_info packet, so this path had never actually run.
    """
    publish_ltp(decoded.ltp_updates)
    publish_candle(decoded.candles)
    publish_book(decoded.book_updates)
    publish_oi(decoded.open_interest)
    publish_greeks(decoded.option_greeks)


def describe_standing(counts: dict, last_failure: str | None) -> dict:
    return {"part_id": PART_ID, **counts, "last_failure": last_failure}


def start_part(context) -> int:
    """Opens one connection, subscribes to what the current instrument
    listing + token standing allow, decodes and republishes every message.
    """
    from websockets.exceptions import ConnectionClosed, WebSocketException
    from websockets.sync.client import connect as connect_websocket

    from runtime.brokers.upstox import UpstoxAdapter
    from runtime.input_assembly import LatestByKey
    from runtime.level_publishing import LevelPublisher, without_observation_time

    adapter = UpstoxAdapter()
    token_standing = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    instrument_listings = LatestByKey(
        read=context.bus.reader("broker-instrument-listing"),
        key_of=listing_key_of,
        maximum_age_seconds=context.number("broker_instrument_listing_maximum_age"),
    )
    # The bounded, already-selected set. Read with an age bound like every
    # other level here: an unbounded LatestByKey is the trap this project has
    # fallen into repeatedly (2026-08-26), and a universe that stopped being
    # restated must stop being subscribed from rather than standing forever.
    selected_universe = LatestByKey(
        read=context.bus.reader("symbol-universe"),
        key_of=lambda entry: (entry.venue_id, entry.symbol),
        maximum_age_seconds=context.number("broker_subscription_universe_maximum_age"),
    )
    # Which underlyings' chains get subscription priority: every built segment's
    # (2026-09-05), each once. Three bots share one connection to the broker, and
    # a segment whose underlyings are not subscribed here receives no prices at
    # all -- the bot runs, reports healthy, and never sees its own market.
    from runtime.segment_settings import underlyings_every_built_segment_trades

    tracked_trading_symbols = underlyings_every_built_segment_trades(context)

    publish_ltp = context.bus.publisher_for("broker-market-data")
    publish_candle = context.bus.publisher_for("broker-candle")
    publish_book = context.bus.publisher_for("broker-order-book-snapshot")
    publish_oi = context.bus.publisher_for("broker-open-interest")
    publish_greeks = context.bus.publisher_for("broker-option-greeks")
    # A level, not an event: the subscription is true until it changes, and it
    # changes at most once a growth check. Compared without observed_at_ns, or
    # the "when I looked" stamp would make every restatement look like a new
    # subscription and nothing would ever be skipped.
    say_the_subscription = LevelPublisher(
        publish=context.bus.publisher_for("broker-subscription-state"),
        refresh_interval_seconds=context.number("level_refresh_interval_seconds"),
        identity_of=without_observation_time,
    )

    counts = {"decoded_messages": 0}

    def on_message(payload: bytes) -> None:
        try:
            decoded = adapter.decode_feed_message(payload)
        except Exception as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            return
        counts["decoded_messages"] += 1
        dispatch_decoded_message(
            decoded, publish_ltp, publish_candle, publish_book,
            publish_oi, publish_greeks,
        )

    state = {
        "connection": None,
        "subscribed": (),
        "backoff_seconds": context.number("broker_reconnect_backoff_floor"),
        "next_growth_check_at": None,
    }
    counts["last_failure"] = None

    def drop_connection(failure: Exception) -> None:
        counts["last_failure"] = f"{type(failure).__name__}: {failure}"
        connection = state["connection"]
        if connection is not None:
            connection.close()
        state["connection"] = None
        ceiling = context.number("broker_reconnect_backoff_ceiling")
        state["backoff_seconds"] = min(state["backoff_seconds"] * 2, ceiling)
        time.sleep(state["backoff_seconds"])

    def ensure_connected() -> bool:
        if state["connection"] is not None:
            return True
        token = token_standing.mapping().get(adapter.broker_id)
        listings = instrument_listings.values()
        universe = selected_universe.values()
        if token is None or not token.is_still_valid() or not (listings or universe):
            # Not a failure -- a normal state before either producer has
            # spoken, or after the token has expired and refresh is still
            # in flight. Reported on the standing, never raised.
            #
            # Either source is enough to connect on. The universe alone is the
            # bootstrap this part depends on: broker-symbol-universe-bridge
            # publishes the index underlyings without needing any price, so
            # they can be subscribed before the catalogue race resolves, and
            # their chains follow once those prices arrive.
            return False
        listings, contracts_left_to_the_universe = only_what_the_segments_trade(
            listings, tracked_trading_symbols, now_ms=time.time_ns() // 1_000_000,
        )
        state["contracts_left_to_the_universe"] = contracts_left_to_the_universe
        plan = plan_subscriptions(
            adapter,
            subscribe_the_universe_first(universe, listings),
            mode=SubscriptionMode.FULL,
        )
        if not plan:
            return False
        try:
            # Upstox's V3 feed is not connected to directly (spec section 5
            # correction, 2026-09-02): the signed, single-use URL comes from
            # this authorize call, and the websocket connection itself needs
            # no Authorization header -- the auth is in the URL's own query.
            stream_url = fetch_authorized_stream_url(adapter, token.access_token)
            connection = connect_websocket(
                stream_url,
                additional_headers={"Accept": "*/*"},
                open_timeout=context.number("broker_connection_open_timeout"),
            )
            connection.send(adapter.encode_subscribe_frame(plan))
        except (OSError, WebSocketException, ValueError) as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            return False
        state["connection"] = connection
        state["subscribed"] = plan
        state["backoff_seconds"] = context.number("broker_reconnect_backoff_floor")
        return True

    def grow_subscriptions_if_due() -> None:
        """The periodic top-up ensure_connected() can't do on its own: it
        only plans a subscription once, at first connect, and this is what
        picks up every instrument that arrived after that -- paced rather
        than run every tick, since instrument_listings.values() copies the
        whole known-listings table (up to 101,393 entries) on every call."""
        now = time.monotonic()
        if (
            state["next_growth_check_at"] is not None
            and now < state["next_growth_check_at"]
        ):
            return
        state["next_growth_check_at"] = now + context.number(
            "broker_subscription_growth_check_interval"
        )
        listings = instrument_listings.values()
        listings, contracts_left_to_the_universe = only_what_the_segments_trade(
            listings, tracked_trading_symbols, now_ms=time.time_ns() // 1_000_000,
        )
        state["contracts_left_to_the_universe"] = contracts_left_to_the_universe
        additional = plan_additional_subscriptions(
            adapter,
            state["subscribed"],
            subscribe_the_universe_first(selected_universe.values(), listings),
            mode=SubscriptionMode.FULL,
        )
        if not additional:
            return
        connection = state["connection"]
        try:
            connection.send(adapter.encode_subscribe_frame(additional))
        except (ConnectionClosed, WebSocketException, OSError) as failure:
            drop_connection(failure)
            return
        state["subscribed"] = state["subscribed"] + additional

    def drain_one_tick() -> None:
        # Every tick, before anything else: this part spends its whole tick
        # inside connection.recv(), and its two levels are otherwise touched
        # only by ensure_connected() -- which returns immediately once
        # connected -- and by the 60-second-paced growth check. Pacing the
        # growth check paced the *drain* with it, and
        # broker-instrument-catalogue-reader restates all 102,940 listings
        # repeatedly, so the bounded bus buffer overflowed in between: measured
        # on the live spine 2026-09-04, this part had received 1,067 listings of
        # 102,940 with input_loss on the type, and the three index underlyings
        # the whole segment is about were not among them. The subscription
        # selection only_what_the_segments_trade() applies was working
        # perfectly and had nothing to select.
        #
        # Draining is cheap; it is values()/mapping() that copies the whole
        # table, and that stays on its interval.
        instrument_listings.take_in_what_arrived()
        token_standing.take_in_what_arrived()
        selected_universe.take_in_what_arrived()
        connected = ensure_connected()
        if connected:
            grow_subscriptions_if_due()
        # Said whether or not there is a connection, and before the drain that
        # spends the rest of the tick: an empty subscription is a fact
        # subscribed-instrument-listing-filter has to be told, or it would go on
        # restating the set from a connection that has since dropped.
        say_the_subscription.publish_level(
            (state_of_the_subscription(
                adapter.broker_id, state["subscribed"], time.time_ns(),
            ),)
        )
        if not connected:
            return
        connection = state["connection"]
        if connection is None:
            return  # grow_subscriptions_if_due found the connection dead
        deadline = time.monotonic() + context.number("broker_stream_drain_interval")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                message = connection.recv(timeout=remaining)
                if isinstance(message, str):
                    continue  # a text frame is never a feed message -- binary only
                on_message(message)
        except TimeoutError:
            return  # nothing arrived this drain window -- not a fault
        except (ConnectionClosed, WebSocketException, OSError) as failure:
            drop_connection(failure)

    def describe() -> dict:
        return {
            "part_id": PART_ID,
            "connected": state["connection"] is not None,
            "subscribed_instruments": len(state["subscribed"]),
            "universe_instruments_known": len(selected_universe.mapping()),
            # Nearest-expiry contracts this part deliberately did not subscribe
            # from the raw catalogue, leaving them to the universe's own ranked
            # chains (2026-09-12). A large number here is the normal state, not
            # a fault: it is 13,738 on the operator's own 220-underlying
            # universe, and every one of them would have been an unranked
            # strike competing for a slot nothing can ever evict.
            "contracts_left_to_the_universe": state.get(
                "contracts_left_to_the_universe", 0
            ),
            "decoded_messages": counts["decoded_messages"],
            "last_failure": counts["last_failure"],
        }

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=drain_one_tick,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=describe,
        )
    finally:
        if state["connection"] is not None:
            state["connection"].close()


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "describe_standing",
    "dispatch_decoded_message",
    "fetch_authorized_stream_url",
    "listing_key_of",
    "plan_additional_subscriptions",
    "plan_subscriptions",
    "subscribe_the_universe_first",
    "only_what_the_segments_trade",
    "start_part",
    "state_of_the_subscription",
]
