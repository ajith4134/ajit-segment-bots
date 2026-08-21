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


# ---------------------------------------------------------------------------
# Round 2: a raising callback must not silently disable either line of
# defence, on_overflow must not blame the event stream for a race it won,
# stop() must survive a failed start(), a standing rejection must not flood,
# and a newly-appeared file must be as visible as a vanished one.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_raising_on_change_does_not_kill_the_watch_and_the_change_is_retried(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    attempts: list[list[SettingsChange]] = []

    def flaky_on_change(changes):
        attempts.append(changes)
        if len(attempts) == 1:
            raise RuntimeError("transient store write failure")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=flaky_on_change,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

        assert len(attempts) >= 2, "the change was not retried after the callback raised"
        assert attempts[0][0].new_value == "2500.0"
        assert attempts[1][0].new_value == "2500.0", "the retry must be the real change, not something else"
        assert watch._observer.is_alive(), "a raising on_change must not kill the observer thread"
        assert watch._periodic_thread.is_alive(), "a raising on_change must not kill the backstop thread"
    finally:
        watch.stop()


@pytest.mark.slow
def test_a_raising_on_rejection_does_not_kill_the_watch_and_is_retried(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    attempts: list = []

    def flaky_on_rejection(rejection):
        attempts.append(rejection)
        if len(attempts) == 1:
            raise RuntimeError("transient recorder failure")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=flaky_on_rejection, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        path.write_text("[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

        assert len(attempts) >= 2, "the rejection was not retried after the callback raised"
        assert watch._observer.is_alive(), "a raising on_rejection must not kill the observer thread"
        assert watch._periodic_thread.is_alive(), "a raising on_rejection must not kill the backstop thread"
    finally:
        watch.stop()


def test_a_raising_on_overflow_does_not_kill_the_backstop_and_is_retried(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    overflow_attempts: list = []
    changes: list = []

    def flaky_on_overflow():
        overflow_attempts.append(True)
        if len(overflow_attempts) == 1:
            raise RuntimeError("transient overflow-handler failure")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=flaky_on_overflow,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(path.parent, "2500.0")
    watch._recheck(is_periodic=True)  # on_overflow raises here
    watch._recheck(is_periodic=True)  # retried: on_overflow succeeds, diff still outstanding

    assert len(overflow_attempts) == 2, "on_overflow must be retried until it stops raising"
    assert changes, "the real change must still surface once delivery succeeds"
    assert all(c[0].new_value == "2500.0" for c in changes), "every report must be the real diff"


def test_an_in_flight_event_suppresses_overflow_but_the_change_still_reports(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    overflows: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: overflows.append(True),
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(path.parent, "2500.0")
    # The adversarial ordering: mark the path in flight -- exactly what
    # on_any_event does, before it ever contends for the lock -- then drive a
    # periodic pass directly, simulating the backstop's clock winning the race
    # with the event's own (not-yet-run) recheck.
    watch._mark_in_flight(path)
    watch._recheck(is_periodic=True)

    assert overflows == [], "a path with an event already in flight must not be blamed for the stream"
    assert changes and changes[0][0].new_value == "2500.0", "the real change must still be reported"


def test_the_in_flight_mark_clears_once_the_events_own_recheck_runs(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch._mark_in_flight(path)
    assert watch._is_in_flight(path)
    watch.recheck_now()  # the non-periodic pass that "owns" resolving this mark
    assert not watch._is_in_flight(path), "the event's own recheck must clear its mark"


def test_stop_after_a_failed_start_does_not_mask_the_real_error(durable_tmp_path):
    missing = durable_tmp_path / "does-not-exist"
    watch = SettingsDirectoryWatch(
        directory=missing, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    with pytest.raises(FileNotFoundError):
        try:
            watch.start()
        finally:
            # Must not itself raise -- a RuntimeError from a half-started watch
            # would replace the real FileNotFoundError above with a misleading one.
            watch.stop()


def test_stop_before_start_is_a_safe_no_op(durable_tmp_path):
    # An object nobody ever start()ed -- e.g. a caller that constructs, decides
    # against it, and tears down. Neither thread ever ran, so both must already
    # read as not alive, and stop() must not raise reaching for either.
    _write(durable_tmp_path, "1000.0")
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.stop()
    assert not watch._observer.is_alive()
    assert not watch._periodic_thread.is_alive()


def test_stop_is_safe_when_called_twice_after_a_normal_start(durable_tmp_path):
    _write(durable_tmp_path, "1000.0")
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    watch.stop()
    watch.stop()  # called twice -- must not raise, must not resurrect anything
    assert not watch._observer.is_alive()
    assert not watch._periodic_thread.is_alive()


def test_after_stop_neither_thread_is_alive(durable_tmp_path):
    _write(durable_tmp_path, "1000.0")
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    assert watch._observer.is_alive()
    assert watch._periodic_thread.is_alive()
    watch.stop()
    assert not watch._observer.is_alive(), "stop() must actually end the observer thread"
    assert not watch._periodic_thread.is_alive(), "stop() must actually end the backstop thread"


@pytest.mark.slow
def test_a_standing_rejection_is_reported_once_not_once_per_tick(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    rejections: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=rejections.append, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        path.write_text("[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not rejections and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rejections, "the broken edit was never reported"
        # Several backstop ticks over the same standing broken file.
        time.sleep(RECHECK_INTERVAL_SECONDS * 8)
    finally:
        watch.stop()

    assert len(rejections) == 1, "the same broken content must be reported once, not once per tick"


def test_editing_into_a_different_broken_state_reports_again(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    rejections: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=rejections.append, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    path.write_text("[main_balance]\nvalue = [1, 2,\n")
    watch.recheck_now()
    assert len(rejections) == 1
    watch.recheck_now()
    assert len(rejections) == 1, "the identical broken content must not be reported twice"

    path.write_text("this is not toml at all ]][[\n")
    watch.recheck_now()
    assert len(rejections) == 2, "a different broken state is a new, reportable rejection"


def test_a_recovered_then_re_broken_file_reports_the_rejection_again(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    rejections: list = []
    changes: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=rejections.append, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    path.write_text("[main_balance]\nvalue = [1, 2,\n")
    watch.recheck_now()
    assert len(rejections) == 1

    _write(path.parent, "2500.0")  # a real, valid fix
    watch.recheck_now()
    assert changes and changes[0][0].new_value == "2500.0"

    path.write_text("[main_balance]\nvalue = [1, 2,\n")  # broken again, same bytes as before
    watch.recheck_now()
    assert len(rejections) == 2, "a rejection cleared by a successful edit is reportable again"


@pytest.mark.slow
def test_a_failed_change_delivery_retries_while_a_standing_rejection_does_not(durable_tmp_path):
    # The deliberate tension fix 1 and fix 4 create, in one test: unconsumed
    # work is retried until it lands; an already-reported state is not repeated.
    path = _write(durable_tmp_path, "1000.0")
    change_attempts: list = []
    rejections: list = []

    def flaky_on_change(changes):
        change_attempts.append(changes)
        if len(change_attempts) == 1:
            raise RuntimeError("transient failure")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=flaky_on_change,
        on_rejection=rejections.append, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(change_attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(change_attempts) >= 2, "the change must be retried after the callback failed"

        path.write_text("[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not rejections and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(RECHECK_INTERVAL_SECONDS * 8)
    finally:
        watch.stop()

    assert len(rejections) == 1, "the rejection must not repeat across the same ticks that retried the change"


@pytest.mark.slow
def test_a_settings_file_appearing_after_start_reports_its_initial_entries(durable_tmp_path):
    changes: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch.start()
    try:
        _write_by_rename(durable_tmp_path, "1000.0")  # created fresh, after start()
        deadline = time.monotonic() + SETTLE_SECONDS
        while not changes and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert changes, "a settings file appearing after start() was not reported"
    assert changes[0] == [
        SettingsChange(
            field="main_balance", old_value="", new_value="1000.0",
            observed_at_ns=changes[0][0].observed_at_ns,
        )
    ]


def test_recheck_now_reports_a_newly_discovered_files_entries_as_new(durable_tmp_path):
    changes: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(durable_tmp_path, "1000.0")  # the directory was empty at construction
    watch.recheck_now()

    assert changes and changes[0][0] == SettingsChange(
        field="main_balance", old_value="", new_value="1000.0",
        observed_at_ns=changes[0][0].observed_at_ns,
    )
