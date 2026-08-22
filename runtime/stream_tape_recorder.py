"""One stream kind, from one venue, onto the tape. The shape every reader is.

T-1 says every feature is the same shape, and the three streaming readers of this
block -- trades, candles, the book -- differ in exactly one thing: which stream
kind they carry. Everything else is identical, and identical code written three
times is three places for the same bug to be fixed twice.

So this is the machinery, and a part is its declaration plus its stream kind. It
lives in the substrate rather than in a part because a part that imported another
part to share code would be wired to it, which is what T-4 forbids.

What it will not do quietly:

- **A message of the wrong kind is counted, never written.** Two readers writing
  one message would put two copies of it on the tape, and a duplicate is
  indistinguishable from a real second print.
- **A message the adapter cannot read is counted with its reason.** A venue
  adding an event type means the adapter is out of date, which is a fact the
  board needs rather than a silence to find later.
- **A reader with nothing assigned is refused at construction.** It would report
  healthy while capturing nothing, which is the failure this block exists to catch.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from runtime.stream_plan import StreamPlan
from runtime.tape import StreamKind, TapeWriter, resolve_tape_root
from runtime.venues.stream_connection import VenueStreamConnection
from runtime.venues.venue_adapter import VenueAdapter, VenueMessageNotRecognised


@dataclass
class CaptureStanding:
    """What this reader has actually written, so the board never infers it.

    `unreadable_messages` is the one that matters most: it counts messages the
    adapter had no reading for, which is the number that says an adapter has
    fallen behind its venue. It is deliberately separate from `control_frames`,
    which are acknowledgements and pongs that correctly belong on no tape.
    """

    venue_id: str
    stream_kind: str
    records_written: int = 0
    control_frames: int = 0
    unreadable_messages: int = 0
    wrong_kind_messages: int = 0
    last_unreadable_reason: str | None = None
    symbols_written: set[str] = field(default_factory=set)

    @property
    def has_written_anything(self) -> bool:
        """False is a real state, not a warning to soften.

        A reader that has written nothing may be watching a quiet market or a
        connection opened to the wrong routed path, and only the connection's own
        standing separates them. Neither of those is 'fine' (Rule 8).
        """
        return self.records_written > 0


class StreamTapeRecorder:
    """Drains one venue's connections each tick and appends what arrived."""

    def __init__(
        self,
        adapter: VenueAdapter,
        plan: StreamPlan,
        stream_kind: StreamKind,
        tape_root: pathlib.Path,
        writeback_interval_bytes: int,
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
    ) -> None:
        assignments = plan.assignments_for(adapter.venue_id, stream_kind)
        if not assignments:
            raise ValueError(
                f"the stream plan assigns {adapter.venue_id} no {stream_kind.name} "
                f"subscriptions. A reader with nothing to read would report healthy while "
                f"capturing nothing, which is the failure this whole block exists to catch."
            )
        self._adapter = adapter
        self._stream_kind = stream_kind
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
        self.standing = CaptureStanding(venue_id=adapter.venue_id, stream_kind=stream_kind.name)

    @property
    def adapter(self) -> VenueAdapter:
        return self._adapter

    @property
    def stream_kind(self) -> StreamKind:
        return self._stream_kind

    @property
    def connections(self) -> tuple[VenueStreamConnection, ...]:
        return tuple(self._connections)

    def capture_one_tick(self, on_recorded_payload=None) -> int:
        """Drain every connection once and write what arrived. Returns records written.

        Never blocks longer than the drain interval each connection was given, so
        the control channel is answered promptly and the governor's off switch
        stays a switch (T-2).

        on_recorded_payload, when given, is called with each payload that reached
        the tape -- and only after it did. The order is deliberate: the tape is the
        record that cannot be rebuilt, and publishing before writing would mean a
        crash between the two lost a message the rest of the system had already
        acted on. A payload the tape refused is never handed on.
        """
        written = 0
        for connection in self._connections:
            for payload in connection.drain():
                recorded = self._record_payload(payload)
                written += recorded
                if recorded and on_recorded_payload is not None:
                    on_recorded_payload(payload)
        return written

    def _record_payload(self, payload: bytes) -> int:
        """Write one message to the tape, or account for why it was not written."""
        try:
            facts = self._adapter.read_message_facts(payload)
        except VenueMessageNotRecognised as unreadable:
            # Counted, not raised. A venue adding an event type must not end the
            # capture -- every other symbol on this connection is still accruing
            # history that cannot be re-fetched.
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
        if facts.stream_kind is not self._stream_kind:
            self.standing.wrong_kind_messages += 1
            self.standing.last_unreadable_reason = (
                f"a {facts.stream_kind.name} message arrived on a "
                f"{self._stream_kind.name} connection"
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

        Lazily rather than for every planned symbol: a file opened for a symbol
        that never trades is an empty tape, which looks exactly like a symbol
        whose messages were lost.
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

        The tidy path, not the one correctness depends on: a part is SIGKILLed as
        the ordinary way of being switched off, and the tape survives that by
        construction (spec §2.2, and the writer is unbuffered so records reach
        the kernel as they are appended).
        """
        for connection in self._connections:
            connection.close()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def __enter__(self) -> "StreamTapeRecorder":
        return self

    def __exit__(self, *exception) -> None:
        self.close()


def describe_recorder(recorder: StreamTapeRecorder, part_id: str) -> dict:
    """Everything measured about this reader, for a health report or the board.

    Rule 8: every number here was counted by something that ran. There is no
    'healthy' field, because health is what a reader would have to assert --
    these are what it observed, and a board decides what they mean.
    """
    return {
        "part_id": part_id,
        "venue_id": recorder.standing.venue_id,
        "stream_kind": recorder.standing.stream_kind,
        "trade_fidelity": str(recorder.adapter.trade_fidelity),
        "records_written": recorder.standing.records_written,
        "symbols_written": sorted(recorder.standing.symbols_written),
        "control_frames": recorder.standing.control_frames,
        "unreadable_messages": recorder.standing.unreadable_messages,
        "wrong_kind_messages": recorder.standing.wrong_kind_messages,
        "last_unreadable_reason": recorder.standing.last_unreadable_reason,
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
            for connection in recorder.connections
        ],
    }
