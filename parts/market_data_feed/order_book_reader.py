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


def describe_all_captures(readers: dict) -> dict:
    """One standing across every venue this part reads, keyed so both survive.

    The live part runs one recorder per venue in one process, and a heartbeat's
    standing keeps only top-level numbers -- so each venue's facts ride under a
    suffixed key and the totals under the plain one. Same shape as the trade
    reader's, and written for the same reason: this part had published 3,248,775
    snapshots and reported `{}` (2026-08-27).

    What only this part can drop rides too. A reader thinning nine of ten pushes
    produces a tape indistinguishable from a venue pushing a tenth as often, so
    what was thinned is a number on the board rather than a silence.
    """
    merged: dict = {"part_id": PART_ID, "venues": len(readers)}
    totals = {
        "records_written": 0,
        "unreadable_messages": 0,
        "symbols_written": 0,
        "thinned_messages": 0,
        "venues_thinning": 0,
    }
    for venue_id, reader in sorted(readers.items()):
        one = describe_recorder(reader, PART_ID)
        symbols = len(one["symbols_written"])
        unexpected = sum(c["unexpected_close_count"] for c in one["connections"])
        merged[f"records_written.{venue_id}"] = one["records_written"]
        merged[f"symbols_written.{venue_id}"] = symbols
        merged[f"unreadable_messages.{venue_id}"] = one["unreadable_messages"]
        merged[f"unexpected_closes.{venue_id}"] = unexpected
        merged[f"thinned_messages.{venue_id}"] = reader.thinned_messages
        merged[f"thins_this_venue.{venue_id}"] = reader.thins_this_venue
        totals["records_written"] += one["records_written"]
        totals["unreadable_messages"] += one["unreadable_messages"]
        totals["symbols_written"] += symbols
        totals["thinned_messages"] += reader.thinned_messages
        totals["venues_thinning"] += 1 if reader.thins_this_venue else 0
    merged.update(totals)
    return merged


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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
            input_descriptors=input_descriptors,
            tick_floor_seconds=tick_floor_seconds,
            read_standing=lambda: describe_capture(reader),
        )
    finally:
        reader.close()


__all__ = [
    "CAPTURED_STREAM_KIND",
    "OrderBookReader",
    "PART_DECLARATION",
    "PART_ID",
    "describe_all_captures",
    "describe_capture",
    "run_order_book_reader",
    "start_part",
]


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The tape gets the venue's bytes, thinned only where that is safe; the bus
    gets the *book* -- every update applied by runtime.order_book.OrderBookKeeper,
    so a consumer reads a top-N it can use rather than a delta it would have to
    keep a book from. A delta the keeper cannot apply publishes nothing, and the
    keeper's own count says how many that was.
    """
    from runtime.input_assembly import LatestValue
    from runtime.order_book import OrderBookKeeper
    from runtime.part_context import RUNTIME_SCOPE
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE]
    adapters = {adapter.venue_id: adapter for adapter in load_captured_venue_adapters(settings)}
    tape_root = pathlib.Path(str(settings.entries["tape_root"].value)).expanduser()
    publish_books = context.bus.publisher_for("order-book-snapshot")
    plans = LatestValue(read=context.bus.reader("stream-plan"))
    standings = LatestValue(read=context.bus.reader("venue-standing"))
    keeper = OrderBookKeeper(depth_levels=int(context.number("book_depth_levels")))

    readers: dict[str, OrderBookReader] = {}
    planned = [None]

    def rebuild_readers_if_the_plan_changed() -> None:
        plan = plans.value()
        standings.value()
        if plan is None or plan == planned[0]:
            return
        for reader in readers.values():
            reader.close()
        readers.clear()
        for venue_id, adapter in sorted(adapters.items()):
            if not plan.assignments_for(venue_id, CAPTURED_STREAM_KIND):
                continue
            readers[venue_id] = OrderBookReader(
                adapter=adapter,
                plan=plan,
                tape_root=tape_root,
                writeback_interval_bytes=int(context.number("writeback_interval")),
                reconnect_backoff_floor_seconds=context.number("venue_reconnect_backoff_floor"),
                reconnect_backoff_ceiling_seconds=context.number("venue_reconnect_backoff_ceiling"),
                drain_interval_seconds=context.number("stream_drain_interval"),
                book_snapshot_interval_seconds=context.number("book_snapshot_interval"),
            )
        planned[0] = plan

    def on_recorded(payload: bytes, adapter) -> None:
        update = adapter.read_book_update(payload)
        if update is None:
            return
        book = keeper.apply(update)
        if book is not None:
            publish_books((book,))

    def capture_and_publish() -> None:
        rebuild_readers_if_the_plan_changed()
        for venue_id, reader in readers.items():
            adapter = adapters[venue_id]
            reader.capture_one_tick(
                on_recorded_payload=lambda payload, adapter=adapter: on_recorded(payload, adapter)
            )

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=capture_and_publish,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=lambda: describe_all_captures(readers),
        )
    finally:
        for reader in readers.values():
            reader.close()
