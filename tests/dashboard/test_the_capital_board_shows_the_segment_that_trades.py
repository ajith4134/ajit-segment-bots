"""The capital board must show the segment that is trading, not a retired one.

`capital_settings_view` and `capital_settings_writer` both hardcoded the scope
`"futures"` -> `segments/futures.toml`. That was correct while futures was the
build order (RL-050). It stopped being correct on 2026-09-02, when `segment_id`
became `index-options` and `segments/index-options.toml` was created --
`segments/futures.toml`'s own note in the settings directory says it is "the
retired crypto segment's file, left in place as history, not read by anything
now".

Measured against the live board on 2026-09-04, it was read by something. The
capital settings view served two scopes -- `main-account` and `futures` --
showing `allocated_balance` 1,000 USDT and `maximum_capital_per_trade` 50 USDT
from the retired segment, and **not showing `index-options` at all**: not the
500,000 INR the bot actually sizes against.

The writer is the more serious half. Its `SCOPE_FILES` pointed the same way, so
an operator editing capital from the board would have written into a file no
running part reads -- the change accepted, the confirmation shown, and the
number the bot sizes trades against untouched.

The segment is `segment_id`'s to name, which is what every per-segment part
already reads to find its own file.
"""

from __future__ import annotations

import pytest

from dashboard.capital_settings_view import (
    build_capital_settings_view,
    built_segment_names,
    segment_scope_name,
    segment_settings_path,
)
from dashboard.capital_settings_writer import scope_files, editable_settings


def test_the_segment_scope_is_read_from_segment_id_not_hardcoded():
    assert segment_scope_name() == "index-options"


def test_the_segment_file_is_the_one_the_parts_read():
    assert segment_settings_path().endswith("segments/index-options.toml")


def test_the_retired_crypto_segment_is_not_a_scope_any_more():
    assert "futures" not in scope_files()
    assert segment_scope_name() != "futures"


def test_the_writer_writes_to_the_segment_that_trades():
    files = scope_files()
    assert files[segment_scope_name()].endswith("segments/index-options.toml")


def test_every_editable_segment_setting_names_a_built_segment():
    """An allowlist keyed on a dead scope refuses every real edit."""
    built = set(built_segment_names())
    segment_keys = [key for key in editable_settings() if key[0] != "main-account"]

    assert segment_keys, "the segment's own capital must stay editable"
    assert all(key[0] in built for key in segment_keys), segment_keys


# ---- every built segment, not only the one segment_id names -----------------
#
# Three segment bots have run on one spine since 2026-09-05, and this board went
# on serving `segment_id` alone. The two it left out were not unbuilt: both
# carried a full set of capital settings and `capital-allotment-reader` was
# already publishing an allotment for each. They were invisible and uneditable
# from the only place RL-051 says these are edited.


def test_the_board_shows_every_segment_the_spine_trades():
    scopes = {scope["scope"] for scope in build_capital_settings_view()["scopes"]}

    assert "main-account" in scopes
    for segment in built_segment_names():
        assert segment in scopes, f"{segment} is built and the board does not show it"


def test_the_writer_can_reach_every_built_segments_file():
    files = scope_files()

    for segment in built_segment_names():
        assert files[segment].endswith(f"segments/{segment}.toml")


def test_the_board_says_which_segment_stands_in_for_the_machine():
    """`segment_id` is one of several now, so which one it is has to be said."""
    scopes = build_capital_settings_view()["scopes"]
    marked = [scope["scope"] for scope in scopes if scope["is_the_standing_in_segment"]]

    assert marked == [segment_scope_name()]


def a_built_segments_value_of(monkeypatch, listed):
    """Drive `built_segment_names` against a given value, without a real file."""
    import runtime.settings_reader as reader
    from dashboard.capital_settings_view import built_segment_names as named

    entry = type("Entry", (), {"value": listed})()
    document = type("Document", (), {"entries": {"built_segments": entry}})()
    monkeypatch.setattr(reader, "load_settings_document", lambda *a, **k: document)
    return named


@pytest.mark.parametrize("listed", ["index-options", "", 3, {}])
def test_a_malformed_built_segments_refuses_rather_than_narrowing_to_one(monkeypatch, listed):
    """Silently reading three segments' capital from one file is the failure being fixed.

    A string is the case that matters: it is iterable, so a version of this that
    only checked truthiness would turn "index-options" into thirteen
    one-character segments and look up `segments/i.toml`.
    """
    named = a_built_segments_value_of(monkeypatch, listed)

    with pytest.raises(ValueError):
        named()


def test_a_list_of_one_segment_is_a_list_and_not_an_error(monkeypatch):
    """A spine trading one segment states a one-item list, not a different shape."""
    named = a_built_segments_value_of(monkeypatch, ["index-options"])

    assert named() == ("index-options",)
