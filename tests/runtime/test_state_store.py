"""Structured part state, in the store section 15.2 chose.

The decisive property is crash-only: a part's off switch is SIGKILL, so a committed
row must survive one. SQLite guarantees that regardless of the synchronous setting;
synchronous only governs power loss. These tests measure it rather than quoting it.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from runtime.state_store import (
    JournalEntry,
    StoreDurability,
    append_journal_entry,
    open_store,
    read_current_setting_and_change_time,
    read_entries_in_window,
    record_setting_change,
)

BUSY_TIMEOUT = 5.0

WRITER = """
import sys, time
sys.path.insert(0, {repository!r})
from runtime.state_store import StoreDurability, append_journal_entry, open_store
connection = open_store({path!r}, StoreDurability.LEDGER, {timeout})
for index in range({count}):
    append_journal_entry(connection, "writer-part", "switch-record",
                         f"payload-{{index}}", "test, deterministic")
print("COMMITTED", flush=True)
time.sleep(30)
"""


def _writer_script(path, count, repository, timeout=BUSY_TIMEOUT):
    return WRITER.format(repository=repository, path=str(path), count=count, timeout=timeout)


def _repository() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def opened_stores():
    """Every connection a test opens through this call, closed on teardown either
    way -- the same discipline control_socket_pair uses for sockets in
    test_control_channel.py, adapted to a factory because store parameters (path,
    durability) vary per call rather than being fixed for the whole test.
    """
    connections = []

    def _open_store(path, durability, busy_timeout_seconds):
        connection = open_store(path, durability, busy_timeout_seconds)
        connections.append(connection)
        return connection

    yield _open_store
    for connection in connections:
        connection.close()


def test_an_appended_entry_comes_back_with_everything_it_was_given(durable_tmp_path, opened_stores):
    connection = opened_stores(durable_tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    before = time.time_ns()
    entry_id = append_journal_entry(
        connection, "market-data-feed", "switch-record", "on", "gate-actuator"
    )
    entries = read_entries_in_window(connection, "market-data-feed", before, time.time_ns())

    assert entry_id > 0
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, JournalEntry)
    assert (entry.part_id, entry.kind, entry.payload, entry.provenance) == (
        "market-data-feed", "switch-record", "on", "gate-actuator",
    )


def test_the_window_query_excludes_what_falls_outside_it(durable_tmp_path, opened_stores):
    connection = opened_stores(durable_tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    append_journal_entry(connection, "part", "kind", "early", "test")
    time.sleep(0.01)
    boundary = time.time_ns()
    time.sleep(0.01)
    append_journal_entry(connection, "part", "kind", "late", "test")

    later = read_entries_in_window(connection, "part", boundary, time.time_ns())
    assert [entry.payload for entry in later] == ["late"]


def test_it_is_in_write_ahead_logging_mode(durable_tmp_path, opened_stores):
    connection = opened_stores(durable_tmp_path / "journal.db", StoreDurability.LEDGER, BUSY_TIMEOUT)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_a_ledger_syncs_fully_and_a_record_store_does_not(durable_tmp_path, opened_stores):
    ledger = opened_stores(durable_tmp_path / "ledger.db", StoreDurability.LEDGER, BUSY_TIMEOUT)
    record = opened_stores(durable_tmp_path / "record.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    assert ledger.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL
    assert record.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_a_second_writer_waits_rather_than_failing(durable_tmp_path, opened_stores):
    # Measured in section 15.2: with busy_timeout unset a second writer fails
    # instantly with 'database is locked'; set, it waits and succeeds. Parts must
    # never carry their own retry loop for this.
    path = durable_tmp_path / "journal.db"
    first = opened_stores(path, StoreDurability.RECORD, BUSY_TIMEOUT)
    second = opened_stores(path, StoreDurability.RECORD, BUSY_TIMEOUT)
    first.execute("BEGIN IMMEDIATE")
    append_journal_entry(first, "part", "kind", "held", "test")
    first.execute("COMMIT")
    append_journal_entry(second, "part", "kind", "after", "test")
    assert len(read_entries_in_window(second, "part", 0, time.time_ns())) == 2


@pytest.mark.slow
def test_committed_rows_survive_the_writer_being_sigkilled(durable_tmp_path, opened_stores):
    # This is the crash-only requirement, and the only test here that really matters.
    path = durable_tmp_path / "journal.db"
    count = 200
    writer = subprocess.Popen(
        [sys.executable, "-c", _writer_script(path, count, _repository())],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert writer.stdout.readline().strip() == "COMMITTED"
        os.kill(writer.pid, signal.SIGKILL)
        writer.wait()
    finally:
        writer.stdout.close()

    reopened = opened_stores(path, StoreDurability.LEDGER, BUSY_TIMEOUT)
    survived = read_entries_in_window(reopened, "writer-part", 0, time.time_ns())
    assert len(survived) == count, f"{len(survived)} of {count} committed rows survived SIGKILL"


def test_the_current_setting_and_when_it_last_changed_are_both_answerable(durable_tmp_path, opened_stores):
    # RL-055's board question, answered from the journal rather than from mtime or
    # git log -- both of which lie for the reasons section 15.3 records.
    connection = opened_stores(durable_tmp_path / "changes.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    assert read_current_setting_and_change_time(connection, "main_balance") is None

    record_setting_change(connection, "main_balance", "0.0", "1000.0")
    time.sleep(0.01)
    record_setting_change(connection, "main_balance", "1000.0", "2500.0")

    value, changed_at_ns = read_current_setting_and_change_time(connection, "main_balance")
    assert value == "2500.0"
    assert changed_at_ns > 0


def test_it_refuses_a_store_on_a_filesystem_whose_pages_are_memory(tmp_path):
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        open_store(tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
