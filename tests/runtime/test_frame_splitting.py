"""Splitting a frame against what the bus will carry, not against a count.

The failure this exists to stop, measured 2026-09-02: `price_frame_maximum_symbols`
is 2000 and is read by price-level-sampler and quote-level-sampler alike, but a
quote level encodes to 86 bytes against a price level's 53. At the cap the quote
frame is 172,280 bytes and the bus refuses anything over 131,072 -- so the cap sat
480 symbols past the point where every frame is dropped whole, and the part went on
ticking while publishing nothing.
"""

from __future__ import annotations

import pytest

from runtime.bus import build_frame
from runtime.frame_splitting import batches_that_fit, frame_size_measured_by

MAXIMUM_MESSAGE_BYTES = 131_072


def _size_of_each_item_is(bytes_per_item: int):
    """A stand-in weigher, so a test can state the size it is exercising."""
    return lambda batch: len(batch) * bytes_per_item


def test_a_batch_within_the_bound_is_left_whole():
    batches = batches_that_fit(
        tuple(range(10)), most_items_per_batch=100, maximum_bytes=1000,
        size_of=_size_of_each_item_is(10),
    )
    assert batches == (tuple(range(10)),)


def test_a_count_cap_that_is_too_generous_is_still_split_by_bytes():
    """The cap allows all 100 in one batch; the bytes do not."""
    batches = batches_that_fit(
        tuple(range(100)), most_items_per_batch=100, maximum_bytes=250,
        size_of=_size_of_each_item_is(10),
    )
    assert len(batches) > 1
    for batch in batches:
        assert len(batch) * 10 <= 250
    assert sum(len(batch) for batch in batches) == 100


def test_splitting_keeps_every_item_and_keeps_them_in_order():
    """A frame numbered 3 of 5 must hold the third slice, not whichever came last."""
    batches = batches_that_fit(
        tuple(range(1000)), most_items_per_batch=1000, maximum_bytes=100,
        size_of=_size_of_each_item_is(7),
    )
    rejoined = [item for batch in batches for item in batch]
    assert rejoined == list(range(1000))


def test_the_count_cap_still_binds_when_it_is_the_smaller_bound():
    batches = batches_that_fit(
        tuple(range(10)), most_items_per_batch=3, maximum_bytes=10_000,
        size_of=_size_of_each_item_is(1),
    )
    assert [len(batch) for batch in batches] == [3, 3, 3, 1]


def test_one_item_too_large_to_send_is_returned_rather_than_dropped():
    """Splitting is for what splitting can fix.

    A single item over the ceiling cannot be made smaller by cutting the batch,
    and swallowing it here would be a part quietly losing data. It goes out, the
    bus refuses it, and the refusal is the fact that belongs on a board.
    """
    batches = batches_that_fit(
        ("an item nothing can shrink",), most_items_per_batch=10, maximum_bytes=1,
        size_of=_size_of_each_item_is(10_000),
    )
    assert batches == (("an item nothing can shrink",),)


def test_a_batch_bound_below_one_item_is_refused():
    with pytest.raises(ValueError):
        batches_that_fit((1, 2), most_items_per_batch=0, maximum_bytes=10, size_of=len)


def test_the_measured_size_is_the_size_the_bus_will_see():
    """Not an estimate: the same encoder that carries the frame weighs it."""
    size_of = frame_size_measured_by(
        data_type="symbol-price-frame",
        producer_part_id="price-level-sampler",
        build_payload=lambda batch, part_number, of_parts: (tuple(batch), part_number, of_parts),
    )
    batch = tuple(f"SYMBOL{index:05d}USDT" for index in range(50))
    measured = size_of(batch)
    actually = len(
        build_frame(
            data_type="symbol-price-frame",
            producer_part_id="price-level-sampler",
            sequence=1,
            published_at_ns=1,
            payload=(batch, 1, 1),
        )
    )
    # The probe measures the widest case on purpose, so it is never under.
    assert measured >= actually
