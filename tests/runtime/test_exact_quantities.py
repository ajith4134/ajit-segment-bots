"""A quantity subtracts exactly, so a position that is flat is flat.

The defect these pin: `1.0 - 0.99` is `0.010000000000000009` in float, so a
position closed in slices left a residue behind, `total_quantity` stayed above
zero, and the lot book never reached flat. No closed trade was ever emitted --
the position stayed open forever and the round trip could never be scored.

These are arithmetic, not market behaviour, so they are the one kind of test in
this project that needs no captured data (RL-063): there is no venue fact here to
get wrong, only Python's own subtraction.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from runtime.trading_types import Lot, LotBook, exact_quantity


def test_a_quantity_is_the_decimal_the_venue_meant_not_the_float_nearest_it():
    """`Decimal(str(x))`, not `Decimal(x)` -- the difference is the whole fix."""
    assert exact_quantity(0.99) == Decimal("0.99")
    assert exact_quantity(0.1) == Decimal("0.1")
    # Decimal(0.99) would be 0.9899999999999999911182158029987476766109466552734375,
    # which carries the float error in rather than leaving it outside.
    assert exact_quantity(0.99) != Decimal(0.99)


def test_subtracting_a_slice_leaves_no_residue():
    assert exact_quantity(1.0) - exact_quantity(0.99) == Decimal("0.01")
    assert float(exact_quantity(1.0) - exact_quantity(0.99)) == 0.01
    # The float this replaces, stated so the test says what it is defending against.
    assert 1.0 - 0.99 != 0.01


def test_a_quantity_that_is_already_exact_is_left_alone():
    assert exact_quantity(Decimal("0.01")) == Decimal("0.01")
    assert exact_quantity(3) == Decimal("3")


def test_a_lot_converts_a_float_quantity_at_its_door():
    """A float slipping in would reintroduce the residue silently."""
    lot = Lot(0.99, 2500.0, 1)
    assert isinstance(lot.quantity, Decimal)
    assert lot.quantity == Decimal("0.99")


def test_a_book_drained_in_two_slices_is_exactly_flat():
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    book.take(0.99)
    assert not book.is_flat
    book.take(0.01)
    assert book.is_flat
    assert book.total_quantity == 0
    assert book.lots == []


@pytest.mark.parametrize("slices", [2, 4, 5, 8, 10, 20, 100])
def test_a_book_drained_in_equal_slices_is_exactly_flat(slices):
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    piece = exact_quantity(1) / slices
    for _ in range(slices):
        book.take(piece)
    assert book.is_flat, f"{slices} slices left {book.total_quantity} behind"


def test_a_slice_that_is_not_a_finite_decimal_still_cannot_sum_back():
    """The boundary of the fix, written down rather than discovered later.

    Decimal removes *binary* representation error. It does not make a third of
    something representable -- 1/3 is no more finite in decimal than 1/10 is in
    binary -- so three exact thirds still leave a remainder behind.

    This is not a gap in production, and the reason is worth stating: a venue
    quantity is always a whole number of the symbol's quantity increment, and
    position-sizer snaps to that increment before an order exists. A third of a
    coin is not a quantity any venue can express, so it is not a quantity that
    can reach a lot book.

    What makes it safe rather than merely unlikely is that closing is driven by
    what is held, not by a computed fraction: `min(filled, book.total_quantity)`
    takes the whole remainder whenever the fill covers it. The test below pins
    that, and it is the property production actually relies on.
    """
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    third = exact_quantity(1) / 3
    for _ in range(3):
        book.take(third)
    assert not book.is_flat
    assert book.total_quantity == Decimal("1E-28")


def test_closing_against_what_is_held_always_reaches_flat():
    """However ragged the slices, taking the remainder empties the book exactly.

    This is the shape the close detector uses -- it never computes a final slice,
    it takes `min(fill, held)` -- which is why a dust remainder cannot strand a
    position no matter how the exit was chopped up.
    """
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    book.take(exact_quantity(1) / 3)
    book.take(exact_quantity(1) / 3)
    book.take(book.total_quantity)
    assert book.is_flat


def test_a_book_drained_by_a_ragged_tail_is_exactly_flat():
    """The shape a participation cap or a partial fill actually produces."""
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    for slice_size in (0.3, 0.3, 0.3, 0.07, 0.03):
        book.take(slice_size)
    assert book.is_flat


def test_taking_more_than_is_held_empties_the_book_and_no_more():
    book = LotBook()
    book.add(Lot(1.0, 2500.0, 1))
    taken = book.take(2.5)
    assert book.is_flat
    assert sum((used for _, used in taken), Decimal(0)) == Decimal("1.0")


def test_the_oldest_lot_is_consumed_first():
    book = LotBook()
    book.add(Lot(1.0, 100.0, 1))
    book.add(Lot(1.0, 200.0, 2))
    taken = book.take(1.0)
    assert [lot.price for lot, _ in taken] == [100.0]
    assert book.total_quantity == Decimal("1.0")


def test_the_average_price_stays_a_float_because_a_price_is_measured():
    """Quantities are counted and must be exact; prices are measured and are not.

    Dressing a price as a decimal would give it a precision the venue never had.
    """
    book = LotBook()
    book.add(Lot(1.0, 100.0, 1))
    book.add(Lot(3.0, 200.0, 2))
    assert isinstance(book.average_price, float)
    assert book.average_price == pytest.approx(175.0)


def test_an_empty_book_has_no_average_price():
    assert LotBook().average_price is None
