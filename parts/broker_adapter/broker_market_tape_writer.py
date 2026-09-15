"""broker-market-tape-writer: persist every decomposed broker feed record to
the tape before anything else reads it.

History accrues only in real time for this segment too -- same reasoning
that made market-data-feed the crypto build's first vertical (RL-068).
Routes each broker record type to the StreamKind added for it; the tape
format itself (runtime/tape.py) is unmodified shared substrate.

**One real divergence from the crypto reader, stated plainly rather than
copied silently:** venue_trade_stream_reader.py writes the *raw venue
payload* to the tape and normalises only on read, because each raw
Binance/Bybit message already carries exactly one StreamKind. Upstox's feed
doesn't have that property -- one raw protobuf message can bundle LTP, book,
OHLC, open interest and greeks together, and the blueprint already commits
this part to consuming the five *decomposed* bus types, not a raw payload.
So `payload` here is a JSON encoding of the already-normalised record this
part received, not the broker's raw bytes.

**Written under trading_symbol, resolved from instrument_key (2026-09-08).**
The five decoded record kinds this part consumes only ever carry Upstox's own
`instrument_key` -- broker_history_reader.py's own `_symbol_by_key` docstring
states why: that is genuinely all the raw feed has, T-4's "the venue's own
naming, translated by a bridge" applying here as much as anywhere else. But
every *reader* of this tape -- dashboard/build_trade_board.py's
`read_last_price`, called with a Position's own `symbol` -- looks the file up
by `trading_symbol`, the shared name broker-symbol-universe-bridge's own
docstring calls out as "the shared name" for exactly this reason. Writing
under instrument_key was a silent second convention this file invented for
itself: real incident, 2026-09-08, ten open stock-options positions with real
capital in them had a live tick every second under
`tape/upstox/NSE_FO|56316/...` and `NOT MEASURED` on the board, because the
board asked for `tape/upstox/AXISBANK 1260 CE 29 SEP 26/...` and nothing was
there. Resolved via `broker-subscribed-instrument-listing`, the same type
`expiry-day-zero-to-hero-detector` already reads for the same purpose. A tick
for a key not yet resolved still writes -- under the instrument_key, as
before -- rather than being dropped; `unresolved_writes` says how often, so a
resolution gap that never closes is visible rather than silently wrong.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

MILLISECONDS_TO_NANOSECONDS = 1_000_000

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import StreamKind, TapeWriter

PART_ID = "broker-market-tape-writer"

PART_DECLARATION = PartDeclaration(
    part_id="broker-market-tape-writer",
    consumes=(
        "broker-candle", "broker-market-data", "broker-open-interest",
        "broker-option-greeks", "broker-order-book-snapshot",
        "broker-subscribed-instrument-listing",
    ),
    produces=("part-health",),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


def _kind_by_type_name():
    # Built lazily against the real classes rather than imported at module
    # scope, so this file has no import-order dependency on broker_adapter.
    from runtime.brokers.broker_adapter import (
        BrokerCandle, BrokerOpenInterest, BrokerOptionGreeks,
        BrokerOrderBookUpdate, LtpUpdate,
    )

    return {
        LtpUpdate: StreamKind.TRADE,
        BrokerCandle: StreamKind.CANDLE,
        BrokerOrderBookUpdate: StreamKind.BOOK,
        BrokerOpenInterest: StreamKind.OPEN_INTEREST,
        BrokerOptionGreeks: StreamKind.OPTION_GREEKS,
    }


_KIND_BY_TYPE = _kind_by_type_name()


def stream_kind_for(record_type: type) -> StreamKind:
    return _KIND_BY_TYPE[record_type]


def venue_time_ns_of(record) -> int:
    """Every decomposed record kind carries its own broker timestamp, but not
    the same field: `BrokerCandle` is the one kind Upstox's own OHLC entry
    states in milliseconds (`bar_time_ms`, the .proto's own `ts`) rather than
    the `broker_time_ns` every other kind carries -- real bug, 2026-09-02:
    write_one() read `record.broker_time_ns` unconditionally and crashed the
    instant a real candle reached the tape writer, the first time the feed
    ever decoded one (the mode="full_d5" subscribe bug meant it never had
    before). broker_candle_bridge.py already does this same ms->ns
    conversion for the same reason.
    """
    from runtime.brokers.broker_adapter import BrokerCandle

    if isinstance(record, BrokerCandle):
        return record.bar_time_ms * MILLISECONDS_TO_NANOSECONDS
    return record.broker_time_ns


def _payload_for(record) -> bytes:
    """JSON encoding of this project's own normalised record -- not the
    broker's raw bytes (module docstring explains why that differs from the
    crypto tape's own convention)."""
    return json.dumps(dataclasses.asdict(record), default=str).encode("utf-8")


def observe_subscribed_listing(symbol_by_key: dict[str, str], listing) -> None:
    """Learn one instrument_key -> trading_symbol mapping.

    Last one wins, matching every other instrument_key-keyed dict in this
    project -- a listing republished with the same key simply restates the
    same mapping in practice, since an instrument_key never changes trading
    symbol mid-life.
    """
    symbol_by_key[listing.instrument_key] = listing.trading_symbol


def symbol_for(symbol_by_key: dict[str, str], instrument_key: str) -> str:
    """The trading_symbol this instrument_key resolves to, or the
    instrument_key itself when nothing has resolved it yet.

    The fallback exists so a tick that arrives before its listing is known
    still gets written -- real data, just temporarily unlabelled -- rather
    than dropped. Callers count how often this happens (`unresolved_writes`):
    a rate that never falls to zero is a resolution bug, not a cold start.
    """
    return symbol_by_key.get(instrument_key, instrument_key)


class TapeWritersByInstrument:
    """One TapeWriter per (name, StreamKind), named by trading symbol once known.

    A tick that arrives before its listing is written under its instrument key
    (`symbol_for`). **When the key resolves, the writers opened under it are
    closed** (2026-09-15). They were held for the rest of the run beside the
    symbol-named writers that replaced them: the listing reaches every subscribed
    instrument over half an hour after a start, so every instrument opened both,
    about 20,000 writers at 13.7 KB each -- measured 263 MB of process memory
    climbing 12 MB a minute, and the part was OOM-killed in its 512 MB scope 19
    minutes after the market-hours start that morning.
    """

    def __init__(self, tape_root: pathlib.Path, writeback_interval_bytes: int, venue: str = "upstox") -> None:
        self._tape_root = tape_root
        self._writeback_interval_bytes = writeback_interval_bytes
        self._venue = venue
        self._writers: dict[tuple[str, StreamKind], TapeWriter] = {}
        self.symbol_by_key: dict[str, str] = {}
        self.key_named_tapes_closed = 0

    @property
    def open_tapes(self) -> int:
        return len(self._writers)

    def learn_listing(self, listing) -> None:
        key = listing.instrument_key
        already = self.symbol_by_key.get(key)
        observe_subscribed_listing(self.symbol_by_key, listing)
        if already is not None:
            return
        for kind in StreamKind:
            writer = self._writers.pop((key, kind), None)
            if writer is not None:
                writer.close()
                self.key_named_tapes_closed += 1

    def writer_for(self, instrument_key: str, kind: StreamKind) -> TapeWriter:
        name = symbol_for(self.symbol_by_key, instrument_key)
        writer = self._writers.get((name, kind))
        if writer is None:
            writer = TapeWriter(
                self._tape_root, self._venue, name, self._writeback_interval_bytes,
                stream_kind=kind,
            )
            self._writers[(name, kind)] = writer
        return writer

    def close_all(self) -> None:
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()


def start_part(context) -> int:
    """One TapeWriter per (symbol, StreamKind), opened lazily on
    first message -- same discipline as the crypto StreamTapeRecorder's own
    _writer_for, so a symbol that never sends one of the five kinds never
    gets a hollow empty tape for it. At up to ~200 symbols x 5 kinds this can
    hold ~1000 open TapeWriters (two file handles each) -- named here as a
    real concern, not yet measured against this box's file-descriptor
    ceiling.
    """
    settings = context.settings[RUNTIME_SCOPE_NAME]
    tape_root = pathlib.Path(str(settings.entries["tape_root"].value)).expanduser()
    writeback_interval_bytes = int(context.number("writeback_interval"))

    # instrument_key -> trading_symbol, the shared name every reader of this
    # tape uses -- built the same way broker_history_reader.py's own
    # _symbol_by_key is, from the same narrowed, already-resolved listing.
    tapes = TapeWritersByInstrument(tape_root, writeback_interval_bytes)
    counts = {
        "records_written": 0, "last_failure": None,
        "symbols_resolved": 0, "unresolved_writes": 0,
    }

    listing_reader = context.bus.reader("broker-subscribed-instrument-listing")

    def learn_listings() -> None:
        # A message's payload is a tuple slice most of the time, same
        # RestatementConveyor pacing subscribed-instrument-listing-filter uses
        # everywhere else it is read (expiry-day-zero-to-hero-detector's own
        # tick does the identical isinstance check for the same reason).
        for message in listing_reader():
            payload = message.payload
            for listing in payload if isinstance(payload, tuple) else (payload,):
                tapes.learn_listing(listing)
        counts["symbols_resolved"] = len(tapes.symbol_by_key)

    def write_one(record) -> None:
        kind = stream_kind_for(type(record))
        if record.instrument_key not in tapes.symbol_by_key:
            counts["unresolved_writes"] += 1
        tapes.writer_for(record.instrument_key, kind).append(
            stream_kind=kind,
            payload=_payload_for(record),
            venue_time_ns=venue_time_ns_of(record),
        )
        counts["records_written"] += 1

    readers = tuple(
        context.bus.reader(data_type)
        for data_type in (
            "broker-market-data", "broker-candle", "broker-order-book-snapshot",
            "broker-open-interest", "broker-option-greeks",
        )
    )

    def write_all_pending() -> None:
        try:
            learn_listings()
            for read in readers:
                for message in read():
                    write_one(message.payload)
            counts["last_failure"] = None
        except OSError as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"

    def describe_standing() -> dict:
        return {
            "part_id": PART_ID, "open_tapes": tapes.open_tapes,
            "key_named_tapes_closed": tapes.key_named_tapes_closed, **counts,
        }

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=write_all_pending,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=describe_standing,
        )
    finally:
        tapes.close_all()


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "TapeWritersByInstrument",
    "observe_subscribed_listing",
    "start_part",
    "stream_kind_for",
    "symbol_for",
    "venue_time_ns_of",
]
