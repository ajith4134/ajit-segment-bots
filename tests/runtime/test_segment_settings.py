"""A segment's universe comes from the segment's own file.

The defect this prevents has no symptom: `segment_id` decided which capital
file was read while the tracked underlyings stayed in machine scope, so
pointing the spine at `stock-options` would have taken the stock segment's
money and traded the index chains -- every part healthy, every counter moving,
the wrong instruments.
"""

import pathlib

import pytest

from runtime.segment_settings import (
    SegmentSettingMissing, option_contracts_per_underlying, read_segment_setting,
    read_segment_symbols, segment_settings_path, underlyings_this_segment_trades,
)

A_SEGMENT = """
[segment_underlying_trading_symbols]
value = ["RELIANCE", "TCS"]
unit  = "trading symbol"
note  = "a test's own segment"

[segment_option_contracts_per_underlying]
value = 20
unit  = "option contracts per underlying per expiry"
note  = "a test's own segment"
"""

NO_UNIVERSE = """
[money_mode]
value = "paper"
unit  = "paper or live"
note  = "a segment written before universes moved here"
"""

EMPTY_UNIVERSE = """
[segment_underlying_trading_symbols]
value = []
unit  = "trading symbol"
note  = "a segment that scans nothing"
"""


@pytest.fixture
def settings_root(tmp_path):
    (tmp_path / "segments").mkdir()
    (tmp_path / "segments" / "stock-options.toml").write_text(A_SEGMENT)
    (tmp_path / "segments" / "old-shape.toml").write_text(NO_UNIVERSE)
    (tmp_path / "segments" / "empty.toml").write_text(EMPTY_UNIVERSE)
    return tmp_path


class _Entry:
    def __init__(self, value):
        self.value = value


class _Context:
    """Only what these helpers read off a real part context."""

    def __init__(self, segment_id, machine):
        self._machine = dict(machine, segment_id=segment_id)

    def setting(self, name):
        return _Entry(self._machine[name])

    def number(self, name):
        return self._machine[name]


def test_a_segment_states_its_own_universe(settings_root):
    assert read_segment_symbols(
        "stock-options", "segment_underlying_trading_symbols", settings_root,
    ) == ("RELIANCE", "TCS")
    assert int(read_segment_setting(
        "stock-options", "segment_option_contracts_per_underlying", settings_root,
    ).value) == 20


def test_a_segment_that_names_no_universe_is_refused_by_name(settings_root):
    with pytest.raises(SegmentSettingMissing) as refusal:
        read_segment_symbols(
            "old-shape", "segment_underlying_trading_symbols", settings_root,
        )
    assert "old-shape" in str(refusal.value)
    assert str(segment_settings_path("old-shape", settings_root)) in str(refusal.value)


def test_an_empty_universe_is_refused_rather_than_returned(settings_root):
    """A part handed no symbols scans nothing while every skip counter reads
    zero, which is indistinguishable from working and finding nothing."""
    with pytest.raises(SegmentSettingMissing) as refusal:
        read_segment_symbols("empty", "segment_underlying_trading_symbols", settings_root)
    assert "scans nothing" in str(refusal.value)


def test_a_segment_with_no_file_at_all_is_refused(settings_root):
    with pytest.raises(Exception):
        read_segment_symbols(
            "no-such-segment", "segment_underlying_trading_symbols", settings_root,
        )


# ---- what a part actually calls ---------------------------------------------


def test_a_part_falls_back_to_machine_scope_when_the_segment_says_nothing(monkeypatch):
    """The narrow fallback: a segment file written before universes moved here
    keeps running exactly as it ran yesterday, rather than making a settings
    edit a condition of the spine starting."""
    import runtime.segment_settings as module

    monkeypatch.setattr(
        module, "read_segment_symbols",
        lambda *a, **k: (_ for _ in ()).throw(SegmentSettingMissing("none stated")),
    )
    context = _Context(
        "index-options",
        {"underlying_price_bridge_index_trading_symbols": ["NIFTY", "BANKNIFTY"]},
    )

    assert underlyings_this_segment_trades(context) == ("NIFTY", "BANKNIFTY")


def test_the_segment_wins_over_machine_scope(monkeypatch):
    import runtime.segment_settings as module

    monkeypatch.setattr(module, "read_segment_symbols", lambda *a, **k: ("RELIANCE",))
    context = _Context(
        "stock-options",
        {"underlying_price_bridge_index_trading_symbols": ["NIFTY"]},
    )

    assert underlyings_this_segment_trades(context) == ("RELIANCE",)


def test_the_chain_width_falls_back_the_same_way(monkeypatch):
    import runtime.segment_settings as module

    monkeypatch.setattr(
        module, "read_segment_setting",
        lambda *a, **k: (_ for _ in ()).throw(SegmentSettingMissing("none stated")),
    )
    context = _Context(
        "index-options", {"symbol_universe_option_contracts_per_underlying": 50},
    )

    assert option_contracts_per_underlying(context) == 50


def test_the_operator_s_own_two_segments_each_state_a_universe():
    """The real files on this machine, not a fixture: index-options must keep
    trading the three indices and stock-options must not inherit them."""
    from runtime.settings_reader import settings_directory

    root = settings_directory()
    if not segment_settings_path("stock-options", root).exists():
        pytest.skip("this machine has no stock-options segment file")

    index = read_segment_symbols("index-options", "segment_underlying_trading_symbols", root)
    stock = read_segment_symbols("stock-options", "segment_underlying_trading_symbols", root)

    assert index == ("NIFTY", "BANKNIFTY", "SENSEX")
    assert "NIFTY" not in stock and "SENSEX" not in stock
    assert "RELIANCE" in stock
    assert not set(index) & set(stock), "the two segments must not share an underlying"
