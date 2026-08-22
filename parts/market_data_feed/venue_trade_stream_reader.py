"""venue-trade-stream-reader: trades onto the tape, as the venue sent them.

**The tape starts here.** Every other part in this project can be built later
against a tape that already exists; this one is the reason a tape exists at all,
and every hour it is not running is an hour of history that cannot be recovered
by any means afterwards.

Pure transport, deterministic by design (RL-060): this part judges nothing. It
subscribes to what `stream-plan` assigned it, and writes what arrived, byte for
byte, with only the index fields the adapter reads out of each message. Anything
it decided would be a decision frozen into a tape that outlives the decision --
and spec §2.2 is explicit that normalisation happens on read, because a
normalisation bug in a reader is a fix while the same bug in the tape is
unrecoverable.

Trades only. The blueprint gives candles to `ccxt-venue-reader` and the book to
`order-book-reader`, and two parts subscribed to one topic would write every
message to the tape twice.

What it refuses to do quietly:

- A message it cannot read is counted and reported, never dropped. A venue adding
  an event type means this adapter is out of date, which is a fact the board needs
  rather than a silence to discover later.
- A venue whose fidelity is aggregated is recorded as aggregated. Binance offers
  no raw trade stream at all, and a tape that stored its 100 ms aggregates
  identically to Bybit's every-print would claim a resolution it does not have.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import PartHealth, run_part
from runtime.stream_plan import StreamPlan
from runtime.tape import StreamKind, TapeWriter, resolve_tape_root
from runtime.venues.stream_connection import VenueStreamConnection
from runtime.venues.venue_adapter import VenueAdapter, VenueMessageNotRecognised

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


@dataclass
class CaptureStanding:
    """What this reader has actually written, so the board never infers it.

    `unreadable_messages` is the one that matters most. It is the count of
    messages the adapter had no reading for -- the number that says this adapter
    has fallen behind the venue -- and it is deliberately separate from
    `control_frames`, which are acknowledgements and pongs that correctly belong
    on no tape.
    """

    venue_id: str
    records_written: int = 0
    control_frames: int = 0
    unreadable_messages: int = 0
    last_unreadable_reason: str | None = None
    symbols_written: set[str] = field(default_factory=set)

    @property
    def has_written_anything(self) -> bool:
        """False is a real state, not a warning to soften.

        A reader that has written nothing may be looking at a quiet market or at
        a connection opened to the wrong routed path, and only the connection's
        own standing separates them. Neither is 'fine'.
        """
        return self.records_written > 0


class TradeStreamReader:
    """One venue's trade streams, drained each tick and appended to the tape."""

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
        assignments = plan.assignments_for(adapter.venue_id, CAPTURED_STREAM_KIND)
        if not assignments:
            raise ValueError(
                f"the stream plan assigns {adapter.venue_id} no {CAPTURED_STREAM_KIND.name} "
                f"subscriptions. A reader with nothing to read would report healthy while "
                f"capturing nothing, which is the failure this whole block exists to catch."
            )
        self._adapter = adapter
        self._tape_root = resolve_tape_root(tape_root)
        self._writeback_interval_bytes = writeback_interval_bytes
        self._writers: dict[str, TapeWriter] = {}
        self._connections = [
            VenueStreamConnection(
                adapter=adapter,
                requests=assignment.requests,
                reconnect_backoff_floor_seconds=reconnect_backoff_floor_seconds,
                reconnect_backoff_ceiling_seconds=reconnect_backoff_ceiling_seconds,
                drain_interval_seconds=drain_interval_seconds,
            )
            for assignment in assignments
        ]
        self.standing = CaptureStanding(venue_id=adapter.venue_id)

    @property
    def connections(self) -> tuple[VenueStreamConnection, ...]:
        return tuple(self._connections)

    def capture_one_tick(self) -> int:
        """Drain every connection once and write what arrived. Returns records written.

        This is the part's whole tick. It never blocks longer than the drain
        interval each connection was given, so the control channel is answered
        promptly and the governor's off switch stays a switch (T-2).
        """
        written = 0
        for connection in self._connections:
            for payload in connection.drain():
                written += self._record_payload(payload)
        return written

    def _record_payload(self, payload: bytes) -> int:
        """Write one message to the tape, or account for why it was not written."""
        try:
            facts = self._adapter.read_message_facts(payload)
        except VenueMessageNotRecognised as unreadable:
            # Counted, not raised. A venue adding an event type must not end the
            # capture -- the tape's remaining symbols are still accruing history
            # that cannot be re-fetched, and this reader going down would cost
            # more than the message it could not read.
            self.standing.unreadable_messages += 1
            self.standing.last_unreadable_reason = str(unreadable)
            return 0
        except (ValueError, KeyError) as malformed:
            self.standing.unreadable_messages += 1
            self.standing.last_unreadable_reason = f"{type(malformed).__name__}: {malformed}"
            return 0

        if facts is None:
            self.standing.control_frames += 1
            return 0
        if facts.stream_kind is not CAPTURED_STREAM_KIND:
            # Another kind arriving here means this connection carries a topic
            # this part did not ask for. Counted rather than written: writing it
            # would duplicate whatever the reader that owns that kind is already
            # recording, and a tape with two copies of a message is worse than
            # one with none.
            self.standing.unreadable_messages += 1
            self.standing.last_unreadable_reason = (
                f"a {facts.stream_kind.name} message arrived on a "
                f"{CAPTURED_STREAM_KIND.name} connection"
            )
            return 0

        self._writer_for(facts.symbol).append(
            stream_kind=facts.stream_kind,
            payload=payload,
            venue_time_ns=facts.venue_time_ns,
            sequence=facts.sequence,
        )
        self.standing.records_written += 1
        self.standing.symbols_written.add(facts.symbol)
        return 1

    def _writer_for(self, symbol: str) -> TapeWriter:
        """One open tape per symbol, opened when its first message arrives.

        Opened lazily rather than for every planned symbol, because a file opened
        for a symbol that never trades is an empty tape that looks exactly like a
        symbol whose messages were lost.
        """
        writer = self._writers.get(symbol)
        if writer is None:
            writer = TapeWriter(
                self._tape_root, self._adapter.venue_id, symbol, self._writeback_interval_bytes
            )
            self._writers[symbol] = writer
        return writer

    def close(self) -> None:
        """Release every socket and every tape file. Safe to call twice.

        A part is SIGKILLed as the ordinary way of being switched off, so this is
        the tidy path rather than the one correctness depends on -- the tape
        survives a kill by construction (spec §2.2), and a writer that only
        flushed here would be a tape that only survived a polite shutdown.
        """
        for connection in self._connections:
            connection.close()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def __enter__(self) -> "TradeStreamReader":
        return self

    def __exit__(self, *exception) -> None:
        self.close()


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


def describe_capture(reader: TradeStreamReader) -> dict:
    """Everything measured about this reader, for a health report or the board.

    Rule 8: every number here was counted by something that ran. There is no
    'healthy' field, because health is what a reader would have to assert --
    these are what it observed, and a board decides what they mean.
    """
    return {
        "part_id": PART_ID,
        "venue_id": reader.standing.venue_id,
        "trade_fidelity": str(reader._adapter.trade_fidelity),
        "records_written": reader.standing.records_written,
        "symbols_written": sorted(reader.standing.symbols_written),
        "control_frames": reader.standing.control_frames,
        "unreadable_messages": reader.standing.unreadable_messages,
        "last_unreadable_reason": reader.standing.last_unreadable_reason,
        "connections": [
            {
                "url": connection.url,
                "topics": list(connection.topics),
                "opened_count": connection.standing.opened_count,
                "scheduled_close_count": connection.standing.scheduled_close_count,
                "unexpected_close_count": connection.standing.unexpected_close_count,
                "consecutive_failures": connection.standing.consecutive_failures,
                "messages_received": connection.standing.messages_received,
                "last_close_reason": connection.standing.last_close_reason,
            }
            for connection in reader.connections
        ],
    }


__all__ = [
    "CAPTURED_STREAM_KIND",
    "CaptureStanding",
    "PART_DECLARATION",
    "PART_ID",
    "PartHealth",
    "TradeStreamReader",
    "describe_capture",
    "run_trade_stream_reader",
]
