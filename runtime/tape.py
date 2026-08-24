"""The tape: what a venue actually sent, kept so it can be re-read years later.

Two files per venue, symbol and UTC day (spec section 2.2):

    {root}/{venue}/{symbol}/{YYYY-MM-DD}.index   fixed-width records, memmap-able
    {root}/{venue}/{symbol}/{YYYY-MM-DD}.blob    the raw venue payloads, end to end

The index is a fixed numpy dtype so a reader can memmap it and binary-search the
time column without parsing anything. The blob holds the bytes exactly as they
arrived. Normalisation happens on read, never on write: RL-063 requires tests to
run on real captured data, and data is only re-interpretable later if what the
venue said was kept rather than what we made of it at the time. A normalisation
bug frozen into the tape is unrecoverable; the same bug in a reader is a fix.

Why not Parquet, which is the obvious answer: its footer is written once, at
close, so a writer killed before that leaves a file that is not truncated but
*unreadable* -- and SIGKILL is how a part is switched off (section 4). HDF5 fails
the same way outside SWMR. Arrow IPC Streaming genuinely survives, but costs a
large dependency for a format this file already gets from numpy, which is pinned.

The write order is blob first, then index. A kill between the two loses one
message and leaves an index that is a whole number of records, which the reader
detects by size. The reverse order would leave an index record pointing past the
end of the blob -- a reader crash rather than a lost message.
"""

from __future__ import annotations

import datetime as _datetime
import enum
import pathlib
import time

import numpy

from runtime.page_cache_discipline import CacheReleasingWriter
from runtime.storage_facts import require_durable_directory

INDEX_SUFFIX = ".index"
BLOB_SUFFIX = ".blob"

# The day a record belongs to is decided by when we received it, in UTC. Venue
# clocks disagree with each other and with ours, so partitioning on their
# timestamp would let one venue's skew put a record in yesterday's file.
DAY_FORMAT = "%Y-%m-%d"


class StreamKind(enum.IntEnum):
    """What kind of message a tape record holds.

    T-5: states are explicit and countable, and this is the vocabulary. An
    IntEnum because it is stored as one byte in the index and read back as a
    number -- the name has to survive the round trip through the file.
    """

    TRADE = 1
    CANDLE = 2
    BOOK = 3
    # A resting best bid and ask, which a symbol has whether or not it trades.
    # Added 2026-08-24: a print is the only price this system had, so a symbol
    # nobody traded had no fresh price and could not be sized against. Stored as
    # one byte like the others, so files written before it read back unchanged.
    QUOTE = 4


class TradeFidelity(enum.StrEnum):
    """Whether a venue's trades are individual prints or its own aggregates.

    Binance USDⓈ-M offers only @aggTrade, aggregated per 100ms, and has no raw
    trade stream at all; Bybit's publicTrade is every print. Both are "trades",
    and a tape that stored them identically would silently claim a resolution one
    of them does not have (spec section 7). This travels with the data.
    """

    EVERY_PRINT = "every-print"
    VENUE_AGGREGATED = "venue-aggregated"


# One index record. Packed rather than aligned so the layout is exactly these
# widths in this order, on any machine, forever -- a tape outlives the process
# that wrote it, and a reader years from now gets no say in numpy's padding
# rules. Field widths are a wire format, not decision code, so RL-061 does not
# reach them.
TAPE_RECORD_DTYPE = numpy.dtype(
    [
        ("received_at_ns", "<u8"),
        ("venue_time_ns", "<u8"),
        ("blob_offset", "<u8"),
        ("blob_length", "<u4"),
        ("sequence", "<u8"),
        ("stream_kind", "u1"),
    ],
    align=False,
)

TAPE_RECORD_BYTES = TAPE_RECORD_DTYPE.itemsize

# What a record carries when the venue did not send the field at all. Zero is
# safe for both: no venue numbers a message zero, and no venue timestamps the
# epoch, so a reader can tell "absent" from "present and small".
NOT_SENT = 0


class TapeRootRefused(RuntimeError):
    """The tape root is not somewhere a tape can honestly live."""


def resolve_tape_root(root: pathlib.Path) -> pathlib.Path:
    """The tape's root directory, refused unless its pages are really on disk.

    /tmp is tmpfs on this box, so a tape written there is a tape held in RAM that
    disappears with the machine -- and it would measure as working right up until
    it mattered. storage_facts is the guard that already knows this.
    """
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        require_durable_directory(root)
    except Exception as refusal:
        raise TapeRootRefused(
            f"{root} cannot hold a tape: {refusal}. History accrues only in real time, "
            f"so a tape on volatile storage is not a slower tape, it is no tape."
        ) from refusal
    return root


def day_of_timestamp_ns(received_at_ns: int) -> str:
    """Which UTC day a record belongs to, from when we received it."""
    seconds = received_at_ns / 1_000_000_000
    return _datetime.datetime.fromtimestamp(seconds, _datetime.UTC).strftime(DAY_FORMAT)


def tape_paths_for(
    root: pathlib.Path, venue: str, symbol: str, day: str
) -> tuple[pathlib.Path, pathlib.Path]:
    """Where one venue's one symbol's one day lives."""
    directory = pathlib.Path(root) / venue / symbol
    return directory / f"{day}{INDEX_SUFFIX}", directory / f"{day}{BLOB_SUFFIX}"


class TapeWriter:
    """Appends what one venue said about one symbol, rolling at UTC midnight.

    Opens in append mode deliberately: a part is killed and restarted as the
    ordinary way of switching it off, and a writer that truncated would erase the
    day's capture on every restart.
    """

    def __init__(
        self,
        root: pathlib.Path,
        venue: str,
        symbol: str,
        writeback_interval_bytes: int,
    ) -> None:
        self._root = pathlib.Path(root)
        self._venue = venue
        self._symbol = symbol
        self._writeback_interval_bytes = writeback_interval_bytes
        self._open_day: str | None = None
        self._index_writer: CacheReleasingWriter | None = None
        self._blob_writer: CacheReleasingWriter | None = None
        self._blob_position = 0

    @property
    def open_day(self) -> str | None:
        return self._open_day

    def append(
        self,
        stream_kind: StreamKind,
        payload: bytes,
        received_at_ns: int | None = None,
        venue_time_ns: int = NOT_SENT,
        sequence: int = NOT_SENT,
    ) -> int:
        """Record one message. Returns the offset its payload landed at.

        Blob first, then index -- so a kill between them costs this one message
        rather than leaving an index record pointing at bytes that are not there.
        """
        if received_at_ns is None:
            received_at_ns = time.time_ns()
        day = day_of_timestamp_ns(received_at_ns)
        if day != self._open_day:
            self._roll_to_day(day)

        offset = self._blob_position
        self._blob_writer.append(payload)
        self._blob_position += len(payload)

        record = numpy.zeros(1, dtype=TAPE_RECORD_DTYPE)
        record[0]["received_at_ns"] = received_at_ns
        record[0]["venue_time_ns"] = venue_time_ns
        record[0]["blob_offset"] = offset
        record[0]["blob_length"] = len(payload)
        record[0]["sequence"] = sequence
        record[0]["stream_kind"] = int(stream_kind)
        self._index_writer.append(record.tobytes())
        return offset

    def force_writeback(self) -> None:
        """Make both files durable now, blob before index.

        Same order as a single append, for the same reason: an index record is
        only meaningful once the bytes it points at are on disk.
        """
        if self._blob_writer is not None:
            self._blob_writer.force_writeback()
        if self._index_writer is not None:
            self._index_writer.force_writeback()

    def close(self) -> None:
        if self._blob_writer is not None:
            self._blob_writer.close()
            self._blob_writer = None
        if self._index_writer is not None:
            self._index_writer.close()
            self._index_writer = None
        self._open_day = None

    def _roll_to_day(self, day: str) -> None:
        self.close()
        index_path, blob_path = tape_paths_for(self._root, self._venue, self._symbol, day)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        # Resuming a day already on disk: the blob continues where it left off, and
        # any torn tail in the index is dropped first so the next record cannot be
        # written after a half-record.
        self._blob_position = blob_path.stat().st_size if blob_path.exists() else 0
        _discard_torn_index_tail(index_path)
        self._blob_writer = CacheReleasingWriter(
            blob_path, self._writeback_interval_bytes, append=True
        )
        self._index_writer = CacheReleasingWriter(
            index_path, self._writeback_interval_bytes, append=True
        )
        self._open_day = day

    def __enter__(self) -> "TapeWriter":
        return self

    def __exit__(self, *exception) -> None:
        self.close()


def _discard_torn_index_tail(index_path: pathlib.Path) -> int:
    """Truncate a half-written trailing record, returning how many bytes went.

    A kill during the index write leaves a partial record. It is detectable
    because every record is the same width, so a file that is not a whole number
    of records has exactly one torn record at its end.
    """
    if not index_path.exists():
        return 0
    size = index_path.stat().st_size
    torn = size % TAPE_RECORD_BYTES
    if torn:
        with open(index_path, "r+b") as handle:
            handle.truncate(size - torn)
    return torn


def count_whole_records(index_path: pathlib.Path) -> int:
    """How many complete records an index holds, ignoring any torn tail."""
    if not index_path.exists():
        return 0
    return index_path.stat().st_size // TAPE_RECORD_BYTES


def read_tape_index(index_path: pathlib.Path) -> numpy.ndarray:
    """The index as a read-only memmap of whole records.

    Reads through the torn tail rather than around it: the map covers only
    complete records, so a truncated final write is invisible to every caller
    instead of being something each one has to remember to handle.
    """
    whole = count_whole_records(index_path)
    if whole == 0:
        return numpy.zeros(0, dtype=TAPE_RECORD_DTYPE)
    return numpy.memmap(index_path, dtype=TAPE_RECORD_DTYPE, mode="r", shape=(whole,))


def read_payload(blob_path: pathlib.Path, record) -> bytes:
    """The raw venue payload one index record points at."""
    with open(blob_path, "rb") as handle:
        handle.seek(int(record["blob_offset"]))
        return handle.read(int(record["blob_length"]))
