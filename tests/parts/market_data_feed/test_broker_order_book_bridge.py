"""Tests against real Upstox instrument-master values and the real
BrokerOrderBookUpdate/BrokerOrderBookLevel shapes already verified this
session (RL-063)."""

from runtime.brokers.broker_adapter import (
    BrokerOrderBookLevel, BrokerOrderBookUpdate, InstrumentListing,
)
from runtime.tape import NOT_SENT
from parts.market_data_feed.broker_order_book_bridge import BrokerOrderBookBridge

# Upstox's own documented ltpc_combined_limit, which is what the
# unresolved_broker_update_hold_limit setting carries in the running spine.
HELD_INSTRUMENT_LIMIT = 2000

NIFTY_CALL = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)


def _book(instrument_key, levels, broker_time_ns=1_740_000_000_000_000_000):
    return BrokerOrderBookUpdate(
        instrument_key=instrument_key,
        levels=tuple(BrokerOrderBookLevel(*level) for level in levels),
        broker_time_ns=broker_time_ns,
    )


def test_book_for_is_none_before_the_listing_resolves():
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    book = _book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)])
    assert bridge.book_for(book) is None


def test_builds_a_real_order_book_snapshot():
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.observe_listing(NIFTY_CALL)
    book = _book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0), (225.35, 30.0, 225.75, 45.0)])
    snapshot = bridge.book_for(book)
    assert snapshot is not None
    assert snapshot.venue_id == "upstox"
    assert snapshot.symbol == "NIFTY 24500 CE"
    assert snapshot.venue_time_ns == 1_740_000_000_000_000_000
    assert snapshot.sequence == NOT_SENT
    assert snapshot.is_from_snapshot is True


def test_bids_and_asks_are_sorted_best_first_regardless_of_input_order():
    """Upstox's own depth levels arrive best-first, but this must not be
    assumed silently -- OrderBookSnapshot.best_bid/best_ask read bids[0]/
    asks[0] as the best, so a bridge that trusted input order and got it
    wrong would misprice every fill against it."""
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.observe_listing(NIFTY_CALL)
    # Deliberately out of order: the worse bid/ask listed first.
    book = _book("NSE_FO|1001", [(225.35, 30.0, 225.75, 45.0), (225.4, 75.0, 225.7, 150.0)])
    snapshot = bridge.book_for(book)
    assert snapshot.best_bid == 225.4
    assert snapshot.best_ask == 225.7
    assert snapshot.bids == ((225.4, 75.0), (225.35, 30.0))
    assert snapshot.asks == ((225.7, 150.0), (225.75, 45.0))


def test_a_zero_price_padding_level_is_dropped_not_sorted_as_best():
    """Real bug, 2026-09-02: Upstox pairs bid and ask at the same depth
    index, and a side with fewer real levels than the other pads the rest
    at price=0, quantity=0. Sorting asks ascending put that pad *first* (0
    sorts below any real ask), so best_ask read 0 instead of the real
    225.7 -- a false 'the book is nearly free' price that poisoned every
    consumer of order-book-snapshot, not only the liquidity grader that
    first surfaced it as a crash."""
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.observe_listing(NIFTY_CALL)
    book = _book("NSE_FO|1001", [
        (225.4, 75.0, 225.7, 150.0),
        (225.35, 30.0, 0.0, 0.0),
    ])
    snapshot = bridge.book_for(book)
    assert snapshot.best_bid == 225.4
    assert snapshot.best_ask == 225.7
    assert snapshot.asks == ((225.7, 150.0),)


def test_a_side_padded_entirely_to_zero_reads_as_no_quote_not_zero():
    """The honest state for a side with no real quote at all is an empty
    tuple (OrderBookSnapshot.best_ask already returns None for that), never
    a fabricated 0 that reads as a free-to-buy market."""
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.observe_listing(NIFTY_CALL)
    book = _book("NSE_FO|1001", [(225.4, 75.0, 0.0, 0.0), (225.35, 30.0, 0.0, 0.0)])
    snapshot = bridge.book_for(book)
    assert snapshot.asks == ()
    assert snapshot.best_ask is None


def test_an_unresolved_instrument_is_ignored_not_raised():
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.observe_listing(NIFTY_CALL)
    book = _book("NSE_FO|9999", [(1.0, 1.0, 2.0, 1.0)])
    assert bridge.book_for(book) is None


def test_a_depth_update_held_before_its_listing_is_released_when_it_arrives():
    """Measured on the live spine 2026-09-06: 1,999 broker-order-book-snapshot
    updates received, 0 order-book-snapshot published, instruments_resolved
    2,000 -- the connect burst met an empty listing map and was dropped."""
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    book = _book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)])
    assert bridge.book_for(book) is None
    assert bridge.books_now_resolvable() == ()

    bridge.observe_listing(NIFTY_CALL)
    released = bridge.books_now_resolvable()
    assert len(released) == 1
    assert released[0].symbol == "NIFTY 24500 CE"
    assert released[0].bids[0] == (225.4, 75.0)
    assert bridge.books_now_resolvable() == ()


def test_only_the_newest_held_depth_per_instrument_is_released():
    """Upstox sends full depth per update rather than deltas, so an earlier
    snapshot is superseded whole -- releasing both would put a stale book on
    the wire behind the current one."""
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.book_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)]))
    bridge.book_for(_book("NSE_FO|1001", [(226.1, 60.0, 226.4, 90.0)]))
    bridge.observe_listing(NIFTY_CALL)
    released = bridge.books_now_resolvable()
    assert [snapshot.bids[0] for snapshot in released] == [(226.1, 60.0)]


def test_a_released_book_never_lands_behind_this_tick_s_newer_book():
    bridge = BrokerOrderBookBridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT)
    bridge.book_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)]))
    bridge.observe_listing(NIFTY_CALL)
    published = bridge.books_from([_book("NSE_FO|1001", [(228.0, 40.0, 228.3, 55.0)])])
    assert [snapshot.bids[0] for snapshot in published] == [(225.4, 75.0), (228.0, 40.0)]
