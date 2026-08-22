"""The capture tiles, including the red one.

A board with no way to render red has never been tested against a real failure,
and the failure these tiles exist for is the quiet one: a capture that stopped
while everything around it kept looking fine. So these tests build real tapes on
real disk, write real records into them, and then make them stale.
"""

import json
import time

import pytest

from runtime.probes import capture_probes
from runtime.probes.capture_probes import (
    FAILING,
    NOT_BUILT,
    NOT_MEASURED,
    OK,
    STALE_HEALTH_SECONDS,
    STALE_TAPE_SECONDS,
    probe_capture_readers,
    probe_tape_freshness,
    probe_tape_records,
    probe_tape_size,
    run_all_capture_probes,
)
from runtime.tape import StreamKind, TapeWriter, resolve_tape_root

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
WRITEBACK_INTERVAL = 8 * 1024 * 1024
A_PAYLOAD = b'{"e":"aggTrade","p":"64000.10","q":"0.5"}'
NANOSECONDS_PER_SECOND = 1_000_000_000


@pytest.fixture
def tape_root(durable_tmp_path, monkeypatch):
    """Point the probes at a tape this test owns, through the same reader they use."""
    root = durable_tmp_path / "tape"
    monkeypatch.setattr(
        capture_probes, "_read_tape_root", lambda: (root, "a tape this test wrote")
    )
    return root


def write_records(root, count, at_ns=None, venue=VENUE, symbol=SYMBOL):
    resolve_tape_root(root)
    at_ns = at_ns if at_ns is not None else time.time_ns()
    with TapeWriter(root, venue, symbol, WRITEBACK_INTERVAL) as writer:
        for offset in range(count):
            writer.append(StreamKind.TRADE, A_PAYLOAD, received_at_ns=at_ns + offset)
    return at_ns


def write_health(root, venue, observed_at_ns):
    directory = root.parent / "capture-health"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{venue}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"observed_at_ns": observed_at_ns, "event": "capture-standing"}) + "\n")
    return path


def test_no_tape_at_all_is_not_built_rather_than_broken(tape_root):
    """A fresh clone has never captured, which is true and unalarming."""
    for probe in (probe_tape_records, probe_tape_freshness, probe_tape_size, probe_capture_readers):
        result = probe()
        assert result.state == NOT_BUILT, f"{result.label} said {result.state}"
        assert result.proof


def test_records_are_counted_from_the_index_files_own_size(tape_root):
    write_records(tape_root, 250)
    result = probe_tape_records()
    assert result.state == OK
    assert "250" in result.value
    assert VENUE in result.value


def test_a_tape_root_that_exists_but_holds_nothing_is_not_built(tape_root):
    resolve_tape_root(tape_root)
    assert probe_tape_records().state == NOT_BUILT
    assert probe_tape_freshness().state == NOT_BUILT


def test_a_fresh_tape_reads_as_fresh(tape_root):
    write_records(tape_root, 5)
    result = probe_tape_freshness()
    assert result.state == OK
    assert VENUE in result.value


def test_a_tape_that_stopped_being_written_goes_red(tape_root):
    """The failure this tile exists for, and the one nothing else would show.

    The tape itself cannot tell a stopped reader from a silent market -- so the
    board says stale, loudly, rather than saying nothing at all.
    """
    stale_ns = time.time_ns() - int((STALE_TAPE_SECONDS + 30) * NANOSECONDS_PER_SECOND)
    write_records(tape_root, 5, at_ns=stale_ns)
    result = probe_tape_freshness()
    assert result.state == FAILING
    assert VENUE in result.value
    assert "stopped writing" in result.value
    assert str(int(STALE_TAPE_SECONDS)) in result.proof


def test_one_stale_venue_reddens_the_tile_even_when_the_other_is_live(tape_root):
    """A half-dead capture must not read as a healthy one."""
    write_records(tape_root, 5)
    write_records(
        tape_root,
        5,
        at_ns=time.time_ns() - int((STALE_TAPE_SECONDS + 30) * NANOSECONDS_PER_SECOND),
        venue="bybit-linear",
    )
    result = probe_tape_freshness()
    assert result.state == FAILING
    assert "bybit-linear" in result.value


def test_a_reader_still_reporting_reads_as_reporting(tape_root):
    write_records(tape_root, 1)
    write_health(tape_root, VENUE, time.time_ns())
    result = probe_capture_readers()
    assert result.state == OK
    assert VENUE in result.value


def test_a_reader_that_stopped_reporting_goes_red(tape_root):
    """Read from what the reader wrote, not from whether a process exists.

    A process that is alive and has stopped reading is exactly the failure worth
    catching, and only the reader's own reports can tell the difference.
    """
    write_records(tape_root, 1)
    write_health(
        tape_root, VENUE, time.time_ns() - int((STALE_HEALTH_SECONDS + 30) * NANOSECONDS_PER_SECOND)
    )
    result = probe_capture_readers()
    assert result.state == FAILING
    assert "stopped reporting" in result.value


def test_a_health_log_of_junk_is_unmeasured_not_healthy(tape_root):
    write_records(tape_root, 1)
    directory = tape_root.parent / "capture-health"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{VENUE}.jsonl").write_text("not json at all\n")
    result = probe_capture_readers()
    assert result.state == NOT_MEASURED
    assert VENUE in result.value


def test_the_health_tail_is_read_from_the_end_not_the_whole_file(tape_root):
    """A board build must not get slower every hour the capture runs."""
    write_records(tape_root, 1)
    now = time.time_ns()
    for index in range(5000):
        write_health(tape_root, VENUE, now - (5000 - index))
    started = time.monotonic()
    result = probe_capture_readers()
    assert result.state == OK
    assert time.monotonic() - started < 1.0


def test_unreadable_settings_are_unmeasured_rather_than_absent(monkeypatch):
    """Rule 8: a fact that could not be established is its own state, never a gap."""
    monkeypatch.setattr(capture_probes, "_read_tape_root", lambda: (None, "settings unreadable"))
    for result in run_all_capture_probes():
        assert result.state == NOT_MEASURED
        assert result.proof


def test_the_size_tile_reports_what_was_written_and_what_is_left(tape_root):
    write_records(tape_root, 100)
    result = probe_tape_size()
    assert result.state == OK
    assert "GB written" in result.value and "GB free" in result.value


def test_a_probe_that_crashes_reports_unmeasured_rather_than_vanishing(tape_root, monkeypatch):
    def explode():
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(capture_probes, "probe_tape_records", explode)
    monkeypatch.setattr(capture_probes, "CAPTURE_PROBES", (explode,))
    results = run_all_capture_probes()
    assert len(results) == 1
    assert results[0].state == NOT_MEASURED
    assert "RuntimeError" in results[0].value
