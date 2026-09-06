"""broker-quote-bridge: republishes a broker's own top of book as market-quote.

`market-quote` had exactly one producer in the whole blueprint --
`venue-quote-stream-reader`, which is a crypto venue part and is off -- so
`quote-level-sampler` had received nothing ever, `symbol-quote-frame` had never
been produced, and both of its readers were cut off:
`spread-reversion-detector` and `instrument-selector`.

That second one is why this part exists. A quote is `instrument-selector`'s
**fallback when the last trade is too old to size against**: `_reference_price`
asks the trade first and, past the symbol's own believable-age bound, asks the
resting quote before giving up (`priced_from_a_quote` on its standing counts
exactly that), and `_price_refusal` does the same before saying
`REFERENCE_PRICE_IS_TOO_OLD`. `NormalisedQuote`'s own docstring records the
measurement the type was built for -- on the live run of 2026-08-24
instrument-selector refused **525 of 9,945 intents for a price too old** on
symbols that were captured and simply not being traded.

Indian option chains make that the ordinary case rather than the corner. A
strike a few steps out of the money goes minutes without a print while carrying
a live bid and ask, and the three segment bots select from exactly those chains.

**Nothing new is subscribed.** `broker-market-feed-reader` already decodes
Upstox's depth into `BrokerOrderBookUpdate`, whose levels carry bid/ask price
and size -- the exact four fields `NormalisedQuote` needs -- and already
publishes them as `broker-order-book-snapshot`, which
`broker-order-book-bridge` reads today. This reads the same wire and states the
best level of it as a quote, which makes it the fourth member of an existing
family rather than a new feed:

    broker-market-data-bridge     broker-market-data          -> market-data
    broker-candle-bridge          broker-candle               -> candle
    broker-order-book-bridge      broker-order-book-snapshot  -> order-book-snapshot
    broker-quote-bridge           broker-order-book-snapshot  -> market-quote

Separate from `broker-order-book-bridge` rather than folded into it (T-6: grow
by adding parts) because the two answer different questions -- the whole book to
walk a fill against, versus the top of it as the price a symbol may be believed
at -- and the resource governor must be able to shed either without the other.

**A zero-priced level is dropped before the best is chosen, never sorted.**
Upstox pairs bid and ask at the same depth index and pads the thinner side with
`price=0, quantity=0`. Taking the minimum ask across a padded level would report
a best ask of 0 -- the exact bug fixed in `broker-order-book-bridge` on
2026-09-02, where it poisoned every consumer of the book, and here it would put
a mid halfway to zero on the wire as the price a position is sized against.

**A one-sided book produces no quote at all.** `NormalisedQuote.mid_price` is
`(bid + ask) / 2` with no notion of a market quoted on one side, so a quote built
without an ask would state a mid that is half the bid. A genuinely one-sided
book is a real state of a thin option chain, and the honest answer to it is no
quote rather than a fabricated one -- the same reasoning that keeps `side` and
`quantity` None on `NormalisedTrade` instead of guessing them.

`venue_time_ns` is Upstox's own `broker_time_ns` and never arrival time: the
whole value of a quote is its age, and an age measured from when we happened to
read the socket is our latency rather than the market's.
"""

from __future__ import annotations

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.pending_instrument_updates import UpdatesAwaitingInstrumentListing
from runtime.venues.venue_adapter import NormalisedQuote

PART_ID = "broker-quote-bridge"
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="broker-quote-bridge",
    consumes=("broker-subscribed-instrument-listing", "broker-order-book-snapshot"),
    produces=("market-quote", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


class BrokerQuoteBridge:
    """Resolves an instrument's own trading_symbol and states its top of book."""

    def __init__(self, *, held_instrument_limit: int) -> None:
        self._trading_symbol_by_key: dict[str, str] = {}
        # The feed's whole snapshot arrives the instant the socket connects while
        # the listings arrive on a 300 s restatement conveyor, so the first burst
        # always meets an empty map -- runtime/pending_instrument_updates.py
        # carries the measurement that cost the other three bridges every message
        # they ever received.
        self._awaiting_listing: UpdatesAwaitingInstrumentListing = (
            UpdatesAwaitingInstrumentListing(held_instrument_limit=held_instrument_limit)
        )
        self.quotes_refused_for_a_one_sided_book = 0

    def observe_listing(self, listing) -> None:
        self._trading_symbol_by_key[listing.instrument_key] = listing.trading_symbol

    def quote_for(self, update) -> NormalisedQuote | None:
        """One depth update, as market-quote -- or None while its instrument is
        unknown (in which case it is held until the listing arrives), or when the
        book is quoted on one side only."""
        symbol = self._trading_symbol_by_key.get(update.instrument_key)
        if symbol is None:
            self._awaiting_listing.hold(update.instrument_key, update)
            return None
        # A padded level carries no real quote, so it is excluded before the best
        # is chosen rather than allowed to win the comparison at price 0.
        bids = [level for level in update.levels if level.bid_price > 0]
        asks = [level for level in update.levels if level.ask_price > 0]
        if not bids or not asks:
            self.quotes_refused_for_a_one_sided_book += 1
            return None
        best_bid = max(bids, key=lambda level: level.bid_price)
        best_ask = min(asks, key=lambda level: level.ask_price)
        return NormalisedQuote(
            venue_id=UPSTOX_VENUE_ID,
            symbol=symbol,
            bid_price=best_bid.bid_price,
            bid_quantity=best_bid.bid_quantity,
            ask_price=best_ask.ask_price,
            ask_quantity=best_ask.ask_quantity,
            venue_time_ns=update.broker_time_ns,
        )

    def quotes_now_resolvable(self) -> tuple[NormalisedQuote, ...]:
        """Every held depth update whose listing has since arrived, as a quote."""
        released = tuple(
            self._awaiting_listing.release_resolvable(
                lambda key: key in self._trading_symbol_by_key
            )
        )
        return tuple(
            quote for update in released if (quote := self.quote_for(update)) is not None
        )

    def quotes_from(self, updates) -> tuple[NormalisedQuote, ...]:
        """One tick's worth of market-quote: what the listings just unblocked,
        then this tick's own depth.

        The order is the point and is why this is a method rather than a line in
        `start_part`. A quote is a level and its age is the whole reason anyone
        reads it, so a released quote landing behind a fresher one for the same
        symbol would leave every consumer holding the older of the two.
        """
        return self.quotes_now_resolvable() + tuple(
            quote for update in updates if (quote := self.quote_for(update)) is not None
        )


def describe_bridge(bridge: BrokerQuoteBridge) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_resolved": len(bridge._trading_symbol_by_key),
        "quotes_refused_for_a_one_sided_book": float(
            bridge.quotes_refused_for_a_one_sided_book
        ),
        **bridge._awaiting_listing.describe(),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    updates = Batch(read=context.bus.reader("broker-order-book-snapshot"))
    publish_quotes = context.bus.publisher_for("market-quote")
    bridge = BrokerQuoteBridge(
        held_instrument_limit=int(
            context.setting("unresolved_broker_update_hold_limit").value
        ),
    )

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        quotes = bridge.quotes_from(updates.payloads())
        if quotes:
            publish_quotes(quotes)

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
    "BrokerQuoteBridge",
    "PART_DECLARATION",
    "PART_ID",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "start_part",
]
