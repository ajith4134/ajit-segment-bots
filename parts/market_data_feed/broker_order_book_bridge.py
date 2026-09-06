"""broker-order-book-bridge: republishes a broker's own depth updates as
order-book-snapshot -- the crypto-era type book-walk-fill-pricer,
limit-price-walker and others already read, so a fill can be walked
against real depth for an option contract the same way crypto's does for a
perpetual.

Bids and asks are sorted best-first rather than trusted from input order:
`OrderBookSnapshot.best_bid`/`best_ask` read `bids[0]`/`asks[0]` as the
best level, and Upstox's own depth levels arriving best-first is an
assumption worth verifying in code, not just believing from the schema.

**A zero-price level is dropped, never sorted.** Real bug, 2026-09-02: a
thin option's real book can carry a level with no quote on one side --
Upstox pairs bid and ask at the same depth index, and a side with fewer
real levels than the other pads the rest at price=0, quantity=0. Sorting
that in ascending order for asks put the pad *first* (0 sorts below any
real ask), so `best_ask` read 0 -- a false "the book is nearly free" price
-- on a book that in fact had a perfectly normal best ask, poisoning every
consumer of `order-book-snapshot`, not only this one. A zero-price level
carries no real quote, so it is excluded before the sort rather than sorted
into first place.

`sequence` uses `NOT_SENT`, the project's own existing sentinel for a
source with no per-update sequence number (runtime/tape.py, already used
by runtime/venues/binance_usdm.py for the same situation) -- not a new
convention. `is_from_snapshot=True` always: Upstox's MarketLevel carries
full depth per update, not deltas applied to a prior snapshot (spec
section 5), so every update this bridge sees already is one.
"""

from __future__ import annotations

from runtime.order_book import OrderBookSnapshot
from runtime.pending_instrument_updates import UpdatesAwaitingInstrumentListing
from runtime.tape import NOT_SENT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-order-book-bridge"
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="broker-order-book-bridge",
    consumes=("broker-subscribed-instrument-listing", "broker-order-book-snapshot"),
    produces=("order-book-snapshot", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


class BrokerOrderBookBridge:
    """Resolves an instrument's own trading_symbol and republishes its depth."""

    def __init__(self, *, held_instrument_limit: int) -> None:
        self._trading_symbol_by_key: dict[str, str] = {}
        # The connect burst arrives against an empty listing map; held rather
        # than dropped (runtime/pending_instrument_updates.py). Newest depth per
        # instrument is the right thing to hold: Upstox sends full depth per
        # update rather than deltas, so an older snapshot is superseded whole.
        self._awaiting_listing: UpdatesAwaitingInstrumentListing = (
            UpdatesAwaitingInstrumentListing(held_instrument_limit=held_instrument_limit)
        )

    def observe_listing(self, listing) -> None:
        self._trading_symbol_by_key[listing.instrument_key] = listing.trading_symbol

    def book_for(self, update) -> OrderBookSnapshot | None:
        """One depth update, as order-book-snapshot -- or None while its
        instrument is unknown, in which case it is held until the listing
        arrives."""
        symbol = self._trading_symbol_by_key.get(update.instrument_key)
        if symbol is None:
            self._awaiting_listing.hold(update.instrument_key, update)
            return None
        bids = tuple(
            sorted(
                (
                    (level.bid_price, level.bid_quantity) for level in update.levels
                    if level.bid_price > 0
                ),
                key=lambda level: level[0], reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (
                    (level.ask_price, level.ask_quantity) for level in update.levels
                    if level.ask_price > 0
                ),
                key=lambda level: level[0],
            )
        )
        return OrderBookSnapshot(
            venue_id=UPSTOX_VENUE_ID, symbol=symbol, bids=bids, asks=asks,
            sequence=NOT_SENT, venue_time_ns=update.broker_time_ns, is_from_snapshot=True,
        )


    def books_now_resolvable(self) -> tuple[OrderBookSnapshot, ...]:
        """Every held depth update whose listing has since arrived."""
        released = tuple(
            self._awaiting_listing.release_resolvable(
                lambda key: key in self._trading_symbol_by_key
            )
        )
        return tuple(
            book for update in released if (book := self.book_for(update)) is not None
        )
    def books_from(self, updates) -> tuple[OrderBookSnapshot, ...]:
        """One tick's worth of order-book-snapshot: what the listings just
        unblocked, then this tick's own depth. The order is the point -- a book
        is a level, and the last one on the wire is the one every consumer
        keeps, so a released snapshot must never land behind a fresher one."""
        return self.books_now_resolvable() + tuple(
            book for update in updates if (book := self.book_for(update)) is not None
        )


def describe_bridge(bridge: BrokerOrderBookBridge) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_resolved": len(bridge._trading_symbol_by_key),
        **bridge._awaiting_listing.describe(),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    updates = Batch(read=context.bus.reader("broker-order-book-snapshot"))
    publish_books = context.bus.publisher_for("order-book-snapshot")
    bridge = BrokerOrderBookBridge(
        held_instrument_limit=int(
            context.setting("unresolved_broker_update_hold_limit").value
        ),
    )

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        books = bridge.books_from(updates.payloads())
        if books:
            publish_books(books)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_bridge(bridge),
    )


__all__ = [
    "BrokerOrderBookBridge",
    "PART_DECLARATION",
    "PART_ID",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "start_part",
]
