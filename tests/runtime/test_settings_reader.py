"""Settings are the named half of RL-061 and the operator's only control surface.

RL-055: capital settings are edited in a file on the server over SSH, and the board
shows the current values and when they last changed. So the reader must survive a
human editing it badly at 2am -- a broken save keeps the last value that parsed and
says so, because under T-3 'off' is the governor's decision and never a part's
reaction to its input.
"""

import pathlib
import textwrap

import pytest

from runtime.settings_reader import (
    LastKnownGoodSettings,
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

GOOD = textwrap.dedent(
    """
    # ajit-segment-bots runtime settings.
    # No API keys or secrets belong here -- see docs/secrets.md.

    [writeback_interval]
    value = 8388608
    unit  = "bytes"
    note  = "operator, 2026-08-20: a stream writer forces writeback and drops its cache every 8 MiB; measured to hold a 500 MB write at a 16 MB peak"

    [placement_confirmation_deadline]
    value = 0.5
    unit  = "seconds"
    note  = "operator, 2026-08-20: placement median 5.7 ms, p95 6.9 ms; this is 70x the p95"
    """
).strip()

BROKEN = "[writeback_interval]\nvalue = [1, 2,\n"


def _write(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = directory / name
    path.write_text(body)
    return path


def test_loads_value_unit_and_note_for_each_entry(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    assert document.read_value("writeback_interval") == 8388608
    entry = document.read_entry("placement_confirmation_deadline")
    assert entry.unit == "seconds"
    assert entry.note.startswith("operator, 2026-08-20")


def test_records_where_it_came_from_and_a_digest_of_what_it_read(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    assert document.source_path == path
    assert len(document.content_digest) == 64
    assert document.parsed_at_ns > 0


def test_refuses_an_entry_with_no_provenance(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", "[orphan]\nvalue = 1\nunit = \"bytes\"\n")
    with pytest.raises(SettingsParseRefused) as refusal:
        load_settings_document(path, scope="runtime")
    assert "note" in str(refusal.value)


def test_refuses_a_bare_value_that_is_not_an_entry_table(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", "writeback_interval = 8388608\n")
    with pytest.raises(SettingsParseRefused):
        load_settings_document(path, scope="runtime")


def test_a_broken_edit_keeps_the_last_value_that_parsed_and_reports_the_rejection(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    assert settings.offer_candidate() is None
    assert settings.current.read_value("writeback_interval") == 8388608

    path.write_text(BROKEN)
    rejection = settings.offer_candidate()

    assert rejection is not None
    assert rejection.source_path == path
    assert "Invalid" in rejection.reason or "invalid" in rejection.reason
    assert settings.current.read_value("writeback_interval") == 8388608


def test_a_repaired_edit_is_accepted_after_a_rejection(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    settings.offer_candidate()
    path.write_text(BROKEN)
    assert settings.offer_candidate() is not None
    path.write_text(GOOD.replace("value = 8388608", "value = 4194304"))
    assert settings.offer_candidate() is None
    assert settings.current.read_value("writeback_interval") == 4194304


def test_a_no_op_save_is_not_reported_as_a_change(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    settings.offer_candidate()
    first_digest = settings.current.content_digest
    path.write_text(GOOD)  # what vim does on :wq with nothing changed
    assert settings.offer_candidate() is None
    assert settings.current.content_digest == first_digest


def test_reading_an_undeclared_name_refuses_rather_than_returning_a_default(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    with pytest.raises(KeyError):
        document.read_value("a_number_nobody_declared")


def test_the_settings_directory_is_under_config_and_not_in_the_repository():
    directory = settings_directory()
    assert directory.parts[-3:] == (".config", "ajit-segment-bots", "settings")
    assert "ajit-segment-bots/docs" not in str(directory)
