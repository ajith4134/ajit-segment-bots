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


# ---- three segments on one spine (2026-09-05) --------------------------------
#
# docs/proposals/three-segments-on-one-spine.md. The segment stops being a
# property of the spine and becomes a property of the instrument, because three
# processes per part cannot exist: the longest inbox address is already 106 of
# the 107 bytes sun_path allows.

from runtime.segment_settings import (  # noqa: E402
    SegmentsOverlap, built_segments, instrument_types_this_segment_trades,
    segment_that_trades, underlyings_every_built_segment_trades,
)

THREE_SEGMENTS = {
    "index-options": ('["option"]', '["NIFTY", "BANKNIFTY"]'),
    "stock-options": ('["option"]', '["RELIANCE", "TCS"]'),
    "cash-equity-intraday": ('["spot"]', '["RELIANCE", "TCS"]'),
}


@pytest.fixture
def three_segments(tmp_path):
    (tmp_path / "segments").mkdir()
    for segment, (types, symbols) in THREE_SEGMENTS.items():
        (tmp_path / "segments" / f"{segment}.toml").write_text(
            f'[segment_instrument_types]\nvalue = {types}\nunit = "instrument type"\n'
            f'note = "a test\'s own segment"\n\n'
            f'[segment_underlying_trading_symbols]\nvalue = {symbols}\n'
            f'unit = "trading symbol"\nnote = "a test\'s own segment"\n'
        )
    return tmp_path


class _MultiSegmentContext(_Context):
    """A context whose machine scope names the built segments."""

    def __init__(self, segment_id, built, machine=None):
        super().__init__(segment_id, machine or {})
        if built is not None:
            self._machine["built_segments"] = built

    def setting(self, name):
        if name not in self._machine:
            raise KeyError(name)
        return _Entry(self._machine[name])


def test_the_spine_states_which_segments_it_trades():
    context = _MultiSegmentContext(
        "index-options", ["index-options", "stock-options", "cash-equity-intraday"],
    )
    assert built_segments(context) == (
        "index-options", "stock-options", "cash-equity-intraday",
    )


def test_a_spine_that_names_no_list_still_trades_its_one_segment():
    """The narrow fallback: runtime.toml before 2026-09-05 named no list, and a
    one-segment spine must start without a settings edit."""
    context = _MultiSegmentContext("index-options", None)
    assert built_segments(context) == ("index-options",)


def test_a_segment_listed_twice_is_counted_once():
    context = _MultiSegmentContext(
        "index-options", ["index-options", "stock-options", "index-options"],
    )
    assert built_segments(context) == ("index-options", "stock-options")


def test_the_feed_subscribes_each_underlying_once_however_many_segments_want_it(
    three_segments,
):
    """RELIANCE is a stock-options underlying and a cash-equity-intraday one.
    Subscribing twice spends the broker's instrument budget on a duplicate."""
    context = _MultiSegmentContext("index-options", list(THREE_SEGMENTS))

    union = underlyings_every_built_segment_trades(context, three_segments)

    assert union == ("NIFTY", "BANKNIFTY", "RELIANCE", "TCS")
    assert len(union) == len(set(union))


def test_an_instrument_belongs_to_the_segment_that_claims_its_type_and_underlying(
    three_segments,
):
    context = _MultiSegmentContext("index-options", list(THREE_SEGMENTS))

    assert segment_that_trades("option", "NIFTY", context, three_segments) == "index-options"
    assert segment_that_trades("option", "RELIANCE", context, three_segments) == "stock-options"
    assert segment_that_trades("spot", "RELIANCE", context, three_segments) == "cash-equity-intraday"


def test_an_instrument_no_built_segment_claims_is_none_not_the_nearest_segment(
    three_segments,
):
    """None is what makes instrument-selector report an unbuilt segment. Picking
    the nearest one would buy an instrument with another segment's money."""
    context = _MultiSegmentContext("index-options", list(THREE_SEGMENTS))

    assert segment_that_trades("option", "WIPRO", context, three_segments) is None
    assert segment_that_trades("dated-future", "NIFTY", context, three_segments) is None
    assert segment_that_trades("spot", "NIFTY", context, three_segments) is None


def test_two_segments_claiming_one_instrument_is_refused_not_resolved(tmp_path):
    """Whose capital buys it is undecidable, and picking one is a wrong answer
    that reports nothing."""
    (tmp_path / "segments").mkdir()
    for segment in ("one", "two"):
        (tmp_path / "segments" / f"{segment}.toml").write_text(
            '[segment_instrument_types]\nvalue = ["option"]\nunit = "instrument type"\n'
            'note = "a test"\n\n[segment_underlying_trading_symbols]\n'
            'value = ["NIFTY"]\nunit = "trading symbol"\nnote = "a test"\n'
        )
    context = _MultiSegmentContext("one", ["one", "two"])

    with pytest.raises(SegmentsOverlap) as refusal:
        segment_that_trades("option", "NIFTY", context, tmp_path)

    assert "one" in str(refusal.value) and "two" in str(refusal.value)


def test_a_segment_stating_no_instrument_type_is_refused_by_name(three_segments):
    (three_segments / "segments" / "typeless.toml").write_text(
        '[segment_underlying_trading_symbols]\nvalue = ["NIFTY"]\n'
        'unit = "trading symbol"\nnote = "a test"\n'
    )
    with pytest.raises(SegmentSettingMissing) as refusal:
        instrument_types_this_segment_trades("typeless", three_segments)
    assert "typeless" in str(refusal.value)


def test_the_operator_s_three_segments_claim_disjoint_instruments():
    """The real files on this machine: the three bots of the 2026-09-05
    temporary goal must not contend for one instrument."""
    from runtime.settings_reader import settings_directory

    root = settings_directory()
    segments = ("index-options", "stock-options", "cash-equity-intraday")
    if not all(segment_settings_path(s, root).exists() for s in segments):
        pytest.skip("this machine does not have all three segment files")

    claimed = {}
    for segment in segments:
        for instrument_type in instrument_types_this_segment_trades(segment, root):
            for symbol in read_segment_symbols(
                segment, "segment_underlying_trading_symbols", root,
            ):
                key = (instrument_type, symbol)
                assert key not in claimed, (
                    f"{key} is claimed by both {claimed.get(key)} and {segment}"
                )
                claimed[key] = segment

    assert len(claimed) == 3 + 14 + 14
