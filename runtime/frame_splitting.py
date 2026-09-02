"""Splitting a set of levels into frames the bus will actually carry.

**A count is not a size.** Every sampler here bounded its frames by a symbol
count, and on 2026-09-02 that turned out to bound nothing: `price_frame_maximum_
symbols` is 2000 and is read by both price-level-sampler and quote-level-sampler,
but a price level encodes to about 53 bytes and a quote level to 86. At the cap
the price frame is 106,231 bytes and the quote frame is 172,280 against a
131,072-byte ceiling -- so for the quote frame the cap sat 480 symbols past the
size at which `encode_frame` refuses the message whole. A refused frame is not
split and not retried; the part goes on ticking and publishes nothing.

Nothing reported it, either: `messages_published` counted every refusal as a
publish. That is why the bound here is measured with the encoder that will carry
the frame rather than estimated from a per-item average -- an average is another
number that can be right when it is written and wrong when a field is added.

The count cap is kept as well, because it bounds something different: how much of
one venue's picture rides on a single datagram that a full buffer can drop. Both
apply, and whichever binds first wins.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Sequence

# A frame is measured as if it were the last one of the run rather than the
# first: `part_number`, `of_parts` and the bus sequence all pickle wider as they
# grow, and a probe measured at 1 would under-state the real frame by a few bytes
# at exactly the boundary this exists to respect. Measuring the widest case makes
# the probe an upper bound on what is actually sent, never a lower one.
WIDEST_SEQUENCE = 2**62


def batches_that_fit(
    items: Sequence,
    most_items_per_batch: int,
    maximum_bytes: int,
    size_of: Callable[[Sequence], int],
) -> tuple[tuple, ...]:
    """Split `items` into batches that each encode within `maximum_bytes`.

    `size_of` is asked what a batch would weigh on the wire; it is the caller's,
    because only the part knows what frame it wraps its items in.

    A single item that does not fit on its own is returned as its own batch. It
    cannot be made smaller by splitting, and swallowing it here would be a part
    silently dropping data -- the bus refuses it, counts it, and the refusal is
    the fact that belongs on a board. Splitting is for what splitting can fix.
    """
    if most_items_per_batch < 1:
        raise ValueError(
            "a batch carries at least one item; a bound below that publishes nothing "
            f"while looking like a working splitter. Got {most_items_per_batch!r}"
        )
    pending = deque(
        tuple(items[start : start + most_items_per_batch])
        for start in range(0, len(items), most_items_per_batch)
    )
    batches: list[tuple] = []
    while pending:
        batch = pending.popleft()
        if not batch:
            continue
        if len(batch) == 1 or size_of(batch) <= maximum_bytes:
            batches.append(batch)
            continue
        middle = len(batch) // 2
        # Pushed to the front, in order, so the batches come back in the order the
        # items arrived: a frame numbered 3 of 5 must hold the third slice of the
        # venue's symbols, not whichever slice the queue happened to reach last.
        pending.appendleft(batch[middle:])
        pending.appendleft(batch[:middle])
    return tuple(batches)


def frame_size_measured_by(
    data_type: str,
    producer_part_id: str,
    build_payload: Callable[[Sequence, int, int], object],
) -> Callable[[Sequence], int]:
    """A `size_of` that weighs a batch as the bus will weigh it.

    The payload is built the same way the part builds the real one, so a field
    added to a frame changes this measurement on the same day it changes the
    frame -- which a bytes-per-item constant would not.
    """
    from runtime.bus import build_frame

    def size_of(batch: Sequence) -> int:
        payload = build_payload(batch, WIDEST_SEQUENCE, WIDEST_SEQUENCE)
        return len(
            build_frame(
                data_type=data_type,
                producer_part_id=producer_part_id,
                sequence=WIDEST_SEQUENCE,
                published_at_ns=WIDEST_SEQUENCE,
                payload=payload,
            )
        )

    return size_of


__all__ = ["WIDEST_SEQUENCE", "batches_that_fit", "frame_size_measured_by"]
