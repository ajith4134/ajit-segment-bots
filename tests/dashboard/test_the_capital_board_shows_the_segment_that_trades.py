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

from dashboard.capital_settings_view import segment_scope_name, segment_settings_path
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


def test_every_editable_segment_setting_names_the_live_segment():
    """An allowlist keyed on a dead scope refuses every real edit."""
    segment = segment_scope_name()
    segment_keys = [key for key in editable_settings() if key[0] != "main-account"]

    assert segment_keys, "the segment's own capital must stay editable"
    assert all(key[0] == segment for key in segment_keys), segment_keys
