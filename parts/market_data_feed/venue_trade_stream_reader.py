"""venue-trade-stream-reader: trades onto the tape, as the venue sent them.

**The tape starts here.** Every other part in this project can be built later
against a tape that already exists; this one is the reason a tape exists at all,
and every hour it is not running is an hour of history that cannot be recovered
by any means afterwards.

Pure transport, deterministic by design (RL-060): this part judges nothing. It
subscribes to what `stream-plan` assigned it and writes what arrived, byte for
byte, with only the index fields the adapter reads out of each message. Anything
it decided would be a decision frozen into a tape that outlives the decision --
and spec §2.2 is explicit that normalisation happens on read, because a
normalisation bug in a reader is a fix while the same bug in the tape is
unrecoverable.

Trades only. The blueprint gives candles to `ccxt-venue-reader` and the book to
`order-book-reader`, and two parts subscribed to one topic would write every
message to the tape twice.

The machinery is `runtime.stream_tape_recorder`, shared with the other two
readers because they differ in exactly one thing -- which stream kind they carry
-- and identical code written three times is three places for one bug to be fixed
twice. What makes this a part rather than a call is its declaration and its kind.

Fidelity travels with what it writes: Binance offers no raw trade stream at all,
only 100 ms aggregates, and a tape that stored those identically to Bybit's
every-print would claim a resolution it does not have (spec §7).
"""

from __future__ import annotations

import pathlib

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import PartHealth, run_part
from runtime.stream_plan import StreamPlan
from runtime.stream_tape_recorder import CaptureStanding, StreamTapeRecorder, describe_recorder
from runtime.tape import StreamKind
from runtime.venues.venue_adapter import VenueAdapter

PART_ID = "venue-trade-stream-reader"

# Read without importing this module (RL-070). Its consumes and produces equal
# the blueprint's, which is what RL-067 requires and what the wiring probe checks.
PART_DECLARATION = PartDeclaration(
    part_id="venue-trade-stream-reader",
    consumes=("stream-plan", "venue-standing"),
    produces=("market-data", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The stream kind this part carries. Named rather than passed in: which reader
# reads which stream is a blueprint fact, not a runtime choice.
CAPTURED_STREAM_KIND = StreamKind.TRADE


class TradeStreamReader(StreamTapeRecorder):
    """This part's recorder, fixed to the one stream kind the blueprint gives it."""

    def __init__(
        self,
        adapter: VenueAdapter,
        plan: StreamPlan,
        tape_root: pathlib.Path,
        writeback_interval_bytes: int,
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
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


def describe_capture(reader: StreamTapeRecorder) -> dict:
    return describe_recorder(reader, PART_ID)


def run_trade_stream_reader(
    adapter: VenueAdapter,
    plan: StreamPlan,
    control_socket,
    tape_root: pathlib.Path,
    writeback_interval_bytes: int,
    reconnect_backoff_floor_seconds: float,
    reconnect_backoff_ceiling_seconds: float,
    drain_interval_seconds: float,
    health_interval_seconds: float,
    emit_health,
) -> int:
    """Run this part until the governor turns it off, capturing all the while.

    The adapter is passed in rather than looked up: no part imports a venue
    module, and which venues are captured is a settings question answered by
    whoever launches this (spec §3.1).
    """
    reader = TradeStreamReader(
        adapter=adapter,
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=writeback_interval_bytes,
        reconnect_backoff_floor_seconds=reconnect_backoff_floor_seconds,
        reconnect_backoff_ceiling_seconds=reconnect_backoff_ceiling_seconds,
        drain_interval_seconds=drain_interval_seconds,
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




def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Two things happen per message and their order is fixed: it goes on the tape,
    then it is published. The tape is the record that cannot be rebuilt, so a crash
    between the two must lose the publication rather than the history.

    **The plan comes from the bus, never from here.** This part consumes
    `stream-plan`, and until one arrives it subscribes to nothing and says so. A
    reader that built its own plan would be a part deciding what to capture, which
    is `stream-budget-planner`'s job -- and the version of this that shipped in
    phase 1 lives in `operate/start_trade_capture.py` precisely because that part
    did not exist yet.

    Normalisation happens here rather than in the 65 parts that consume
    `market-data`: the tape keeps the venue's bytes, the adapter turns them into a
    `NormalisedTrade`, and no consumer ever sees a venue's phrasing.
    """
    from runtime.input_assembly import LatestValue
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE_NAME]
    adapters = {adapter.venue_id: adapter for adapter in load_captured_venue_adapters(settings)}
    tape_root = pathlib.Path(str(settings.entries["tape_root"].value)).expanduser()
    publish_trades = context.bus.publisher_for("market-data")
    plans = LatestValue(read=context.bus.reader("stream-plan"))

    readers: dict[str, TradeStreamReader] = {}
    planned = [None]

    def rebuild_readers_if_the_plan_changed() -> None:
        plan = plans.value()
        # Compared by value, never by identity. stream-budget-planner re-plans on
        # every tick and publishes a fresh object each time even when nothing
        # changed, so an identity check tore down and reopened every venue
        # connection once a second -- which on the first live run left consumers
        # receiving 3.5 messages a second out of 516 written to the tape.
        if plan is None or plan == planned[0]:
            return
        for reader in readers.values():
            reader.close()
        readers.clear()
        for venue_id, adapter in sorted(adapters.items()):
            if not plan.assignments_for(venue_id, CAPTURED_STREAM_KIND):
                continue  # this plan gives that venue nothing; not this part's call
            readers[venue_id] = TradeStreamReader(
                adapter=adapter,
                plan=plan,
                tape_root=tape_root,
                writeback_interval_bytes=int(context.number("writeback_interval")),
                reconnect_backoff_floor_seconds=context.number("venue_reconnect_backoff_floor"),
                reconnect_backoff_ceiling_seconds=context.number("venue_reconnect_backoff_ceiling"),
                drain_interval_seconds=context.number("stream_drain_interval"),
            )
        planned[0] = plan

    def capture_and_publish() -> None:
        rebuild_readers_if_the_plan_changed()
        for venue_id, reader in readers.items():
            adapter = adapters[venue_id]
            reader.capture_one_tick(
                on_recorded_payload=lambda payload, adapter=adapter: publish_trades(
                    adapter.read_trades(payload)
                )
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
        )
    finally:
        for reader in readers.values():
            reader.close()


__all__ = [
    "CAPTURED_STREAM_KIND",
    "CaptureStanding",
    "PART_DECLARATION",
    "PART_ID",
    "PartHealth",
    "TradeStreamReader",
    "describe_capture",
    "run_trade_stream_reader",
    "start_part",
]
