"""When it last changed, measured -- not mtime, not git log.

Section 15.3: mtime lies on a no-op save because vim rewrites the file on :wq;
git log is empty because RL-055's operator edits over SSH and never commits;
inotify can drop events, so an overflow forces a full re-read rather than reading
as nothing happened.
"""

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


def _write(directory, balance: str):
    path = directory / "main-account.toml"
    path.write_text(TEMPLATE.format(balance=balance))
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
def test_a_broken_edit_reports_a_rejection_and_does_not_report_a_change(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    rejections: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=rejections.append, on_overflow=lambda: None,
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
    # What an IN_Q_OVERFLOW triggers: re-establish ground truth rather than trust
    # a stream that admits it dropped something.
    _write(durable_tmp_path, "1000.0")
    seen: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
    )
    _write(durable_tmp_path, "2500.0")
    watch.recheck_now()
    assert seen and seen[0][0].new_value == "2500.0"
