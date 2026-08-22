"""ccxt-venue-reader: 1-minute candles onto the tape, with the venue's own closed flag.

Same shape as the trade reader, one stream kind apart -- `runtime.stream_tape_recorder`
is the machinery, and what makes this a part is its declaration and its kind.

**Why the name says ccxt and the code does not use it.** The blueprint named this
part when the plan was to read candles through ccxt's unified interface, and the
blueprint is the source of truth for names (a design change is a blueprint edit
first). The stream half is deliberately not ccxt's, for the reason pinned in
`pyproject.toml`: the tape records raw venue payloads rather than a library's
normalisation (spec §2.2), and ccxt's OHLCV shape has already thrown away the
one field this part exists to preserve -- whether the candle is closed. ccxt
remains the project's REST client, which is what it was admitted for.

**The closed flag is the whole point.** Both venues push updates to the *current*
candle continuously -- Binance every 250 ms, Bybit every 1 to 60 seconds -- and
only one field says which update is the terminal one for that minute: `k.x` on
Binance, `confirm` on Bybit. A reader that took the last update it happened to
see would silently record a partial minute as a finished one, and every
indicator built on that would be wrong in a way nothing detects.

That flag is not in the tape's index, and that is deliberate. The index carries
what every stream has in common; the flag lives in the payload, which is stored
exactly as it arrived, so a reader recovers it by asking the adapter. Adding it
to the index would be normalising on write, which spec §2.2 forbids for the one
reason that matters: a mistake there is unrecoverable, and the same mistake in a
reader is a fix.
"""

from __future__ import annotations

import pathlib

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.stream_plan import StreamPlan
from runtime.stream_tape_recorder import StreamTapeRecorder, describe_recorder
from runtime.tape import StreamKind
from runtime.venues.venue_adapter import VenueAdapter

PART_ID = "ccxt-venue-reader"

PART_DECLARATION = PartDeclaration(
    part_id="ccxt-venue-reader",
    consumes=("stream-plan", "venue-standing"),
    produces=("market-data", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CAPTURED_STREAM_KIND = StreamKind.CANDLE


class CandleStreamReader(StreamTapeRecorder):
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


def is_closed_candle(adapter: VenueAdapter, payload: bytes) -> bool | None:
    """Whether one recorded candle message is the terminal update for its minute.

    Asked of the adapter rather than parsed here, because the two venues name it
    differently and neither name belongs in a part. None means the message is not
    a candle at all -- which a caller reading a mixed tape must be able to tell
    from a candle that is merely still open.
    """
    facts = adapter.read_message_facts(payload)
    if facts is None or facts.stream_kind is not StreamKind.CANDLE:
        return None
    return facts.is_closed_candle


def describe_capture(reader: StreamTapeRecorder) -> dict:
    return describe_recorder(reader, PART_ID)


def run_candle_stream_reader(
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
    """Run this part until the governor turns it off, capturing all the while."""
    reader = CandleStreamReader(
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


__all__ = [
    "CAPTURED_STREAM_KIND",
    "CandleStreamReader",
    "PART_DECLARATION",
    "PART_ID",
    "describe_capture",
    "is_closed_candle",
    "run_candle_stream_reader",
]
