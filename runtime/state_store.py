"""Structured part state: settings changes, journal entries, provenance, switch records.

SQLite in WAL mode, from the standard library (section 15.2). LMDB was measured
faster at bulk append and lost on three counts that matter more at this cadence:
it would be a dependency where sqlite3 is not one; it is a key/value store with no
answer to a time-window query beyond composite keys every part must encode
identically; and its map_size is a ceiling chosen up front whose growth every other
process has to react to, which cuts against RL-061.

Crash-only, from SQLite's own documentation: "Transactions are durable across
application crashes regardless of the synchronous setting or journal mode."
synchronous governs power loss, not a part being SIGKILLed -- which is exactly the
off switch section 4 defines.
"""

from __future__ import annotations

import enum
import pathlib
import sqlite3
import time
from dataclasses import dataclass

from runtime.storage_facts import require_durable_directory


class StoreDurability(enum.StrEnum):
    """How much a store is willing to lose to a power cut. Never to a SIGKILL."""

    # A committed row must not vanish. One extra WAL sync per commit.
    LEDGER = "ledger"
    # Never corrupt, and may lose only the very last write to a genuine power
    # failure. Correct for provenance, metadata and soft state.
    RECORD = "record"


_SYNCHRONOUS_FOR = {StoreDurability.LEDGER: "FULL", StoreDurability.RECORD: "NORMAL"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entry (
    entry_id       INTEGER PRIMARY KEY,
    part_id        TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    provenance     TEXT    NOT NULL,
    recorded_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS journal_entry_by_part_and_time
    ON journal_entry (part_id, recorded_at_ns);

CREATE TABLE IF NOT EXISTS setting_change (
    change_id      INTEGER PRIMARY KEY,
    field          TEXT    NOT NULL,
    old_value      TEXT    NOT NULL,
    new_value      TEXT    NOT NULL,
    observed_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS setting_change_by_field_and_time
    ON setting_change (field, observed_at_ns);
"""


@dataclass(frozen=True)
class JournalEntry:
    """One immutable thing that happened, and what said so."""

    entry_id: int
    part_id: str
    kind: str
    payload: str
    provenance: str
    recorded_at_ns: int


def open_store(
    path: pathlib.Path,
    durability: StoreDurability,
    busy_timeout_seconds: float,
) -> sqlite3.Connection:
    """Open or create a store, with the pragmas section 15.2 settled.

    busy_timeout is not optional. WAL permits exactly one writer per file; measured
    with it unset a second writer fails instantly with 'database is locked', and with
    it set the same writer waits and succeeds. A part should never carry retry code
    for something the library already does correctly.
    """
    path = pathlib.Path(path)
    require_durable_directory(path.parent)
    connection = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_seconds)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA synchronous={_SYNCHRONOUS_FOR[durability]}")
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}")
    connection.executescript(_SCHEMA)
    return connection


def append_journal_entry(
    connection: sqlite3.Connection,
    part_id: str,
    kind: str,
    payload: str,
    provenance: str,
) -> int:
    """Record one immutable entry and return its id.

    Transactions stay short -- insert and commit, never held across other work --
    because a held write transaction stalls every other writer for its whole duration.
    """
    cursor = connection.execute(
        "INSERT INTO journal_entry (part_id, kind, payload, provenance, recorded_at_ns) "
        "VALUES (?, ?, ?, ?, ?)",
        (part_id, kind, payload, provenance, time.time_ns()),
    )
    return int(cursor.lastrowid)


def read_entries_in_window(
    connection: sqlite3.Connection, part_id: str, start_ns: int, end_ns: int
) -> list[JournalEntry]:
    """What did this part produce between these two moments?

    This query is why the store is relational. In a key/value store it is a composite
    key convention every part has to implement the same way and keep correct.
    """
    rows = connection.execute(
        "SELECT entry_id, part_id, kind, payload, provenance, recorded_at_ns "
        "FROM journal_entry WHERE part_id = ? AND recorded_at_ns >= ? AND recorded_at_ns <= ? "
        "ORDER BY recorded_at_ns, entry_id",
        (part_id, start_ns, end_ns),
    ).fetchall()
    return [JournalEntry(*row) for row in rows]


def record_setting_change(
    connection: sqlite3.Connection, field: str, old_value: str, new_value: str
) -> int:
    """Append one observed settings change. The board's 'last changed' reads this.

    Never the file's mtime, which lies on a no-op save, and never git log, which is
    empty because RL-055's operator edits over SSH and never commits.
    """
    cursor = connection.execute(
        "INSERT INTO setting_change (field, old_value, new_value, observed_at_ns) "
        "VALUES (?, ?, ?, ?)",
        (field, old_value, new_value, time.time_ns()),
    )
    return int(cursor.lastrowid)


def read_current_setting_and_change_time(
    connection: sqlite3.Connection, field: str
) -> tuple[str, int] | None:
    """The value this field last changed to, and when that was observed."""
    row = connection.execute(
        "SELECT new_value, observed_at_ns FROM setting_change WHERE field = ? "
        "ORDER BY observed_at_ns DESC, change_id DESC LIMIT 1",
        (field,),
    ).fetchone()
    return (row[0], int(row[1])) if row else None
