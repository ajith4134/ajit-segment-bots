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
    return _write_raw_by_rename(directory, TEMPLATE.format(balance=balance))


def _write_raw_by_rename(directory, content: str):
    """Same atomic swap as _write_by_rename, for content that need not parse --
    used for broken-edit tests against a live, running watch. path.write_text()
    truncates before it writes, and a background thread polling fast enough can
    read the file in between: an empty file is valid, empty TOML, so a plain
    in-place write can produce a real, spurious "every entry removed" diff
    purely from the timing of the read, independent of anything this module
    does. os.replace() is atomic at the filesystem level -- no reader ever sees
    a partial file -- which is exactly why the module watches for it.
    """
    path = directory / "main-account.toml"
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
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
        _write_by_rename(durable_tmp_path, "2500.0")
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
        _write_raw_by_rename(path.parent, "[main_balance]\nvalue = [1, 2,\n")
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
    #
    # This is the one test in the file that needs a wider interval than
    # RECHECK_INTERVAL_SECONDS. The in-flight mark is set inside on_any_event,
    # which only runs once watchdog's own dispatcher thread has pulled the raw
    # kernel event off the queue -- there is real, if normally sub-millisecond,
    # latency between os.replace() landing on disk and that dispatch actually
    # happening. A periodic tick landing in that specific window would find the
    # real diff before any mark exists to suppress it, and -- correctly, if
    # eagerly -- report it as an overflow; that is a narrower, earlier race
    # than the one the mark is built to solve (an event already marked racing
    # the lock), and no mark can protect a window that starts before the mark
    # is set. It cost one flake at RECHECK_INTERVAL_SECONDS=0.05 across ~110
    # runs, entirely from squeezing a 50ms clock this close to microsecond-scale
    # dispatch latency; nothing about the content delivery was ever wrong in any
    # of those runs (on_change fired exactly once, with the real value, every
    # time). A wide interval here, rather than a chase for a guarantee no
    # periodic-poll design can make against an async notifier, is what actually
    # matches production, where the real setting (5.0s) makes this collision
    # negligible.
    generous_interval_seconds = RECHECK_INTERVAL_SECONDS * 10
    _write(durable_tmp_path, "1000.0")
    changes: list = []
    overflows: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: overflows.append(True),
        recheck_interval_seconds=generous_interval_seconds,
    )
    watch.start()
    try:
        _write_by_rename(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not changes and time.monotonic() < deadline:
            time.sleep(0.02)
        # Give the backstop's own clock several ticks after the edit already
        # settled. Correctness here does not depend on timing: once _known is
        # in sync, every subsequent periodic pass finds zero diff regardless of
        # how many ticks land, so this cannot flake into a false pass.
        time.sleep(generous_interval_seconds * 2)
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
        _write_by_rename(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        # Several more backstop ticks after the retry succeeded: once delivered,
        # the identical diff must not surface a third time.
        time.sleep(RECHECK_INTERVAL_SECONDS * 5)

        assert len(attempts) == 2, "the change must be retried exactly once, not lost or duplicated"
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
        _write_raw_by_rename(path.parent, "[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        # Several more ticks over the same standing (now successfully reported)
        # rejection: it must not be reported a third time.
        time.sleep(RECHECK_INTERVAL_SECONDS * 5)

        assert len(attempts) == 2, "the rejection must be retried exactly once, not lost or repeated"
        assert watch._observer.is_alive(), "a raising on_rejection must not kill the observer thread"
        assert watch._periodic_thread.is_alive(), "a raising on_rejection must not kill the backstop thread"
    finally:
        watch.stop()


def test_a_raising_on_overflow_is_retried_without_duplicating_the_change(durable_tmp_path):
    # on_overflow and on_change answer different questions ("did the stream
    # lie?" vs "what changed?") and must be retried independently: a failure of
    # ONE must not cause the OTHER -- which already succeeded -- to be
    # redelivered. Pins the exact count on both sides, not just their content,
    # since a duplicate on_change here is a duplicate journal entry against
    # real capital.
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
    watch._recheck(is_periodic=True)  # on_overflow raises; on_change succeeds
    watch._recheck(is_periodic=True)  # retry: on_overflow succeeds; no new diff to report
    watch._recheck(is_periodic=True)  # a third pass: nothing outstanding on either side now

    assert len(overflow_attempts) == 2, "on_overflow must be retried until it stops raising"
    assert len(changes) == 1, "on_change must not be redelivered just because on_overflow failed"
    assert changes[0][0].new_value == "2500.0"


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
    assert len(changes) == 1, "the real change must be reported exactly once"
    assert changes[0][0].new_value == "2500.0"


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
        _write_raw_by_rename(path.parent, "[main_balance]\nvalue = [1, 2,\n")
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
        _write_by_rename(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while len(change_attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

        _write_raw_by_rename(path.parent, "[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not rejections and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(RECHECK_INTERVAL_SECONDS * 8)
    finally:
        watch.stop()

    assert len(change_attempts) == 2, "the change must be retried exactly once, not lost or duplicated"
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
        # Several more backstop ticks over the now-delivered new file: it must
        # not be reported a second time.
        time.sleep(RECHECK_INTERVAL_SECONDS * 5)
    finally:
        watch.stop()

    assert len(changes) == 1, "a settings file appearing after start() must be reported exactly once"
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
    watch.recheck_now()  # nothing changed on disk -- must not be reported a second time

    assert len(changes) == 1, "a newly discovered file's entries must be reported exactly once"
    assert changes[0][0] == SettingsChange(
        field="main_balance", old_value="", new_value="1000.0",
        observed_at_ns=changes[0][0].observed_at_ns,
    )


# ---------------------------------------------------------------------------
# Round 3: "unconsumed until delivered" must not collapse into "never
# delivered reads as already equal", and on_change/on_overflow must be
# retried independently rather than as one bundled unit.
# ---------------------------------------------------------------------------


def test_a_newly_discovered_files_content_is_retried_if_its_first_delivery_fails(durable_tmp_path):
    # The specific silence this closes: previous == candidate is not the same
    # state as "nothing has ever been delivered for this path". Falling back
    # to the current content when nothing has been delivered yet would make
    # the diff trivially empty and drop the file's real, still-present initial
    # content permanently after exactly one failed attempt.
    attempts: list = []

    def flaky_on_change(changes):
        attempts.append(changes)
        if len(attempts) == 1:
            raise RuntimeError("transient failure on the first delivery")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=flaky_on_change,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    _write(durable_tmp_path, "1000.0")  # a file appearing fresh; nothing else on disk changes below

    watch.recheck_now()  # first look: on_change raises, nothing consumed yet
    assert len(attempts) == 1
    assert attempts[0][0].new_value == "1000.0"

    watch.recheck_now()  # nothing on disk changed -- the same initial content must be retried
    assert len(attempts) == 2, "the file's initial content must be retried, not silently dropped"
    assert attempts[1][0].new_value == "1000.0", "the retry must be the real initial content"
    assert attempts[1][0].old_value == "", "the retry is still a change from nothing, not from itself"

    watch.recheck_now()  # a third look: already delivered, must not fire again
    assert len(attempts) == 2, "once delivered, the same content must not be redelivered"


def test_a_vanished_files_content_is_retried_if_its_first_removal_delivery_fails(durable_tmp_path):
    # The same fix, the other direction: a file present at construction (so
    # its content is already "delivered") that vanishes must have its removal
    # retried if on_change fails, using the correct baseline (what was
    # delivered), not silently drop the removal either.
    path = _write(durable_tmp_path, "1000.0")
    attempts: list = []

    def flaky_on_change(changes):
        attempts.append(changes)
        if len(attempts) == 1:
            raise RuntimeError("transient failure on the removal delivery")

    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=flaky_on_change,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    path.unlink()

    watch.recheck_now()  # first look: on_change raises, the removal is not yet consumed
    assert len(attempts) == 1
    assert attempts[0][0].old_value == "1000.0"
    assert attempts[0][0].new_value == ""

    watch.recheck_now()  # still gone -- the same removal must be retried, not dropped
    assert len(attempts) == 2, "the removal must be retried, not silently dropped"
    assert attempts[1][0].old_value == "1000.0"
    assert attempts[1][0].new_value == ""

    watch.recheck_now()  # already delivered -- nothing left to report
    assert len(attempts) == 2, "once the removal is delivered, it must not be redelivered"


def test_an_in_flight_mark_for_a_path_that_no_longer_exists_does_not_leak(durable_tmp_path):
    # A file created and deleted before the triggered recheck's own glob runs
    # is in neither the present-path loop nor the vanished-path loop, so
    # nothing would ever individually clear its mark. A guard that silently
    # disables the signal it protects is worse than the race it fixed.
    ghost = durable_tmp_path / "ghost.toml"
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=lambda changes: None,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
        recheck_interval_seconds=RECHECK_INTERVAL_SECONDS,
    )
    watch._mark_in_flight(ghost)
    assert watch._is_in_flight(ghost)
    watch.recheck_now()  # ghost is in neither current_paths nor self._known
    assert not watch._is_in_flight(ghost), "a mark for a path nobody ever revisits must not leak"
