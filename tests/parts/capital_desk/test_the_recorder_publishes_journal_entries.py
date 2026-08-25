"""A part publishes the shape it declares, or it breaks everyone reading that type.

`capital-settings-change-recorder` declares `produces: journal-entry`. It built a
proper `JournalEntry` in `_record` and threw it away, publishing raw
`SettingChange` objects instead. The moment it was first switched on --
2026-08-25, phase 8 -- three parts that had been running happily for hours began
crash-looping on

    AttributeError: 'SettingChange' object has no attribute 'payload'

entry-quality-scorer, trade-episode-encoder and trade-replay-verifier. None of
them had changed. They read `journal-entry`, and a new producer of that type
started emitting something that was not one.

This is the third time in one day that starting a producer broke consumers of a
shared type: candles onto `market-data` was the first two. The difference matters.
Those consumers were assuming a stream carried only what they wanted, and the fix
was a guard at the reader. This one is the producer lying about its own contract,
and the fix belongs at the producer -- guarding the readers would have taught them
to tolerate a part that does not do what the blueprint says it does.
"""

from __future__ import annotations

import time

from parts.capital_desk.capital_settings_change_recorder import (
    PART_DECLARATION,
    CapitalSettingsChangeRecorder,
)
from runtime.journal import Journal


def a_recorder() -> CapitalSettingsChangeRecorder:
    written: list[str] = []
    journal = Journal(append_line=written.append, continues_from=None)
    return CapitalSettingsChangeRecorder(journal=journal, now_ns=time.time_ns)


def test_the_part_declares_it_produces_journal_entry():
    assert "journal-entry" in PART_DECLARATION.produces


def test_what_it_hands_on_carries_a_payload():
    """The attribute whose absence crash-looped three parts."""
    recorder = a_recorder()
    recorder.observe("futures", {"allocated_balance": 10_000.0})

    entries = recorder.take_journal_entries()

    assert entries, "observing a setting for the first time must produce an entry"
    for entry in entries:
        assert hasattr(entry, "payload"), f"{type(entry).__name__} has no payload"
        assert hasattr(entry, "kind")
        assert isinstance(entry.payload, dict)


def test_the_payload_carries_the_change_itself():
    recorder = a_recorder()
    recorder.observe("futures", {"leverage_ceiling": 1.0})
    recorder.take_journal_entries()
    recorder.observe("futures", {"leverage_ceiling": 3.0})

    entry = recorder.take_journal_entries()[0]

    assert entry.payload["setting"] == "leverage_ceiling"
    assert entry.payload["previous_value"] == 1.0
    assert entry.payload["new_value"] == 3.0


def test_entries_are_handed_on_once_and_not_repeated():
    """A recorder that re-published its whole history every tick would flood the bus."""
    recorder = a_recorder()
    recorder.observe("futures", {"allocated_balance": 10_000.0})

    first = recorder.take_journal_entries()
    second = recorder.take_journal_entries()

    assert len(first) == 1
    assert second == ()


def test_a_setting_that_has_not_moved_produces_no_entry():
    """Only news. An unchanged setting restated is not a change."""
    recorder = a_recorder()
    recorder.observe("futures", {"allocated_balance": 10_000.0})
    recorder.take_journal_entries()

    recorder.observe("futures", {"allocated_balance": 10_000.0})

    assert recorder.take_journal_entries() == ()


def test_the_change_itself_is_still_returned_from_observe():
    """The two answer different questions and both are wanted.

    A caller reasoning about a setting wants the change; the bus wants the entry.
    Collapsing them is what caused the defect.
    """
    recorder = a_recorder()
    changes = recorder.observe("futures", {"minimum_capital_per_trade": 10.0})

    assert changes
    assert changes[0].setting == "minimum_capital_per_trade"
    assert changes[0].is_first_reading


def test_last_changed_comes_from_the_journal_not_a_file(tmp_path):
    """RL-055: the board's 'when did this last change' has one legal source."""
    recorder = a_recorder()
    recorder.observe("futures", {"leverage_ceiling": 1.0})
    first_at = recorder.last_changed_at("futures", "leverage_ceiling")

    recorder.observe("futures", {"leverage_ceiling": 5.0})
    second_at = recorder.last_changed_at("futures", "leverage_ceiling")

    assert first_at is not None
    assert second_at is not None and second_at >= first_at
    assert recorder.last_changed_at("futures", "never_set") is None
