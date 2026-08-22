"""order-book-reader: the shallow book onto the tape, at the depth settings ask for.

Same machinery as the other two readers (`runtime.stream_tape_recorder`), with
one thing none of them needs: **it may not thin every venue's stream.**

`book_snapshot_interval` says how often a book snapshot is written, as distinct
from how often the venue pushes. Whether that interval can be honoured at all is
a venue question:

- Binance's partial-depth stream sends a **fresh top-N every push**, so recording
  one message every few seconds costs resolution and nothing else.
- Bybit sends **one snapshot and then deltas**, and nothing will resend the
  snapshot unless the venue itself decides to. Dropping a delta there does not
  cost resolution, it costs the book: every later price is wrong and nothing in
  the record says so. So a delta stream is recorded whole, the interval does not
  apply to it, and that is reported rather than silently ignored.

Every message not written is counted. A reader that quietly dropped nine of ten
pushes would produce a tape indistinguishable from a venue that pushed a tenth as
often -- no silent caps, so what was thinned is a number on the board.

The depth actually served is the adapter's answer, not the setting's value: the
two venues offer different ladders (5/10/20 against 1/50/200/1000), so a request
for 20 is served at 20 on one and 50 on the other, and the topic records which.
"""

from __future__ import annotations

import pathlib
import time

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.stream_plan import StreamPlan
from runtime.stream_tape_recorder import StreamTapeRecorder, describe_recorder
from runtime.tape import StreamKind
from runtime.venues.venue_adapter import VenueAdapter, VenueMessageNotRecognised

PART_ID = "order-book-reader"

PART_DECLARATION = PartDeclaration(
    part_id="order-book-reader",
    consumes=("stream-plan", "venue-standing"),
    produces=("order-book-snapshot", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CAPTURED_STREAM_KIND = StreamKind.BOOK


class OrderBookReader(StreamTapeRecorder):
    """Records the book, thinning to the snapshot interval only where that is safe."""

    def __init__(
        self,
        adapter: VenueAdapter,
        plan: StreamPlan,
        tape_root: pathlib.Path,
        writeback_interval_bytes: int,
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
        book_snapshot_interval_seconds: float,
        monotonic=time.monotonic,
    ) -> None:
        super().__init__(
            adapter=adapter,
            plan=plan,
            stream_kind=CAPTURED_STREAM_KIND,
            tape_root=tape_root,
            writeback_interval_bytes=writeback_interval_bytes,
            reconnect_backoff_floor_seconds=reconnect_backoff_floor_seconds,
            reconnect_backoff_ceiling_seconds=reconnect_backoff_ceiling_seconds,
            drain_interval_seconds=drain_interval_seconds,
        )
        self._snapshot_interval = book_snapshot_interval_seconds
        self._monotonic = monotonic
        self._may_thin = adapter.book_stream_delivers_full_depth()
        self._last_written_at: dict[str, float] = {}
        self.thinned_messages = 0

    @property
    def thins_this_venue(self) -> bool:
        """Whether the snapshot interval applies to this venue's stream at all."""
        return self._may_thin

    def _record_payload(self, payload: bytes) -> int:
        """Write, unless this venue's stream may be thinned and this push is early.

        The thinning decision comes before the write and after the read, so a
        message that is dropped is still one the adapter understood -- an
        unreadable message is never mistaken for a thinned one.
        """
        if not self._may_thin:
            return super()._record_payload(payload)

        try:
            facts = self._adapter.read_message_facts(payload)
        except (VenueMessageNotRecognised, ValueError, KeyError):
            return super()._record_payload(payload)
        if facts is None or facts.stream_kind is not CAPTURED_STREAM_KIND:
            return super()._record_payload(payload)

        now = self._monotonic()
        last = self._last_written_at.get(facts.symbol)
        # A resync is written whatever the interval says. A book rebuilt after a
        # gap is not the same object as one that never gapped (spec §6), and the
        # message that says so is the only place the tape can record it.
        if last is not None and not facts.resets_sequence and now - last < self._snapshot_interval:
            self.thinned_messages += 1
            return 0

        written = super()._record_payload(payload)
        if written:
            self._last_written_at[facts.symbol] = now
        return written


def describe_capture(reader: OrderBookReader) -> dict:
    """The recorder's own numbers, plus what this part alone can drop."""
    description = describe_recorder(reader, PART_ID)
    description["thins_this_venue"] = reader.thins_this_venue
    description["thinned_messages"] = reader.thinned_messages
    description["book_snapshot_interval_seconds"] = reader._snapshot_interval
    return description


def run_order_book_reader(
    adapter: VenueAdapter,
    plan: StreamPlan,
    control_socket,
    tape_root: pathlib.Path,
    writeback_interval_bytes: int,
    reconnect_backoff_floor_seconds: float,
    reconnect_backoff_ceiling_seconds: float,
    drain_interval_seconds: float,
    book_snapshot_interval_seconds: float,
    health_interval_seconds: float,
    emit_health,
) -> int:
    """Run this part until the governor turns it off, capturing all the while."""
    reader = OrderBookReader(
        adapter=adapter,
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=writeback_interval_bytes,
        reconnect_backoff_floor_seconds=reconnect_backoff_floor_seconds,
        reconnect_backoff_ceiling_seconds=reconnect_backoff_ceiling_seconds,
        drain_interval_seconds=drain_interval_seconds,
        book_snapshot_interval_seconds=book_snapshot_interval_seconds,
    )
    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=control_socket,
            do_one_tick=reader.capture_one_tick,
            emit_health=emit_health,
            health_interval_seconds=health_interval_seconds,
        )
    finally:
        reader.close()


__all__ = [
    "CAPTURED_STREAM_KIND",
    "OrderBookReader",
    "PART_DECLARATION",
    "PART_ID",
    "describe_capture",
    "run_order_book_reader",
]
