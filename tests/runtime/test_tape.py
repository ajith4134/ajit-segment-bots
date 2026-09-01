"""The tape has to survive the way a part actually dies.

A part is SIGKILLed as the ordinary way of switching it off, so these tests are
about torn writes and restarts rather than about happy-path round trips. The
bytes written here are a deterministic pattern, not market data -- the tape's own
correctness is a property of the file format, and phase 1's parts are what get
tested against the real captured tape (RL-063).
"""

import os
import pathlib
import signal
import subprocess
import sys
import time

import numpy
import pytest

from runtime.tape import (
    BLOB_SUFFIX,
    INDEX_SUFFIX,
    TAPE_RECORD_BYTES,
    TAPE_RECORD_DTYPE,
    NOT_SENT,
    StreamKind,
    TapeRootRefused,
    TapeWriter,
    count_whole_records,
    day_of_timestamp_ns,
    read_payload,
    read_tape_index,
    resolve_tape_root,
    tape_paths_for,
)

VENUE = "binanceusdm"
SYMBOL = "BTCUSDT"
# A day and a nanosecond inside it, fixed so the test does not depend on when it runs.
DAY = "2026-08-21"
NOON_NS = 1787313600_000_000_000

WRITEBACK_INTERVAL = 8 * 1024 * 1024


@pytest.fixture
def tape_root(durable_tmp_path):
    """A tape root on genuinely durable storage."""
    return resolve_tape_root(durable_tmp_path / "tape")


def _paths(root, stream_kind=StreamKind.TRADE):
    return tape_paths_for(root, VENUE, SYMBOL, DAY, stream_kind)


def test_a_record_round_trips_with_its_raw_payload_unchanged(tape_root):
    payload = b'{"e":"aggTrade","p":"64000.10","q":"0.5"}'
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(
            StreamKind.TRADE, payload,
            received_at_ns=NOON_NS, venue_time_ns=NOON_NS - 1000, sequence=42,
        )

    index_path, blob_path = _paths(tape_root)
    records = read_tape_index(index_path)
    assert len(records) == 1
    assert records[0]["stream_kind"] == int(StreamKind.TRADE)
    assert records[0]["received_at_ns"] == NOON_NS
    assert records[0]["venue_time_ns"] == NOON_NS - 1000
    assert records[0]["sequence"] == 42
    # The point of the tape: what the venue sent, byte for byte, not a parse of it.
    assert read_payload(blob_path, records[0]) == payload


def test_a_torn_trailing_index_record_is_ignored_rather_than_read_as_data(tape_root):
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        for n in range(3):
            writer.append(StreamKind.TRADE, b"tick", received_at_ns=NOON_NS + n)

    index_path, _ = _paths(tape_root)
    assert count_whole_records(index_path) == 3

    # A kill mid-record leaves a fraction of one behind. Truncate a real file
    # rather than mock the condition -- the whole claim is about what is on disk.
    with open(index_path, "r+b") as handle:
        handle.truncate(3 * TAPE_RECORD_BYTES - 7)

    assert count_whole_records(index_path) == 2
    records = read_tape_index(index_path)
    assert len(records) == 2, "the torn record must be invisible, not half-read"
    assert [int(r["received_at_ns"]) for r in records] == [NOON_NS, NOON_NS + 1]


def test_reopening_a_day_appends_rather_than_truncating_what_is_there(tape_root):
    """A part restart must not erase the day's capture -- restart is the normal path."""
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(StreamKind.TRADE, b"before", received_at_ns=NOON_NS)

    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(StreamKind.TRADE, b"after", received_at_ns=NOON_NS + 1)

    index_path, blob_path = _paths(tape_root)
    records = read_tape_index(index_path)
    assert len(records) == 2, "the second writer truncated the first writer's day"
    assert read_payload(blob_path, records[0]) == b"before"
    assert read_payload(blob_path, records[1]) == b"after"


def test_resuming_after_a_torn_record_does_not_write_behind_the_tear(tape_root):
    """The next record must land after whole records, never after half of one."""
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(StreamKind.TRADE, b"first", received_at_ns=NOON_NS)
        writer.append(StreamKind.TRADE, b"second", received_at_ns=NOON_NS + 1)

    index_path, blob_path = _paths(tape_root)
    with open(index_path, "r+b") as handle:
        handle.truncate(2 * TAPE_RECORD_BYTES - 11)

    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(StreamKind.TRADE, b"third", received_at_ns=NOON_NS + 2)

    records = read_tape_index(index_path)
    assert len(records) == 2
    assert index_path.stat().st_size % TAPE_RECORD_BYTES == 0
    assert read_payload(blob_path, records[0]) == b"first"
    assert read_payload(blob_path, records[1]) == b"third"


def test_a_record_after_utc_midnight_rolls_into_the_next_days_files(tape_root):
    one_second_before_midnight = 1787356799_000_000_000
    just_after_midnight = 1787356800_000_000_000
    assert day_of_timestamp_ns(one_second_before_midnight) != day_of_timestamp_ns(
        just_after_midnight
    ), "fixture timestamps must straddle a UTC midnight for this test to mean anything"

    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        writer.append(StreamKind.TRADE, b"yesterday", received_at_ns=one_second_before_midnight)
        writer.append(StreamKind.TRADE, b"today", received_at_ns=just_after_midnight)

    for stamp, expected in (
        (one_second_before_midnight, b"yesterday"),
        (just_after_midnight, b"today"),
    ):
        index_path, blob_path = tape_paths_for(
            tape_root, VENUE, SYMBOL, day_of_timestamp_ns(stamp)
        )
        records = read_tape_index(index_path)
        assert len(records) == 1, f"{index_path.name} should hold exactly its own day"
        assert read_payload(blob_path, records[0]) == expected


def test_the_blob_offset_of_every_record_stays_inside_the_blob(tape_root):
    """Blob-before-index means an index record can never point past the blob's end."""
    payloads = [b"a", b"bb", b"ccc", b"dddd"]
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        for n, payload in enumerate(payloads):
            writer.append(StreamKind.TRADE, payload, received_at_ns=NOON_NS + n)

    index_path, blob_path = _paths(tape_root)
    blob_size = blob_path.stat().st_size
    for record in read_tape_index(index_path):
        end = int(record["blob_offset"]) + int(record["blob_length"])
        assert end <= blob_size, "an index record points past the end of the blob"


def test_a_tape_root_whose_pages_are_memory_is_refused(tmp_path):
    """pytest's own tmp_path is under /tmp, which is tmpfs here -- a tape there is RAM."""
    with pytest.raises(TapeRootRefused) as refusal:
        resolve_tape_root(tmp_path / "tape")
    assert "tape" in str(refusal.value).lower()


def test_a_record_the_venue_gave_no_sequence_or_timestamp_for_says_so(tape_root):
    """Absent is its own value, distinguishable from present-and-small (Rule 8)."""
    with TapeWriter(
        tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL, stream_kind=StreamKind.BOOK
    ) as writer:
        writer.append(StreamKind.BOOK, b"{}", received_at_ns=NOON_NS)

    index_path, _ = _paths(tape_root, StreamKind.BOOK)
    record = read_tape_index(index_path)[0]
    assert record["sequence"] == NOT_SENT
    assert record["venue_time_ns"] == NOT_SENT


def test_the_record_layout_is_packed_and_fixed_width():
    """A tape outlives the process that wrote it; the layout may not drift."""
    assert TAPE_RECORD_DTYPE.itemsize == TAPE_RECORD_BYTES
    # Packed, not padded: the sum of the field widths is the record width.
    assert sum(TAPE_RECORD_DTYPE.fields[name][0].itemsize for name in TAPE_RECORD_DTYPE.names) == (
        TAPE_RECORD_BYTES
    )
    # Explicit little-endian on every multi-byte field, so a reader on another
    # machine gets the same numbers rather than byte-swapped ones. `.str` is what
    # reports this honestly: `.byteorder` says "=" whenever the requested order
    # happens to match the native one, which hides the difference on this box.
    for name in TAPE_RECORD_DTYPE.names:
        field = TAPE_RECORD_DTYPE.fields[name][0]
        assert field.str[0] in ("<", "|"), f"{name} is {field.str}, not little-endian"


def test_an_empty_index_reads_as_no_records_rather_than_an_error(tape_root):
    index_path, _ = _paths(tape_root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(b"")
    assert count_whole_records(index_path) == 0
    assert len(read_tape_index(index_path)) == 0


WRITE_THEN_WAIT = """
import sys, time
sys.path.insert(0, {repository!r})
from runtime.tape import TapeWriter, StreamKind

root, interval = {root!r}, {interval}
writer = TapeWriter(root, {venue!r}, {symbol!r}, interval)
for n in range({count}):
    writer.append(StreamKind.TRADE, b"killme", received_at_ns={base} + n)
writer.force_writeback()
print("WRITTEN", flush=True)
time.sleep(60)
"""


@pytest.mark.slow
def test_records_flushed_before_a_real_sigkill_are_still_there_afterwards(tape_root):
    """The claim is about SIGKILL, so the test uses one -- not close(), not an exception."""
    import pathlib

    repository = str(pathlib.Path(__file__).resolve().parents[2])
    script = WRITE_THEN_WAIT.format(
        repository=repository, root=str(tape_root), interval=WRITEBACK_INTERVAL,
        venue=VENUE, symbol=SYMBOL, count=5, base=NOON_NS,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        # Block until the child says its writeback returned -- so the kill lands
        # after the data was made durable, not before it was ever written.
        assert child.stdout.readline().strip() == "WRITTEN"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.stdout is not None:
            child.stdout.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    assert child.returncode == -signal.SIGKILL, "the child must have died by SIGKILL"

    index_path, blob_path = _paths(tape_root)
    records = read_tape_index(index_path)
    assert len(records) == 5, "flushed records did not survive the kill"
    for record in records:
        assert read_payload(blob_path, record) == b"killme"


WRITE_WITHOUT_FLUSHING = """
import sys, time
sys.path.insert(0, {repository!r})
from runtime.tape import TapeWriter, StreamKind

root, interval = {root!r}, {interval}
writer = TapeWriter(root, {venue!r}, {symbol!r}, interval)
for n in range({count}):
    writer.append(StreamKind.TRADE, {payload!r}, received_at_ns={base} + n)
print("WRITTEN", flush=True)
time.sleep(60)
"""


@pytest.mark.slow
def test_a_kill_with_no_flush_costs_at_most_the_message_in_flight(tape_root):
    """The ordinary path, which is the one the spec's claim is about.

    Spec section 2.2: "A kill between the two loses one message." A part is
    SIGKILLed as the *ordinary* way of being switched off, and nothing calls
    force_writeback on that path -- the writeback interval is 8 MiB, which a
    quiet symbol does not reach for hours. So this test appends and kills with
    no flush at all, which is exactly what a governor switching a part off does.

    It failed when first written: the writer opened its file buffered, so up to
    a userspace buffer of records lived only in the dying process. That is not
    one message, and on a low-volume symbol it is minutes of trades that cannot
    be recaptured.
    """
    import pathlib

    repository = str(pathlib.Path(__file__).resolve().parents[2])
    written_count = 200
    payload = b'{"e":"aggTrade","p":"64000.10","q":"0.5"}'
    script = WRITE_WITHOUT_FLUSHING.format(
        repository=repository, root=str(tape_root), interval=WRITEBACK_INTERVAL,
        venue=VENUE, symbol=SYMBOL, count=written_count, base=NOON_NS, payload=payload,
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "WRITTEN"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        child.stdout.close()

    index_path, blob_path = _paths(tape_root)
    survived = count_whole_records(index_path)
    assert survived >= written_count - 1, (
        f"{written_count - survived} of {written_count} records did not survive a kill; "
        f"the spec's claim is that a kill costs the one message in flight"
    )

    # And every surviving index record still points at bytes that are really
    # there -- blob before index, at the syscall level and not just in the
    # order the calls were made.
    index = read_tape_index(index_path)
    blob_size = blob_path.stat().st_size
    for record in index:
        assert int(record["blob_offset"]) + int(record["blob_length"]) <= blob_size
        assert read_payload(blob_path, record) == payload


def test_two_kinds_of_the_same_symbol_are_two_files(tape_root):
    """One file, one kind, one writer.

    ccxt-venue-reader appended candles to the blob venue-trade-stream-reader was
    appending trades to on 2026-08-25. Each held its own byte counter, so from
    that minute every index offset pointed into the other writer's payloads:
    218,027 of 218,116 records for one symbol were unreadable, and the offsets in
    the index ran backwards. The parts stayed healthy and every board stayed green.
    """
    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as trades:
        trades.append(StreamKind.TRADE, b'{"trade":1}', received_at_ns=NOON_NS)
        with TapeWriter(
            tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL, stream_kind=StreamKind.CANDLE
        ) as candles:
            candles.append(StreamKind.CANDLE, b'{"candle":1}', received_at_ns=NOON_NS)
            trades.append(StreamKind.TRADE, b'{"trade":2}', received_at_ns=NOON_NS + 1)

    trade_index, trade_blob = _paths(tape_root)
    candle_index, candle_blob = _paths(tape_root, StreamKind.CANDLE)
    assert trade_index != candle_index

    trade_records = read_tape_index(trade_index)
    assert [read_payload(trade_blob, record) for record in trade_records] == [
        b'{"trade":1}', b'{"trade":2}'
    ]
    candle_records = read_tape_index(candle_index)
    assert [read_payload(candle_blob, record) for record in candle_records] == [b'{"candle":1}']


def test_a_second_writer_on_one_tape_is_refused_rather_than_interleaved(tape_root):
    """The kind in the path stopped that pair; this stops the next one."""
    from runtime.tape import TapeAlreadyBeingWritten

    script = (
        "import sys;"
        "sys.path.insert(0, %r);"
        "from runtime.tape import StreamKind, TapeWriter;"
        "writer = TapeWriter(%r, %r, %r, %d);"
        "writer.append(StreamKind.TRADE, b'held', received_at_ns=%d);"
        "print('holding', flush=True);"
        "sys.stdin.readline()"
    ) % (
        str(pathlib.Path(__file__).resolve().parents[2]),
        str(tape_root), VENUE, SYMBOL, WRITEBACK_INTERVAL, NOON_NS,
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "holding"
        with pytest.raises(TapeAlreadyBeingWritten):
            TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL).append(
                StreamKind.TRADE, b"second", received_at_ns=NOON_NS + 1
            )
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)


def test_a_record_of_the_wrong_kind_is_refused_by_its_writer(tape_root):
    from runtime.tape import TapeKindRefused

    with TapeWriter(tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL) as writer:
        with pytest.raises(TapeKindRefused):
            writer.append(StreamKind.CANDLE, b'{"candle":1}', received_at_ns=NOON_NS)


def test_stream_kind_gains_open_interest_and_option_greeks_without_renumbering_existing():
    # Existing five must be unchanged -- a tape written before this change
    # reads back identically, since StreamKind is stored as one byte (T-5).
    assert StreamKind.TRADE == 1
    assert StreamKind.CANDLE == 2
    assert StreamKind.BOOK == 3
    assert StreamKind.QUOTE == 4
    assert StreamKind.PREMIUM == 5
    assert StreamKind.OPEN_INTEREST == 6
    assert StreamKind.OPTION_GREEKS == 7


def test_trade_fidelity_gains_last_traded_price_only():
    from runtime.tape import TradeFidelity

    assert TradeFidelity.EVERY_PRINT == "every-print"
    assert TradeFidelity.VENUE_AGGREGATED == "venue-aggregated"
    assert TradeFidelity.LAST_TRADED_PRICE_ONLY == "last-traded-price-only"
