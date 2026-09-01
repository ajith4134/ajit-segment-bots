"""broker-market-feed-reader: stream a broker's feed, decomposed into this
project's own record kinds (spec section 5).

WebSocket connection, protobuf-decoded via the adapter, one bundled message
per instrument split into up to five separate record kinds before
publishing -- never republished as one wire carrying several shapes
(docs/proposals/upstox-broker-adapter.md, same reasoning that split
candle/market-data/order-book-snapshot apart for the crypto build).
"""

from __future__ import annotations

import time
from typing import Sequence

from runtime.brokers.broker_adapter import (
    BrokerAdapter, InstrumentListing, SubscriptionMode, SubscriptionRequest,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-market-feed-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("broker-token-standing", "broker-instrument-listing"),
    produces=(
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks", "part-health",
    ),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


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
        key_of=lambda _: adapter.broker_id,
        maximum_age_seconds=context.number("broker_instrument_listing_maximum_age"),
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
        for update in decoded.ltp_updates:
            publish_ltp(update)
        for candle in decoded.candles:
            publish_candle(candle)
        for book in decoded.book_updates:
            publish_book(book)
        for oi in decoded.open_interest:
            publish_oi(oi)
        for greeks in decoded.option_greeks:
            publish_greeks(greeks)

    state = {
        "connection": None,
        "subscribed": (),
        "backoff_seconds": context.number("broker_reconnect_backoff_floor"),
    }
    counts["last_failure"] = None

    def ensure_connected() -> bool:
        if state["connection"] is not None:
            return True
        token = token_standing.mapping().get(adapter.broker_id)
        listings = instrument_listings.mapping().get(adapter.broker_id)
        if token is None or not token.is_still_valid() or not listings:
            # Not a failure -- a normal state before either producer has
            # spoken, or after the token has expired and refresh is still
            # in flight. Reported on the standing, never raised.
            return False
        plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
        if not plan:
            return False
        try:
            connection = connect_websocket(
                adapter.stream_endpoint_url(),
                additional_headers={
                    "Authorization": f"Bearer {token.access_token}",
                    "Accept": "*/*",
                },
                open_timeout=context.number("broker_connection_open_timeout"),
            )
            connection.send(adapter.encode_subscribe_frame(plan))
        except (OSError, WebSocketException) as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            return False
        state["connection"] = connection
        state["subscribed"] = plan
        state["backoff_seconds"] = context.number("broker_reconnect_backoff_floor")
        return True

    def drain_one_tick() -> None:
        if not ensure_connected():
            return
        connection = state["connection"]
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
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            connection.close()
            state["connection"] = None
            ceiling = context.number("broker_reconnect_backoff_ceiling")
            state["backoff_seconds"] = min(state["backoff_seconds"] * 2, ceiling)
            time.sleep(state["backoff_seconds"])

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
    "plan_subscriptions",
    "start_part",
]
