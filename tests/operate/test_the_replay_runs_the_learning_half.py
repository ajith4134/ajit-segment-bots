"""The replay decodes a closed trade, and does it on the tape's clock.

Until 2026-09-06 `operate/replay_a_captured_session.py` imported seven parts and
stopped at `position-close-detector`. The 2026-09-04 replay closed 252 trades
and not one reached `pnl-attributor`, so everything downstream of `closed-trade`
had never run on a real trade -- which is why `edge-graduation-gate` reads
`judgements 0` on the live spine and `bot-maturity` has never been produced.

Running it exposed a defect the trading half could never have shown: both
`paper-fill-simulator` and `position-close-detector` stamp with `now_ns()`,
which in a replay was the wall clock of the machine running it. A forty-minute
round trip was recorded as held for milliseconds. Net PnL does not depend on the
clock so nothing noticed, but `luck-skill-separator` scales a symbol's
volatility to the horizon actually held, and it reported outcomes of **-1,958
and -5,689 standard deviations** where the honest figures are -6.9 and -9.2.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPLAY_SOURCE = pathlib.Path(__file__).resolve().parents[2] / (
    "operate/replay_a_captured_session.py"
)


def load_replay():
    spec = importlib.util.spec_from_file_location("replay_a_captured_session", REPLAY_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def replay():
    return load_replay()


# ---- TapeClock ---------------------------------------------------------------

def test_the_clock_reads_the_print_it_was_advanced_to(replay):
    clock = replay.TapeClock(1_000)
    assert clock() == 1_000
    clock.advance_to(5_000)
    assert clock() == 5_000


def test_the_clock_never_goes_backwards(replay):
    """The tape is walked in order, and a clock that went back would make a
    holding period negative -- a trade that closed before it opened."""
    clock = replay.TapeClock(5_000)
    clock.advance_to(1_000)
    assert clock() == 5_000


def test_a_replayed_holding_period_is_market_time_not_wall_time(replay):
    """The defect this closes. A chain given the tape's clock reports the
    captured seconds between entry and exit; one given the wall clock reports
    how long the replay itself took."""
    entry_ns = 1_788_498_230_357_000_000
    thirty_minutes_later = entry_ns + 1_800 * 10**9
    clock = replay.TapeClock(entry_ns)
    clock.advance_to(thirty_minutes_later)
    assert (clock() - entry_ns) / 1e9 == pytest.approx(1_800.0)


# ---- the two figures measured from the tape ----------------------------------

def test_daily_volatility_is_measured_in_return_space(replay):
    """`luck-skill-separator` divides `realised_pnl / notional` by this, so it is
    a fraction and never rupees. A 1% wobble between prints on a minute cadence
    is a large daily figure, not a small one."""
    prints = [(i * 60 * 10**9, 100.0 * (1.01 if i % 2 else 1.0)) for i in range(60)]
    volatility = replay.daily_volatility_of(prints)
    assert volatility is not None
    assert 0.0 < volatility < 10.0


def test_a_contract_with_too_few_prints_has_no_measurable_volatility(replay):
    """None rather than zero: the separator reports "this symbol's volatility has
    never been measured", which is a different fact from a symbol that does not
    move."""
    assert replay.daily_volatility_of([(0, 100.0), (1, 100.0)]) is None
    assert replay.daily_volatility_of([]) is None


def test_volatility_is_none_when_no_time_passed(replay):
    """Every print at the same instant scales by nothing; a divide would either
    raise or invent a figure."""
    assert replay.daily_volatility_of([(0, 100.0), (0, 101.0), (0, 102.0)]) is None


def test_typical_movement_is_the_contracts_own_median_move(replay):
    """What entry-quality-scorer measures chasing against, so a thin contract is
    judged against itself rather than against a busy one."""
    prints = [(0, 100.0), (1, 101.0), (2, 103.0), (3, 104.0)]
    assert replay.typical_movement_of(prints) == pytest.approx(1.0)


def test_typical_movement_ignores_prints_that_did_not_move(replay):
    assert replay.typical_movement_of([(0, 100.0), (1, 100.0)]) is None


# ---- what the replay states rather than measures ------------------------------

def test_the_replay_names_what_it_could_not_measure(replay):
    """It opens at the first print by construction, so no detector fired and no
    bot stated a conviction. Both are named rather than left to read as real."""
    assert "replay" in replay.THE_REPLAY_HAS_NO_DETECTOR
    assert "replay" in replay.THE_REPLAY_HAS_NO_REGIME


def test_the_learning_replay_says_which_part_it_refuses_to_run(replay):
    """edge-graduation-gate needs a decision quality, a refutation verdict, a
    trial verdict and a coverage report. A replay produces none of the four, and
    inventing them would call a fabrication a graduation."""
    assert "edge-graduation-gate" in replay.LearningReplay.__doc__
    assert "deliberately not run" in replay.LearningReplay.__doc__


# ---- derived segment membership (2026-09-12) ---------------------------------
#
# Both option segments stopped stating their underlyings that day and started
# deriving them from the broker's master. `segment_that_trades` answers None for
# a derived segment it is given no membership for -- honestly, since it holds no
# master -- so the replay has to supply one per derived segment or it finds that
# nobody owns anything. It did exactly that for one run: "Segments that opened
# AND closed a trade: 0 of 2", with every instrument counted as unowned and no
# error anywhere. That is the silent-wrong-answer shape this project keeps
# finding, and it is what these guard.

def test_every_derived_segment_gets_a_membership_set(replay):
    """A derived segment absent from this map owns nothing, silently."""
    master = replay.instruments_by_key()
    membership = replay.derived_membership_for(master)

    assert {"index-options", "stock-options", "cash-equity-intraday"} <= set(membership)
    for segment, symbols in membership.items():
        assert symbols, f"{segment} derived an empty universe, which owns nothing"


def test_the_derived_option_universes_match_the_bridges_own_rules(replay):
    """Reused from broker-symbol-universe-bridge, never restated (T-6).

    A replay that disagreed with the live spine about who owns an instrument is
    a replay that cannot verify the live spine.
    """
    master = replay.instruments_by_key()
    membership = replay.derived_membership_for(master)

    indices = membership["index-options"]
    stocks = membership["stock-options"]

    # Measured on the real master 2026-09-12.
    assert indices == {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
        "NIFTYFPI", "FOCIT", "SENSEX", "BANKEX", "SENSEX50",
    }
    assert len(stocks) > 150, "the F&O stock list is 210 names, not a handful"
    assert {"RELIANCE", "HDFCBANK", "MARUTI", "TATASTEEL"} <= stocks
    assert not indices & stocks, "no underlying may be owned by both segments"


def test_an_underlying_with_no_option_written_on_it_is_owned_by_neither(replay):
    """The half of each rule that needs the whole master.

    2,655 ordinary shares are listed and 210 carry options; admitting on "is an
    ordinary share" alone would hand 2,445 chainless names to an options segment.
    """
    master = replay.instruments_by_key()
    membership = replay.derived_membership_for(master)

    owned_by_options = membership["index-options"] | membership["stock-options"]
    cash = membership["cash-equity-intraday"]

    assert not owned_by_options & cash, (
        "the option rules and the cash rule are complements; an overlap is "
        "double exposure nothing bounds"
    )
    assert len(cash) > len(owned_by_options), (
        "far more shares carry no derivative than carry one"
    )
