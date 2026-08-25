"""An open position survives the off switch.

The failure these pin was measured, not supposed: the spine had started 46 times,
855 positions had been opened and 115 round trips closed. The lot books lived only
in the process, so every start forgot every position that was open -- and a
position whose lots are forgotten can never reach flat, never emits a
`closed-trade`, and can never be scored by anything downstream of it.

Written against the real `DurableStateStore` on a real directory rather than a
stub, because the thing being tested is that state crosses a process boundary and
a stub that held it in memory would prove nothing about that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from parts.portfolio_state.cost_basis_tracker import CostBasisTracker
from parts.portfolio_state.position_close_detector import PositionCloseDetector
from runtime.durable_state import RESTORED, DurableStateStore
from runtime.lot_book_checkpoint import (
    book_key_of,
    book_key_text,
    books_as_documents,
    books_from_documents,
)
from runtime.trading_types import BUY, FLAT, SELL, Fill, Lot, LotBook

VENUE = "binance-usdm"
SYMBOL = "ETHUSDC"
SECOND = 1_000_000_000
SETTINGS = {"remembered_fill_ids": 5000.0}


def fill(fill_id, side, price, quantity, at=1, fee=0.55):
    return Fill(
        fill_id=fill_id, venue_id=VENUE, symbol=SYMBOL, side=side,
        price=price, quantity=quantity, fee=fee, filled_at_ns=at * SECOND,
    )


def test_a_key_survives_the_round_trip_through_json():
    key = (VENUE, SYMBOL)
    assert book_key_of(book_key_text(key)) == key


def test_a_symbol_containing_no_separator_cannot_be_split_wrongly():
    """The separator is chosen so the split back is unambiguous, not usually right."""
    for symbol in ("BTCUSDT", "1000PEPEUSDT", "ETH-USDC", "BTCUSDT_240628"):
        assert book_key_of(book_key_text((VENUE, symbol))) == (VENUE, symbol)


def test_a_lot_book_keeps_its_exact_quantities_through_json():
    """A float round-trip would put back the residue exact_quantity keeps out."""
    import json

    book = LotBook()
    book.add(Lot(0.01, 2500.0, 1 * SECOND, 0.55))
    documents = json.loads(json.dumps(books_as_documents({(VENUE, SYMBOL): book})))
    restored = books_from_documents(documents)[(VENUE, SYMBOL)]
    assert restored.lots[0].quantity == Decimal("0.01")
    assert restored.total_quantity == Decimal("0.01")


def test_an_empty_book_is_not_written_because_it_is_not_a_position():
    assert books_as_documents({(VENUE, SYMBOL): LotBook()}) == {}


def test_a_half_closed_round_trip_finishes_in_the_next_process(durable_tmp_path):
    """The whole point: the trade the old code could never close.

    Opened 1.0, sold 0.99, then the process ends. A new one restores and sells the
    last 0.01 -- and the closed trade must describe the whole round trip, not just
    what the second process saw.
    """
    store = DurableStateStore(durable_tmp_path)

    before = PositionCloseDetector()
    before.observe_fill(fill("f1", BUY, 2500.0, 1.0, at=1))
    before.observe_fill(fill("f2", SELL, 2510.0, 0.99, at=2))
    store.save("position-close-detector", "positions", before.read_checkpoint_state(), SETTINGS)

    after = PositionCloseDetector()
    restoration = store.restore("position-close-detector", "positions", SETTINGS)
    assert restoration.verdict == RESTORED
    assert after.restore_from_checkpoint(restoration.state) == 1

    trade = after.observe_fill(fill("f3", SELL, 2510.0, 0.01, at=3))
    assert trade is not None, "the restored round trip never reached flat"
    assert trade.quantity == pytest.approx(1.0)
    assert trade.entry_price == pytest.approx(2500.0)
    assert trade.realised_pnl == pytest.approx(10.0)
    # Fees from both processes, because they were paid by one round trip.
    assert trade.fees_paid == pytest.approx(1.65)
    assert trade.holding_seconds == pytest.approx(2.0)


def test_a_restored_trade_keeps_the_excursion_it_went_through(durable_tmp_path):
    store = DurableStateStore(durable_tmp_path)
    before = PositionCloseDetector()
    before.observe_excursion(VENUE, SYMBOL, best=75.0, worst=-10.0)
    before.observe_fill(fill("f1", BUY, 100.0, 2.0, at=1))
    before.observe_fill(fill("f2", SELL, 110.0, 1.0, at=2))
    store.save("position-close-detector", "positions", before.read_checkpoint_state(), SETTINGS)

    after = PositionCloseDetector()
    after.restore_from_checkpoint(
        store.restore("position-close-detector", "positions", SETTINGS).state
    )
    trade = after.observe_fill(fill("f3", SELL, 110.0, 1.0, at=3))
    assert trade.best_unrealised == pytest.approx(75.0)
    assert trade.worst_unrealised == pytest.approx(-10.0)


def test_a_fill_re_delivered_across_a_restart_is_not_counted_twice(durable_tmp_path):
    """Without this a venue's re-send after a restart opens a phantom position."""
    store = DurableStateStore(durable_tmp_path)
    before = PositionCloseDetector()
    before.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    store.save("position-close-detector", "positions", before.read_checkpoint_state(), SETTINGS)

    after = PositionCloseDetector()
    after.restore_from_checkpoint(
        store.restore("position-close-detector", "positions", SETTINGS).state
    )
    assert after.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1)) is None
    assert after.standing.fills_seen == 0
    trade = after.observe_fill(fill("f2", SELL, 110.0, 1.0, at=2))
    assert trade.quantity == pytest.approx(1.0), "the re-sent fill was counted"


def test_the_oldest_fill_ids_fall_out_of_the_window_first(durable_tmp_path):
    """Bounded on purpose: the books are written every fill, so the set cannot grow."""
    detector = PositionCloseDetector(remembered_fill_ids=3)
    for index in range(5):
        detector.observe_fill(fill(f"f{index}", BUY, 100.0, 1.0, at=1 + index))
    remembered = detector.read_checkpoint_state()["seen_fills"]
    assert remembered == ["f2", "f3", "f4"]


def test_a_cost_basis_survives_a_restart(durable_tmp_path):
    store = DurableStateStore(durable_tmp_path)
    before = CostBasisTracker()
    before.observe_fill(fill("f1", BUY, 90.0, 1.0, at=1))
    before.observe_fill(fill("f2", BUY, 110.0, 1.0, at=2))
    store.save("cost-basis-tracker", "lots", before.read_checkpoint_state(), SETTINGS)

    after = CostBasisTracker()
    assert after.restore_from_checkpoint(
        store.restore("cost-basis-tracker", "lots", SETTINGS).state
    ) == 1
    basis = after.read(VENUE, SYMBOL)
    assert basis.quantity == pytest.approx(2.0)
    assert basis.average_price == pytest.approx(100.0)
    # Both fees, because both fills built this position.
    assert basis.fees_paid == pytest.approx(1.1)


def test_a_restored_cost_basis_returns_to_flat_when_the_position_closes(durable_tmp_path):
    store = DurableStateStore(durable_tmp_path)
    before = CostBasisTracker()
    before.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    store.save("cost-basis-tracker", "lots", before.read_checkpoint_state(), SETTINGS)

    after = CostBasisTracker()
    after.restore_from_checkpoint(store.restore("cost-basis-tracker", "lots", SETTINGS).state)
    after.observe_fill(fill("f2", SELL, 110.0, 0.99, at=2))
    after.observe_fill(fill("f3", SELL, 110.0, 0.01, at=3))
    assert after.read(VENUE, SYMBOL).direction == FLAT


def test_a_narrowed_fill_window_is_applied_now_not_as_it_was_stored(durable_tmp_path):
    """An operator who narrows the window means it to apply to the restore too."""
    store = DurableStateStore(durable_tmp_path)
    before = PositionCloseDetector(remembered_fill_ids=10)
    for index in range(6):
        before.observe_fill(fill(f"f{index}", BUY, 100.0, 1.0, at=1 + index))
    store.save("position-close-detector", "positions", before.read_checkpoint_state(), SETTINGS)

    after = PositionCloseDetector(remembered_fill_ids=2)
    after.restore_from_checkpoint(
        store.restore("position-close-detector", "positions", SETTINGS).state
    )
    assert after.read_checkpoint_state()["seen_fills"] == ["f4", "f5"]


def test_nothing_open_restores_as_nothing_open_rather_than_failing(durable_tmp_path):
    store = DurableStateStore(durable_tmp_path)
    empty = PositionCloseDetector()
    store.save("position-close-detector", "positions", empty.read_checkpoint_state(), SETTINGS)

    after = PositionCloseDetector()
    assert after.restore_from_checkpoint(
        store.restore("position-close-detector", "positions", SETTINGS).state
    ) == 0
    assert after.standing.open_symbols == 0
