"""When it last changed, measured -- not mtime, not git log.

Section 15.3: mtime lies on a no-op save because vim rewrites the file on :wq;
git log is empty because RL-055's operator edits over SSH and never commits;
inotify can drop events, so an overflow forces a full re-read rather than reading
as nothing happened.
"""

import os
import tempfile
import textwrap
import time

import pytest

from runtime.settings_reader import LastKnownGoodSettings, load_settings_document
from runtime.settings_watcher import SettingsChange, SettingsDirectoryWatch, compare_settings_documents

TEMPLATE = textwrap.dedent(
    """
    [main_balance]
    value = {balance}
    unit  = "USDT"
    note  = "operator, 2026-08-20: test"
    """
).strip()

SETTLE_SECONDS = 2.0
# Fast on purpose: these tests aren't measuring the production cadence (that
# number lives in settings/runtime.example.toml with its own provenance), only
# that the backstop's clock and its is_periodic distinction behave correctly.
RECHECK_INTERVAL_SECONDS = 0.05


def _write(directory, balance: str):
    path = directory / "main-account.toml"
    path.write_text(TEMPLATE.format(balance=balance))
    return path


def _write_by_rename(directory, balance: str):
    """What an editor with atomic-save actually does: write elsewhere in the
    same directory, then os.replace() over the target. This swaps the inode --
    unlike _write(), which truncates and rewrites the same one in place.
    """
    path = directory / "main-account.toml"
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(TEMPLATE.format(balance=balance))
    os.replace(tmp_name, path)
    return path


def test_a_changed_value_is_reported_with_both_sides(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    _write(durable_tmp_path, "2500.0")
    candidate = load_settings_document(path, scope="main-account")

    changes = compare_settings_documents(previous, candidate)
    assert changes == [
        SettingsChange(
            field="main_balance", old_value="1000.0", new_value="2500.0",
            observed_at_ns=changes[0].observed_at_ns,
        )
    ]


def test_a_no_op_save_produces_no_change(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    time.sleep(0.01)
    _write(durable_tmp_path, "1000.0")  # what vim does on :wq with nothing edited
    candidate = load_settings_document(path, scope="main-account")
    assert compare_settings_documents(previous, candidate) == []


def test_an_added_entry_reports_as_a_change_from_nothing(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    path.write_text(
        TEMPLATE.format(balance="1000.0")
        + '\n\n[leverage_ceiling]\nvalue = 3.0\nunit = "multiple"\nnote = "operator: test"\n'
    )
    candidate = load_settings_document(path, scope="main-account")
    changes = compare_settings_documents(previous, candidate)
    assert [change.field for change in changes] == ["leverage_ceiling"]
    assert changes[0].old_value == ""


@pytest.mark.slow
def test_an_edit_over_ssh_wakes_the_watch_and_reports_the_change(durable_tmp_path):
    _write(durable_tmp_path, "1000.0")
    seen: list[list[SettingsChange]] = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert seen, "the watch did not wake on an edit"
    assert seen[0][0].new_value == "2500.0"


@pytest.mark.slow
def test_a_write_temp_then_rename_save_is_seen(durable_tmp_path):
    # What an editor with atomic-save actually does, and the reason the docstring
    # gives for watching the DIRECTORY: the rename replaces the inode, so a watch
    # on the file alone would be left pointing at one nobody writes again.
    _write(durable_tmp_path, "1000.0")
    seen: list[list[SettingsChange]] = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write_by_rename(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert seen, "a write-temp-then-rename save was not seen"
    assert seen[0][0].new_value == "2500.0"


@pytest.mark.slow
def test_renaming_a_settings_file_away_reports_its_entries_as_removed(durable_tmp_path):
    # The other half of a move: the settings file's name leaving is a change too,
    # not just a name arriving. A move event carries the origin in src_path and
    # the destination in dest_path -- this is the src_path side.
    path = _write(durable_tmp_path, "1000.0")
    seen: list[list[SettingsChange]] = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        os.replace(path, durable_tmp_path / "main-account.toml.bak")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert seen, "a settings file being renamed away was not reported"
    assert seen[0] == [
        SettingsChange(
            field="main_balance", old_value="1000.0", new_value="",
            observed_at_ns=seen[0][0].observed_at_ns,
        )
    ]


@pytest.mark.slow
def test_a_broken_edit_reports_a_rejection_and_does_not_report_a_change(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    rejections: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=rejections.append, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        path.write_text("[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not rejections and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert rejections, "a broken edit was not reported"
    assert changes == [], "a broken edit must not take effect"


def test_recheck_now_re_reads_without_waiting_for_an_event(durable_tmp_path):
    # A caller-driven re-establishment of ground truth, without waiting on any
    # timer or event -- the watch is never even started here.
    _write(durable_tmp_path, "1000.0")
    seen: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(durable_tmp_path, "2500.0")
    watch.recheck_now()
    assert seen and seen[0][0].new_value == "2500.0"


def test_recheck_now_does_not_claim_the_event_stream_missed_anything(durable_tmp_path):
    # The mirror of the periodic-backstop test below: a recheck a caller asked
    # for -- the same internal path an inotify event drives via recheck_now() --
    # must never report on_overflow. Only a periodic pass finding an unannounced
    # diff is evidence the event stream did not already handle it.
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    overflows: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: overflows.append(True),
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(path.parent, "2500.0")
    watch.recheck_now()

    assert changes and changes[0][0].new_value == "2500.0"
    assert overflows == [], "recheck_now() must never claim the event stream missed something"


def test_a_periodic_recheck_flags_overflow_for_a_change_no_event_could_have_announced(
    durable_tmp_path,
):
    # The backstop itself, called directly and synchronously -- exactly what its
    # own clock thread calls (_run_periodic_recheck). The watch is never started,
    # so no observer and no periodic thread are running: this edit genuinely
    # cannot have been announced by any event, only found by a direct call to the
    # same recheck the clock uses. That is the deterministic, no-sleep way to
    # exercise the backstop's actual triggering condition -- an inotify overflow
    # is real (proven separately against this watch's watchdog==6.0.0 dependency:
    # a raw 49152-event burst produced a genuine wd==-1 IN_Q_OVERFLOW record) but
    # its timing is not something a test should have to depend on to pass.
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    overflows: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: overflows.append(True),
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(path.parent, "2500.0")
    watch._recheck(is_periodic=True)

    assert overflows, "a change no event could have announced was not flagged"
    assert changes and changes[0][0].new_value == "2500.0"


@pytest.mark.slow
def test_the_periodic_backstop_runs_on_its_own_clock_without_flagging_a_delivered_edit(
    durable_tmp_path,
):
    # End-to-end with the real observer and the real periodic thread both
    # running: an edit inotify actually delivers must be reported once, as a
    # plain change, and several backstop ticks afterward must not relabel it
    # (or anything else) as overflow.
    _write(durable_tmp_path, "1000.0")
    changes: list = []
    overflows: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: overflows.append(True),
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not changes and time.monotonic() < deadline:
            time.sleep(0.02)
        # Give the backstop's own clock several ticks after the edit already
        # settled. Correctness here does not depend on timing: once _known is
        # in sync, every subsequent periodic pass finds zero diff regardless of
        # how many ticks land, so this cannot flake into a false pass.
        time.sleep(RECHECK_INTERVAL_SECONDS * 5)
    finally:
        watch.stop()

    assert changes and changes[0][0].new_value == "2500.0"
    assert len(changes) == 1, "the already-delivered edit must not be reported twice"
    assert overflows == [], "an edit the event stream actually delivered must not read as overflow"
