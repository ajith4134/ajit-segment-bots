"""The tape: what a venue actually sent, kept so it can be re-read years later.

Two files per venue, symbol, UTC day **and stream kind** (spec section 2.2):

    {root}/{venue}/{symbol}/{YYYY-MM-DD}.index          trades, memmap-able
    {root}/{venue}/{symbol}/{YYYY-MM-DD}.blob           the raw venue payloads
    {root}/{venue}/{symbol}/{YYYY-MM-DD}.candle.index   one file per other kind
    {root}/{venue}/{symbol}/{YYYY-MM-DD}.candle.blob

**The stream kind is in the path since 2026-08-25, and it is there because it was
not.** A record carries its own kind, so one file per symbol was the design --
and a file has one writer only if one *process* writes it. `ccxt-venue-reader`
started with phase 6 that afternoon and appended candles to the same blob
`venue-trade-stream-reader` was appending trades to. Each holds its own byte
counter, so from that minute every index offset pointed into the other writer's
bytes: 218,027 of 218,116 records for one symbol were unreadable, and offsets in
the index ran *backwards*. Nothing detected it -- the parts were healthy, the
board was green, and the corruption was only visible to something that tried to
parse a payload.

Trades keep the bare name so three days of captured history stay exactly where
every reader already looks; every other kind is suffixed. The asymmetry is the
price of not rewriting an irreplaceable corpus, and it is stated here rather than
discovered.

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
import fcntl
import pathlib
import time

import numpy

from runtime.page_cache_discipline import CacheReleasingWriter
from runtime.storage_facts import require_durable_directory

INDEX_SUFFIX = ".index"
BLOB_SUFFIX = ".blob"
# Beside each index, held for as long as a process is appending to that tape.
WRITER_LOCK_SUFFIX = ".writing"


class TapeAlreadyBeingWritten(RuntimeError):
    """A second process tried to append to a tape another one already holds."""


class TapeKindRefused(ValueError):
    """A record of one stream kind was handed to a tape writer for another."""

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
    # Mark price, index price and the venue's declared funding rate for one
    # symbol. Added 2026-08-26: funding-rate-forecaster computes the next
    # settlement from the premium -- mark minus index -- and neither number was
    # on any wire, so it forecast nothing at all and said so in its standing.
    PREMIUM = 5
    # Open interest and today's traded volume/buy-sell quantity for one
    # derivative contract. Added 2026-09-01 for the Indian-markets broker
    # adapter -- no crypto perpetual venue ever published this (spot/perp
    # pricing has no concept of open interest the way a listed derivative
    # contract does). Appended, never inserted, so a tape written before this
    # change reads back with every existing value unchanged.
    OPEN_INTEREST = 6
    # Delta, theta, gamma, vega, rho and implied volatility for one options
    # contract. Added 2026-09-01, same reasoning as OPEN_INTEREST above.
    OPTION_GREEKS = 7
    # One news item as its source delivered it, or the published news fact
    # once the block has understood it. Added 2026-09-12 with
    # `news-tape-writer`: the news block's own design says history accrues
    # only in real time, same as the market tape, and a story is not
    # re-fetchable -- Upstox's news endpoint carries a rolling backlog
    # (measured 2026-09-12: 153.3 hours of it) and nothing older.
    # Appended, never inserted, so every tape written before this reads back
    # unchanged.
    NEWS = 8


class TradeFidelity(enum.StrEnum):
    """Whether a venue's trades are individual prints or its own aggregates.

    Binance USDⓈ-M offers only @aggTrade, aggregated per 100ms, and has no raw
    trade stream at all; Bybit's publicTrade is every print. Both are "trades",
    and a tape that stored them identically would silently claim a resolution one
    of them does not have (spec section 7). This travels with the data.
    """

    EVERY_PRINT = "every-print"
    VENUE_AGGREGATED = "venue-aggregated"
    # A retail broker's last-traded-price ticker: the exchange's own last
    # print, restated on update, not a stream of every individual print the
    # way a crypto venue's trade feed is. Added 2026-09-01 -- Upstox's feed
    # has no per-print trade stream at all (spec section 5); recording an LTP
    # update as EVERY_PRINT or VENUE_AGGREGATED would claim a resolution this
    # feed does not have, the same reasoning the other two values exist for.
    LAST_TRADED_PRICE_ONLY = "last-traded-price-only"
    # The close of one historical bar, replayed for the hours the market is
    # shut (2026-09-02, Phase A). One print per bar, not a tick stream: a
    # consumer counting prints per second over a replay is counting minutes,
    # and this is what lets it know that -- the same reason the three values
    # above exist.
    #
    # It carries a second meaning that only the fill path uses, and the meaning
    # is sound because of where the data comes from: **a historical bar exists
    # only because the market traded that minute**, so the bar is evidence of
    # its own session. That is what lets paper-fill-simulator fill against it
    # while the wall clock says the market is shut, without weakening the guard
    # that stops a *live* price filling at a moment nobody was trading.
    HISTORICAL_BAR_CLOSE = "historical-bar-close"


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


def tape_stem_for(day: str, stream_kind: StreamKind) -> str:
    """The file stem for one day of one kind.

    TRADE keeps the bare day, because that is where every reader and three days of
    captured history already are. Every other kind is suffixed with its own name,
    so two parts recording two kinds of the same symbol write two files.
    """
    if stream_kind is StreamKind.TRADE:
        return day
    return f"{day}.{stream_kind.name.lower()}"


def tape_paths_for(
    root: pathlib.Path,
    venue: str,
    symbol: str,
    day: str,
    stream_kind: StreamKind = StreamKind.TRADE,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Where one venue's one symbol's one day of one stream kind lives."""
    directory = pathlib.Path(root) / venue / symbol
    stem = tape_stem_for(day, stream_kind)
    return directory / f"{stem}{INDEX_SUFFIX}", directory / f"{stem}{BLOB_SUFFIX}"


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
        stream_kind: StreamKind = StreamKind.TRADE,
    ) -> None:
        self._root = pathlib.Path(root)
        self._venue = venue
        self._symbol = symbol
        self._stream_kind = stream_kind
        self._writeback_interval_bytes = writeback_interval_bytes
        self._lock_handle = None
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

        if stream_kind is not self._stream_kind:
            raise TapeKindRefused(
                f"a {stream_kind.name} record was handed to a {self._stream_kind.name} tape "
                f"writer for {self._venue}/{self._symbol}. One file, one kind, one writer: "
                f"two writers on one blob each count their own bytes, and every offset after "
                f"the first interleave points into the other writer's payloads."
            )
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

    def _take_the_writer_lock(self, index_path: pathlib.Path) -> None:
        """One writer per file, enforced by the kernel rather than by convention.

        The kind in the path stops the two writers that collided on 2026-08-25.
        This stops the next pair: any second process appending to the same tape is
        refused at open, loudly, instead of silently interleaving its bytes into
        someone else's offsets. An advisory flock is released when the process
        dies however it dies, which is what a part being SIGKILLed needs.
        """
        lock_path = index_path.with_suffix(WRITER_LOCK_SUFFIX)
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as refusal:
            handle.close()
            raise TapeAlreadyBeingWritten(
                f"another process is already writing {index_path}. Two writers on one tape "
                f"file each count their own blob position, so every index offset after the "
                f"first interleaved append points at the wrong bytes -- and nothing detects "
                f"it until something tries to parse a payload."
            ) from refusal
        self._release_the_writer_lock()
        self._lock_handle = handle

    def _release_the_writer_lock(self) -> None:
        if self._lock_handle is not None:
            self._lock_handle.close()  # closing releases the flock
            self._lock_handle = None

    def close(self) -> None:
        if self._blob_writer is not None:
            self._blob_writer.close()
            self._blob_writer = None
        if self._index_writer is not None:
            self._index_writer.close()
            self._index_writer = None
        self._release_the_writer_lock()
        self._open_day = None

    def _roll_to_day(self, day: str) -> None:
        self.close()
        index_path, blob_path = tape_paths_for(
            self._root, self._venue, self._symbol, day, self._stream_kind
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        self._take_the_writer_lock(index_path)
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
