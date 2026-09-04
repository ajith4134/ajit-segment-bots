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
from typing import Callable, Sequence

from runtime.brokers.broker_adapter import (
    BrokerAdapter, DecodedFeedMessage, InstrumentListing, SubscriptionMode,
    SubscriptionRequest,
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
    consumes=("broker-token-standing", "broker-instrument-listing"),
    produces=(
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks", "part-health",
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


def prioritize_index_option_chain(
    listings: Sequence[InstrumentListing],
    tracked_trading_symbols: Sequence[str],
    now_ms: int,
) -> tuple[InstrumentListing, ...]:
    """Reorders listings so the tracked index underlyings and their
    nearest-expiry option chain come first -- plan_subscriptions and
    plan_additional_subscriptions both take whatever fits the cap "in
    listing order" (their own docstrings), and broker-instrument-catalogue-
    reader's raw listing order has no relationship to what this project
    trades: of 101,393 real Upstox listings, the first 2000 in catalogue
    order essentially never include the NIFTY/BANKNIFTY/SENSEX option chain
    instrument-selector needs a delta for.

    Confirmed live, 2026-09-02: with this project's real subscribed set,
    instrument-selector refused 14/14 trade-intents
    no-instrument-is-listed-for-this-symbol and symbols_with_listed_
    instruments stayed at 0 -- no option contract for any tracked underlying
    was ever subscribed, so broker-option-greeks never carried a delta and
    AtmStrikeTracker.atm_call_for/atm_put_for could never resolve, no matter
    how long the connection stayed open.

    Nearest expiry only (spec section 2), computed here rather than assumed
    from listing order, since Upstox's own catalogue is not expiry-sorted.
    An expiry that has already passed is dropped, not just deprioritized --
    a same-day-expired contract is real, current data and would otherwise
    still win a nearest-expiry comparison against tomorrow's real chain.
    """
    tracked = set(tracked_trading_symbols)
    underlying_keys: dict[str, str] = {
        listing.trading_symbol: listing.instrument_key
        for listing in listings
        if listing.instrument_type == "INDEX" and listing.trading_symbol in tracked
    }
    tracked_underlying_keys = set(underlying_keys.values())

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

    priority: list[InstrumentListing] = []
    priority_keys: set[str] = set()
    for listing in listings:
        is_tracked_underlying = listing.instrument_key in tracked_underlying_keys
        is_nearest_expiry_option = listing.expiry_ms is not None and listing.expiry_ms == (
            nearest_expiry_by_underlying.get(listing.underlying_key)
        )
        if is_tracked_underlying or is_nearest_expiry_option:
            priority.append(listing)
            priority_keys.add(listing.instrument_key)

    rest = (listing for listing in listings if listing.instrument_key not in priority_keys)
    return tuple(priority) + tuple(rest)


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
    tracked_index_trading_symbols = tuple(
        str(symbol)
        for symbol in context.setting("underlying_price_bridge_index_trading_symbols").value
    )

    publish_ltp = context.bus.publisher_for("broker-market-data")
    publish_candle = context.bus.publisher_for("broker-candle")
    publish_book = context.bus.publisher_for("broker-order-book-snapshot")
    publish_oi = context.bus.publisher_for("broker-open-interest")
    publish_greeks = context.bus.publisher_for("broker-option-greeks")

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
        if token is None or not token.is_still_valid() or not listings:
            # Not a failure -- a normal state before either producer has
            # spoken, or after the token has expired and refresh is still
            # in flight. Reported on the standing, never raised.
            return False
        listings = prioritize_index_option_chain(
            listings, tracked_index_trading_symbols, now_ms=time.time_ns() // 1_000_000,
        )
        plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
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
        listings = prioritize_index_option_chain(
            listings, tracked_index_trading_symbols, now_ms=time.time_ns() // 1_000_000,
        )
        additional = plan_additional_subscriptions(
            adapter, state["subscribed"], listings, mode=SubscriptionMode.FULL,
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
        # priority prioritize_index_option_chain() applies was working
        # perfectly and had nothing to promote.
        #
        # Draining is cheap; it is values()/mapping() that copies the whole
        # table, and that stays on its interval.
        instrument_listings.take_in_what_arrived()
        token_standing.take_in_what_arrived()
        if not ensure_connected():
            return
        grow_subscriptions_if_due()
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
    "prioritize_index_option_chain",
    "start_part",
]
