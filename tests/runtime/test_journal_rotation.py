"""A journal that grows without bound is a disk that fills, and deleting it is evidence lost.

Measured 2026-09-04: the five journals held 38 GB between them, 15.4 GB of it in
one `learning-recorder` file -- 75% `decision-rationale` -- written on a day when
no trade had been placed. The journal is the only account of what the system did,
so the answer cannot be to drop the oldest part of it.

Rotation is the answer that keeps both properties: the file changes, the chain
does not. A closed segment is renamed, never removed, and the next entry still
carries the digest of the one before it.
"""

from __future__ import annotations

import json

import pytest

from runtime.journal import (
    GENESIS_DIGEST,
    Journal,
    RollingJournalSink,
    read_journal_tail,
    segment_paths_for,
)


def _write(sink, journal, count, kind="switch-record"):
    """The recorder's whole job: append. The sink rolls itself."""
    for index in range(count):
        journal.append(kind, "control-recorder", {"index": index, "padding": "x" * 200})


def test_a_segment_rolls_when_it_is_full_and_nothing_is_deleted(tmp_path):
    live = tmp_path / "journal.control-recorder.sqlite"
    sink = RollingJournalSink(live, maximum_bytes=2_000)
    journal = Journal(append_line=sink.append_line)

    _write(sink, journal, 60)

    segments = segment_paths_for(live)
    assert sink.rolls >= 2
    assert len(segments) == sink.rolls + 1
    assert segments[-1] == live
    # Every entry is still on disk somewhere: nothing rotates by forgetting.
    lines = [line for path in segments for line in path.read_text().splitlines() if line]
    assert len(lines) == 60


def test_the_chain_is_unbroken_across_a_roll(tmp_path):
    """The property rotation must not cost.

    Each entry carries the digest of the one before it, and the one before it may
    be the last line of the previous file. A reader walking `segment_paths_for` in
    order sees exactly the chain it would have seen in a single file.
    """
    live = tmp_path / "journal.control-recorder.sqlite"
    sink = RollingJournalSink(live, maximum_bytes=2_000)
    journal = Journal(append_line=sink.append_line)

    _write(sink, journal, 60)

    previous = GENESIS_DIGEST
    sequence = 0
    for path in segment_paths_for(live):
        for line in path.read_text().splitlines():
            if not line:
                continue
            entry = json.loads(line)
            sequence += 1
            assert entry["sequence"] == sequence
            assert entry["previous_digest"] == previous
            previous = entry["digest"]
    assert sequence == 60


def test_a_restart_after_a_roll_continues_from_the_live_segment(tmp_path):
    """`read_journal_tail` reads the live file, which is where the chain now ends."""
    live = tmp_path / "journal.control-recorder.sqlite"
    sink = RollingJournalSink(live, maximum_bytes=2_000)
    journal = Journal(append_line=sink.append_line)
    _write(sink, journal, 60)
    ended_at = journal.last_digest

    tail = read_journal_tail(live)
    assert tail is not None
    assert tail.digest == ended_at

    resumed = Journal(append_line=sink.append_line, continues_from=tail)
    entry = resumed.append("switch-record", "control-recorder", {"after": "a restart"})
    assert entry.previous_digest == ended_at
    assert entry.sequence == 61


def test_rolling_never_overwrites_a_closed_segment(tmp_path):
    """Silently clobbering a closed segment would destroy the evidence, quietly."""
    live = tmp_path / "journal.control-recorder.sqlite"
    sink = RollingJournalSink(live, maximum_bytes=100)
    live.write_text("x" * 500)
    (tmp_path / "journal.control-recorder.7.sqlite").write_text("a closed segment")

    with pytest.raises(FileExistsError):
        sink.roll_if_full(7)


def test_a_segment_bound_that_is_not_positive_is_refused(tmp_path):
    """Zero would roll on every entry -- one file per record, not a policy."""
    with pytest.raises(ValueError):
        RollingJournalSink(tmp_path / "j.sqlite", maximum_bytes=0)
    with pytest.raises(ValueError):
        RollingJournalSink(tmp_path / "j.sqlite", maximum_bytes=-1)


def test_segments_sort_by_sequence_not_by_name(tmp_path):
    """`.9.` must come before `.10.`, which string ordering gets backwards."""
    live = tmp_path / "journal.control-recorder.sqlite"
    for sequence in (9, 10, 100):
        (tmp_path / f"journal.control-recorder.{sequence}.sqlite").write_text("")
    live.write_text("")

    assert [p.name for p in segment_paths_for(live)] == [
        "journal.control-recorder.9.sqlite",
        "journal.control-recorder.10.sqlite",
        "journal.control-recorder.100.sqlite",
        "journal.control-recorder.sqlite",
    ]
