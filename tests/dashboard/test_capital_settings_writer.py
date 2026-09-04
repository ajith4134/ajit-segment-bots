"""Changing a capital setting is refused unless it is safe, and never silently.

This is the path that can move the numbers the bot sizes trades against, reachable
from a public URL. RL-055 decided these were edited over SSH with the board only
displaying them; the operator reversed that on 2026-08-25, and the premise changed
with it -- an edit form on a public URL is a public write endpoint.

So every guard is tested, and the refusals matter more than the acceptance.
"""

from __future__ import annotations

import pytest

from dashboard.capital_settings_writer import (
    ACCEPTED,
    REFUSED_CONTRADICTS,
    REFUSED_NOT_A_NUMBER,
    REFUSED_NOT_EDITABLE,
    REFUSED_REAL_MONEY,
    REFUSED_UNCHANGED,
    AttemptLimiter,
    find_contradictions,
    judge_change,
    read_number,
    write_setting,
    segment_scope_name,
)

# Which segment's capital is editable is `segment_id`'s to say, not this file's.
# These tests said "futures" until 2026-09-04, which is what let the board go on
# reading and writing the retired crypto segment after segment_id became
# index-options -- a hardcoded scope in the tests could not catch a hardcoded
# scope in the code.
SEGMENT = segment_scope_name()

# The shipped values, as the operator's own files hold them.
CURRENT = {
    ("main-account", "main_balance"): 10_000.0,
    ("main-account", "maximum_capital_per_trade"): 1_000.0,
    ("main-account", "leverage_ceiling"): 1.0,
    (SEGMENT, "allocated_balance"): 10_000.0,
    (SEGMENT, "minimum_capital_per_trade"): 10.0,
    (SEGMENT, "maximum_capital_per_trade"): 1_000.0,
    (SEGMENT, "leverage_ceiling"): 1.0,
}


# ---- what may be changed at all ---------------------------------------------


def a_runtime_naming_the_segment(root):
    """The `segment_id` the board reads to know whose capital it is editing.

    Written into every temporary settings directory because the code reads it
    rather than assuming a segment -- which is exactly the fix these tests
    cover. A harness that omitted it would be asking the code to guess.
    """
    (root / "runtime.toml").write_text(
        "[segment_id]\n"
        f'value = "{SEGMENT}"\n'
        'unit  = "segment"\n'
        'note  = "a test\'s segment."\n'
    )


def test_money_mode_is_refused_however_valid_it_looks():
    """RL-005 is paper-first. A public endpoint is not where real money starts."""
    verdict = judge_change(SEGMENT, "money_mode", "live", CURRENT)
    assert not verdict.is_accepted
    assert verdict.reason == REFUSED_REAL_MONEY


def test_a_setting_nobody_listed_is_refused():
    """An allowlist refuses the thing nobody thought of; a blocklist admits it."""
    for name in ("quote_currency", "captured_symbol_count", "journal_path", "anything"):
        verdict = judge_change(SEGMENT, name, 5.0, CURRENT)
        assert not verdict.is_accepted
        assert verdict.reason == REFUSED_NOT_EDITABLE


def test_a_value_that_is_not_a_number_is_refused_rather_than_zeroed():
    for value in ("", "abc", None, [], {}, "1.2.3"):
        verdict = judge_change(SEGMENT, "allocated_balance", value, CURRENT)
        assert not verdict.is_accepted, f"{value!r} was accepted"
        assert verdict.reason == REFUSED_NOT_A_NUMBER


def test_true_is_not_a_number_even_though_python_says_it_is():
    """bool is an int in Python, and a leverage ceiling of True is not a ceiling."""
    assert read_number(True) is None
    assert not judge_change(SEGMENT, "leverage_ceiling", True, CURRENT).is_accepted


def test_a_negative_capital_setting_is_refused():
    verdict = judge_change(SEGMENT, "allocated_balance", -1.0, CURRENT)
    assert not verdict.is_accepted


def test_a_value_that_is_already_set_is_refused_rather_than_rewritten():
    """A no-op write would append a note line claiming a change nobody made."""
    verdict = judge_change(SEGMENT, "allocated_balance", 10_000.0, CURRENT)
    assert not verdict.is_accepted
    assert verdict.reason == REFUSED_UNCHANGED


# ---- the contradictions the validator refuses to trade under ----------------

def test_a_minimum_above_the_maximum_is_refused():
    verdict = judge_change(SEGMENT, "minimum_capital_per_trade", 5_000.0, CURRENT)
    assert not verdict.is_accepted
    assert verdict.reason == REFUSED_CONTRADICTS
    assert any("no order size satisfies both" in fault for fault in verdict.faults)


def test_a_maximum_above_the_allocation_is_refused():
    verdict = judge_change(SEGMENT, "maximum_capital_per_trade", 50_000.0, CURRENT)
    assert not verdict.is_accepted
    assert any("of an allocation of" in fault for fault in verdict.faults)


def test_an_allocation_above_the_main_balance_is_refused():
    """The money is not there, and each segment would be individually correct."""
    verdict = judge_change(SEGMENT, "allocated_balance", 50_000.0, CURRENT)
    assert not verdict.is_accepted
    assert any("the money is not there" in fault for fault in verdict.faults)


def test_a_leverage_ceiling_below_unlevered_is_refused():
    verdict = judge_change(SEGMENT, "leverage_ceiling", 0.5, CURRENT)
    assert not verdict.is_accepted
    assert any("below unlevered" in fault for fault in verdict.faults)


def test_lowering_the_main_balance_under_the_allocation_is_refused():
    """The cross-check runs on the file as it WOULD be, not as it is."""
    verdict = judge_change("main-account", "main_balance", 500.0, CURRENT)
    assert not verdict.is_accepted
    assert any("the money is not there" in fault for fault in verdict.faults)


def test_a_change_that_is_individually_fine_and_jointly_wrong_is_caught():
    """Each of these is sensible alone; together they contradict."""
    assert find_contradictions({
        (SEGMENT, "minimum_capital_per_trade"): 900.0,
        (SEGMENT, "maximum_capital_per_trade"): 1_000.0,
        (SEGMENT, "allocated_balance"): 500.0,
    })


def test_the_shipped_settings_do_not_contradict_each_other():
    """A guard on the guard: if this fails the operator's file is already broken."""
    assert find_contradictions(CURRENT) == []


# ---- what is accepted -------------------------------------------------------

def test_a_safe_change_is_accepted():
    verdict = judge_change(SEGMENT, "maximum_capital_per_trade", 500.0, CURRENT)
    assert verdict.is_accepted
    assert verdict.reason == ACCEPTED


def test_raising_leverage_within_the_rules_is_accepted():
    """RL-041: 5x, 10x, 20x are the operator's to choose."""
    for ceiling in (2.0, 5.0, 10.0, 20.0):
        assert judge_change(SEGMENT, "leverage_ceiling", ceiling, CURRENT).is_accepted


# ---- the door ---------------------------------------------------------------

def test_the_wait_doubles_with_each_failure():
    clock = [0.0]
    limiter = AttemptLimiter(first_wait_seconds=1.0, _now=lambda: clock[0])

    assert limiter.record_failure() == 1.0
    assert limiter.record_failure() == 2.0
    assert limiter.record_failure() == 4.0
    assert limiter.failures == 3


def test_the_wait_is_capped_so_a_typo_is_not_a_permanent_lockout():
    """An unbounded wait is a denial of service the attacker did not have to win."""
    clock = [0.0]
    limiter = AttemptLimiter(first_wait_seconds=1.0, longest_wait_seconds=60.0, _now=lambda: clock[0])
    for _ in range(30):
        wait = limiter.record_failure()
    assert wait == 60.0


def test_a_blocked_door_stays_blocked_until_the_wait_has_passed():
    clock = [0.0]
    limiter = AttemptLimiter(first_wait_seconds=10.0, _now=lambda: clock[0])
    limiter.record_failure()

    assert limiter.is_blocked()
    clock[0] = 9.0
    assert limiter.is_blocked()
    clock[0] = 10.5
    assert not limiter.is_blocked()


def test_success_clears_the_penalty():
    clock = [0.0]
    limiter = AttemptLimiter(first_wait_seconds=5.0, _now=lambda: clock[0])
    limiter.record_failure()
    limiter.record_success()

    assert not limiter.is_blocked()
    assert limiter.failures == 0
    # And the next failure starts from the first wait rather than where it left off.
    assert limiter.record_failure() == 5.0


# ---- writing the file -------------------------------------------------------

def test_writing_keeps_the_note_and_appends_what_changed(tmp_path, monkeypatch):
    """settings_reader refuses an entry with no note, so losing it breaks every part."""
    root = tmp_path / "settings"
    (root / "segments").mkdir(parents=True)
    a_runtime_naming_the_segment(root)
    original = '''# a comment that must survive

[allocated_balance]
value = 10000.0
unit  = "USDT"
note  = "operator, 2026-08-22: the balance this segment trades against."

[leverage_ceiling]
value = 1.0
unit  = "multiple"
note  = "operator: unlevered."
'''
    (root / "segments" / f"{SEGMENT}.toml").write_text(original)
    monkeypatch.setattr(
        "runtime.settings_reader.settings_directory", lambda: root
    )

    write_setting(SEGMENT, "allocated_balance", 5_000.0, changed_by="a test")
    written = (root / "segments" / f"{SEGMENT}.toml").read_text()

    assert "value = 5000.0" in written
    assert "the balance this segment trades against." in written, "the note was lost"
    assert "Changed from 10000.0 to 5000.0 by a test" in written
    # Everything else is untouched.
    assert "# a comment that must survive" in written
    assert 'note  = "operator: unlevered."' in written
    assert "value = 1.0" in written


def test_writing_refuses_a_section_that_does_not_exist(tmp_path, monkeypatch):
    root = tmp_path / "settings"
    (root / "segments").mkdir(parents=True)
    a_runtime_naming_the_segment(root)
    (root / "segments" / f"{SEGMENT}.toml").write_text("[something_else]\nvalue = 1\n")
    monkeypatch.setattr("runtime.settings_reader.settings_directory", lambda: root)

    with pytest.raises(KeyError):
        write_setting(SEGMENT, "allocated_balance", 5_000.0)


def test_writing_refuses_an_entry_with_no_note(tmp_path, monkeypatch):
    """Rather than write a file no part can read."""
    root = tmp_path / "settings"
    (root / "segments").mkdir(parents=True)
    a_runtime_naming_the_segment(root)
    (root / "segments" / f"{SEGMENT}.toml").write_text(
        '[allocated_balance]\nvalue = 10000.0\nunit  = "USDT"\n'
    )
    monkeypatch.setattr("runtime.settings_reader.settings_directory", lambda: root)

    with pytest.raises(KeyError):
        write_setting(SEGMENT, "allocated_balance", 5_000.0)


def test_the_written_file_is_still_readable_by_the_settings_reader(tmp_path, monkeypatch):
    """The test that matters: a part must still be able to load it."""
    from runtime.settings_reader import load_settings_document

    root = tmp_path / "settings"
    (root / "segments").mkdir(parents=True)
    a_runtime_naming_the_segment(root)
    (root / "segments" / f"{SEGMENT}.toml").write_text(
        '[allocated_balance]\nvalue = 10000.0\nunit  = "USDT"\nnote  = "operator: the balance."\n'
    )
    monkeypatch.setattr("runtime.settings_reader.settings_directory", lambda: root)

    write_setting(SEGMENT, "allocated_balance", 2_500.0, changed_by="a test")

    document = load_settings_document(root / "segments" / f"{SEGMENT}.toml", SEGMENT)
    assert document.entries["allocated_balance"].value == 2_500.0


def test_the_next_section_is_not_swallowed_onto_the_note(tmp_path, monkeypatch):
    """The defect a synthetic fixture with nothing after the note could not show.

    `\\s*$` in MULTILINE swallows the line break -- `\\s` matches a newline and `$`
    matches at the next boundary -- so the following section header landed on the
    end of the note and the file stopped being valid TOML. Every part reads these
    files, so that breaks all of them at once.

    Found on 2026-08-25 by writing to a copy of the operator's real settings
    rather than only to a fixture built for the test.
    """
    from runtime.settings_reader import load_settings_document

    root = tmp_path / "settings"
    (root / "segments").mkdir(parents=True)
    a_runtime_naming_the_segment(root)
    (root / "segments" / f"{SEGMENT}.toml").write_text(
        '[allocated_balance]\n'
        'value = 10000.0\n'
        'unit  = "USDT"\n'
        'note  = "operator: the balance."\n'
        '\n'
        '[minimum_capital_per_trade]\n'
        'value = 10.0\n'
        'unit  = "USDT"\n'
        'note  = "operator: the floor."\n'
    )
    monkeypatch.setattr("runtime.settings_reader.settings_directory", lambda: root)

    write_setting(SEGMENT, "allocated_balance", 5_000.0, changed_by="a test")
    written = (root / "segments" / f"{SEGMENT}.toml").read_text()

    assert "\n[minimum_capital_per_trade]" in written, "the section header was swallowed"
    document = load_settings_document(root / "segments" / f"{SEGMENT}.toml", SEGMENT)
    assert document.entries["allocated_balance"].value == 5_000.0
    assert document.entries["minimum_capital_per_trade"].value == 10.0
    assert len(document.entries) == 2
