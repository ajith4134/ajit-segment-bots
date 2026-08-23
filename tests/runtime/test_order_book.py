"""Books kept from the two venues' real messages (RL-063)."""

from __future__ import annotations

import pytest

from runtime.order_book import OrderBookKeeper
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import BookUpdate

BINANCE = "binance-usdm"
BYBIT = "bybit-linear"
BINANCE_BOOK_RUN = "2026-08-22-public-ws-depth20.jsonl"
BYBIT_BOOK_RUN = "2026-08-22-public-linear-orderbook.jsonl"


def updates_from(venue_id, capture, read_captured_payloads):
    adapter = load_venue_adapter(venue_id)
    found = [adapter.read_book_update(payload) for _t, payload in read_captured_payloads(venue_id, capture)]
    return [update for update in found if update is not None]


def test_binance_partial_depth_is_a_snapshot_on_every_push(read_captured_payloads):
    updates = updates_from(BINANCE, BINANCE_BOOK_RUN, read_captured_payloads)
    assert updates and all(update.is_snapshot for update in updates)
    keeper = OrderBookKeeper(depth_levels=20)
    for update in updates:
        book = keeper.apply(update)
        assert book is not None and not book.is_crossed
        assert book.bids == tuple(sorted(book.bids, key=lambda level: -level[0]))
        assert book.asks == tuple(sorted(book.asks, key=lambda level: level[0]))
    assert keeper.standing.snapshots_taken == len(updates)


def test_bybit_deltas_apply_in_order_and_zero_removes_a_level(read_captured_payloads):
    updates = updates_from(BYBIT, BYBIT_BOOK_RUN, read_captured_payloads)
    assert updates[0].is_snapshot and not updates[1].is_snapshot
    keeper = OrderBookKeeper(depth_levels=50)
    books = [keeper.apply(update) for update in updates]
    assert all(book is not None for book in books), "the captured run is contiguous"
    assert keeper.standing.deltas_applied == len(updates) - 1
    assert all(not book.is_crossed for book in books)
    removed = next(
        (price for update in updates[1:] for price, quantity in update.asks if quantity == 0), None
    )
    assert removed is not None, "the captured run removes at least one level"
    final = books[-1]
    assert all(quantity > 0 for _price, quantity in final.bids + final.asks)
    assert books[0].is_from_snapshot and not books[-1].is_from_snapshot


def test_a_missed_delta_drops_the_book_rather_than_handing_on_a_wrong_one(read_captured_payloads):
    updates = updates_from(BYBIT, BYBIT_BOOK_RUN, read_captured_payloads)
    keeper = OrderBookKeeper(depth_levels=50)
    assert keeper.apply(updates[0]) is not None
    skipped = updates[2]  # updates[1] never arrives
    assert keeper.apply(skipped) is None
    assert keeper.standing.deltas_refused_out_of_order == 1
    assert keeper.apply(updates[3]) is None, "no book until the venue sends a snapshot"
    assert keeper.standing.deltas_refused_without_a_book == 1
    assert keeper.apply(updates[0]) is not None, "a snapshot starts it again"


def test_a_delta_before_any_snapshot_is_refused():
    keeper = OrderBookKeeper(depth_levels=5)
    delta = BookUpdate(BYBIT, "BTCUSDT", ((1.0, 1.0),), (), False, 7, 1)
    assert keeper.apply(delta) is None
    assert keeper.standing.deltas_refused_without_a_book == 1


def test_a_book_of_no_levels_is_refused():
    with pytest.raises(ValueError):
        OrderBookKeeper(depth_levels=0)
