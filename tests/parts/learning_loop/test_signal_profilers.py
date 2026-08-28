"""The two profiles an exit plan cannot be built without, measured from live claims.

**Every label these parts read is produced by the real labeller**, from claims
placed on real captured BTCUSDT trades (RL-063). Nothing here hand-writes a
`TrainingLabel`: the numbers under test are the excursions the labeller measured
tick by tick, and a fixture that invented them would be testing arithmetic on
numbers this system never produces.

The properties that matter most are the two refusals. A profile that reported
itself fitted on three claims would place a stop from three observations wearing
the authority of a distribution, and `bull-exit-plan-proposer` would accept it. A
profile built from claims that came *wrong* would measure how far price runs when a
call was bad -- which is unbounded, and would put every stop far enough away to
guarantee the loss is large when it arrives.
"""

from __future__ import annotations

import pytest

from parts.learning_loop.signal_excursion_profiler import (
    PART_DECLARATION as EXCURSION_DECLARATION,
    PART_ID as EXCURSION_PART_ID,
    REFUSED_NO_DIRECTION,
    REFUSED_NO_SETUP_VERDICT,
    SignalExcursionProfiler,
    describe_excursion_profiling,
)
from parts.learning_loop.signal_horizon_profiler import (
    PART_DECLARATION as HORIZON_DECLARATION,
    PART_ID as HORIZON_PART_ID,
    REFUSED_NOT_A_DURATION,
    SignalHorizonProfiler,
    describe_horizon_profiling,
)
from parts.learning_loop.signal_outcome_labeller import SignalOutcomeLabeller
from runtime.learned_estimator import Estimate
from runtime.learning_types import THE_SETUP_WAS_RIGHT, TrainingLabel
from runtime.market_signal import LONG, REVERSION, SHORT, make_candidate
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trade_profiles import FROM_SETTLED_CLAIMS

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
DETECTOR = "spread-reversion-detector"
# What the detector named above calibrates on, carried on the claim so the label
# can be routed back to the estimator that made it. Six of the nine detectors key
# on the market regime and three do not -- the sweeper on its condition, the whale
# reader on flow direction, the sentiment reader on the relationship it found --
# which is why the key travels with the candidate rather than being re-derived
# from the regime at settlement.
CALIBRATION_KEY = "reverting"
# The captured run spans 0.133% of price over 28 seconds, so a barrier has to sit
# well inside that for enough claims to settle to take a quantile of. At two basis
# points -- what the labeller's own tests use -- the run settles eight, which is
# fewer than any distribution should be judged on. Half a basis point settles
# dozens of the same real moves. The shipped 0.002 is measured against the tape
# instead: measurements/2026-08-22-vertical-numbers/README.md.
MOVE_FRACTION = 0.00005
HORIZON_SECONDS = 60.0
NANOSECONDS = 1_000_000_000

WINDOW = 300
MINIMUM_CLAIMS = 5  # low enough that this 28-second fixture can reach it
ADVERSE_QUANTILE = 0.85
FAVOURABLE_QUANTILES = (0.5, 0.75)


class Clock:
    def __init__(self, at_ns: int = 1_787_000_000_000_000_000) -> None:
        self.at_ns = at_ns

    def __call__(self) -> int:
        return self.at_ns

    def advance_seconds(self, seconds: float) -> None:
        self.at_ns += int(seconds * NANOSECONDS)


@pytest.fixture(scope="module")
def real_prices(read_captured_payloads):
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, "2026-08-22-btcusdt-aggtrade-run.jsonl")
    prices = [trade.price for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(prices) > 500, "the captured run must hold enough prices to settle claims"
    return prices


def an_untested_confidence() -> Estimate:
    return Estimate(
        value=0.5, is_fitted=False, observations=0, prior=0.5, was_clamped=False,
        bound_low=None, bound_high=None, reason="no outcomes observed yet",
    )


def a_claim(direction: str = LONG, detector: str = DETECTOR):
    return make_candidate(
        detector=detector, venue_id=VENUE, symbol=SYMBOL, direction=direction,
        expectation=REVERSION, signal_strength=2.5, confidence=an_untested_confidence(),
        horizon_seconds=HORIZON_SECONDS, evidence={"spread_z": 2.5},
        reason="the spread is stretched", calibration_key=CALIBRATION_KEY,
    )


def labels_from_real_prices(prices, direction=LONG, detector=DETECTOR, wanted=40):
    """Run the real labeller over real prices and collect the labels it produced.

    One claim at a time, because the labeller allows one open claim per detector
    per symbol -- which is itself correct, and is why a run of claims has to be
    walked rather than opened all at once.
    """
    clock = Clock()
    labeller = SignalOutcomeLabeller(
        move_fraction=MOVE_FRACTION, maximum_open_claims=100, now_ns=clock
    )
    collected = []
    labeller.observe_price(VENUE, SYMBOL, prices[0], clock.at_ns)
    labeller.observe_candidate(a_claim(direction, detector))
    for price in prices[1:]:
        clock.advance_seconds(0.1)
        labeller.observe_price(VENUE, SYMBOL, price, clock.at_ns)
        collected.extend(labeller.resolve_settled_claims())
        if len(collected) >= wanted:
            break
        if not labeller.open_claims:
            labeller.observe_candidate(a_claim(direction, detector))
    return collected


@pytest.fixture(scope="module")
def real_labels(real_prices):
    labels = labels_from_real_prices(real_prices)
    assert len(labels) >= 10, (
        f"the captured run settled only {len(labels)} claim(s); the profiles below would be "
        f"taken over a sample too small to judge them on"
    )
    return labels


def an_excursion_profiler(minimum_claims: int = MINIMUM_CLAIMS) -> SignalExcursionProfiler:
    return SignalExcursionProfiler(
        window=WINDOW, minimum_claims=minimum_claims,
        adverse_quantile=ADVERSE_QUANTILE, favourable_quantiles=FAVOURABLE_QUANTILES,
    )


# ---- the declarations match the blueprint ------------------------------------

@pytest.mark.parametrize(
    "part_id, declaration",
    [(EXCURSION_PART_ID, EXCURSION_DECLARATION), (HORIZON_PART_ID, HORIZON_DECLARATION)],
)
def test_every_built_declaration_equals_the_blueprint(part_id, declaration):
    assert declaration == load_declaration_from_blueprint(part_id)


# ---- signal-excursion-profiler -----------------------------------------------

def test_the_labeller_reports_the_excursions_it_measured(real_labels):
    """The whole mechanism in one assertion: the numbers reach the label.

    They were computed on every tick and then discarded, which is exactly why the
    system could not place a stop before its first trade had closed.
    """
    assert all(label.direction == LONG for label in real_labels)
    assert any(label.best_favourable_fraction > 0 for label in real_labels)
    assert any(label.worst_adverse_fraction < 0 for label in real_labels)


def test_a_profile_from_real_claims_places_a_stop_beyond_what_a_winner_survived(real_labels):
    profiler = an_excursion_profiler()
    for label in real_labels:
        profiler.observe_label(label)

    profile = profiler.profile(VENUE, SYMBOL, LONG)
    if not profile.is_fitted:
        pytest.skip(
            f"only {profile.trades_observed} of the captured claims came right; the quantile "
            f"below needs {MINIMUM_CLAIMS}"
        )
    assert profile.adverse_excursion > 0, (
        "a stop at the entry price is not a stop; a correct call still moves against you first"
    )
    assert profile.source == FROM_SETTLED_CLAIMS
    assert "closed trades, which do not exist yet" in profile.reason
    # Every adverse excursion the profile was built from is a real one this symbol
    # actually made, so the quantile cannot exceed the worst of them.
    worst = max(
        abs(min(0.0, label.worst_adverse_fraction))
        for label in real_labels
        if label.label_for(THE_SETUP_WAS_RIGHT)
    )
    assert profile.adverse_excursion <= worst + 1e-12


def test_targets_are_priced_only_where_the_symbol_actually_reached(real_labels):
    profiler = an_excursion_profiler()
    for label in real_labels:
        profiler.observe_label(label)
    profile = profiler.profile(VENUE, SYMBOL, LONG)
    if not profile.is_fitted:
        pytest.skip("too few claims came right in the captured run")
    assert set(profile.favourable_quantiles) <= set(FAVOURABLE_QUANTILES)
    assert all(reached > 0 for reached in profile.favourable_quantiles.values())


def test_a_profile_that_has_seen_too_little_says_so_instead_of_guessing(real_labels):
    """Rule 8, inside a part: an unfitted profile is published, not hidden.

    Published rather than withheld because the proposer refuses an unfitted
    profile by name, and silence would look identical to a part that had died.
    """
    profiler = an_excursion_profiler(minimum_claims=len(real_labels) + 100)
    for label in real_labels:
        profiler.observe_label(label)
    profile = profiler.profile(VENUE, SYMBOL, LONG)
    assert profile.is_fitted is False
    assert profile.adverse_excursion == 0.0
    assert profile.favourable_quantiles == {}
    assert "no stop may be placed from it" in profile.reason


def test_claims_that_came_wrong_are_counted_but_never_measured(real_labels):
    """How far price runs when the call was bad is unbounded.

    A stop placed beyond it would make every loss as large as the worst one ever
    seen, which is the opposite of what a stop is for.
    """
    profiler = an_excursion_profiler()
    wrong = [
        TrainingLabel(
            venue_id=VENUE, symbol=SYMBOL, detector=DETECTOR, regime="any",
            labels={THE_SETUP_WAS_RIGHT: False}, horizon_seconds=HORIZON_SECONDS,
            seconds_to_resolve=3.0, resolved_within_horizon=True, features={},
            built_at_ns=1, claimed_at_ns=0, direction=LONG,
            best_favourable_fraction=0.0, worst_adverse_fraction=-0.9,
        )
    ]
    for label in real_labels + wrong:
        profiler.observe_label(label)
    assert profiler.standing.claims_that_came_wrong >= 1
    profile = profiler.profile(VENUE, SYMBOL, LONG)
    if profile.is_fitted:
        assert profile.adverse_excursion < 0.9, (
            "a 90% adverse excursion from a claim that was simply wrong must not reach the stop"
        )


def test_a_long_and_a_short_are_profiled_separately(real_prices):
    """An excursion means the opposite thing for each, and averaging describes neither."""
    profiler = an_excursion_profiler(minimum_claims=2)
    for direction in (LONG, SHORT):
        for label in labels_from_real_prices(real_prices, direction=direction, wanted=12):
            profiler.observe_label(label)
    long_profile = profiler.profile(VENUE, SYMBOL, LONG)
    short_profile = profiler.profile(VENUE, SYMBOL, SHORT)
    assert long_profile.side == LONG and short_profile.side == SHORT
    assert (long_profile.trades_observed, short_profile.trades_observed) != (0, 0)


def test_a_label_with_no_direction_is_refused_by_name(real_labels):
    profiler = an_excursion_profiler()
    unusable = TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector=DETECTOR, regime="any",
        labels={THE_SETUP_WAS_RIGHT: True}, horizon_seconds=HORIZON_SECONDS,
        seconds_to_resolve=3.0, resolved_within_horizon=True, features={},
        built_at_ns=1, claimed_at_ns=0,
    )
    assert profiler.observe_label(unusable) == REFUSED_NO_DIRECTION
    assert describe_excursion_profiling(profiler)["by_refusal"][REFUSED_NO_DIRECTION] == 1


def test_a_label_that_judges_no_setup_is_refused_by_name():
    profiler = an_excursion_profiler()
    unusable = TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector=DETECTOR, regime="any",
        labels={}, horizon_seconds=HORIZON_SECONDS, seconds_to_resolve=3.0,
        resolved_within_horizon=True, features={}, built_at_ns=1, claimed_at_ns=0,
        direction=LONG,
    )
    assert profiler.observe_label(unusable) == REFUSED_NO_SETUP_VERDICT


def test_a_quantile_outside_the_distribution_is_refused_at_construction():
    with pytest.raises(ValueError, match="inside the distribution"):
        SignalExcursionProfiler(
            window=WINDOW, minimum_claims=5, adverse_quantile=1.0,
            favourable_quantiles=FAVOURABLE_QUANTILES,
        )


def test_a_profiler_with_no_favourable_quantile_is_refused():
    with pytest.raises(ValueError, match="nowhere to put a target"):
        SignalExcursionProfiler(
            window=WINDOW, minimum_claims=5, adverse_quantile=0.85, favourable_quantiles=(),
        )


# ---- signal-horizon-profiler -------------------------------------------------

def test_the_horizon_is_the_median_of_real_settled_claims(real_labels):
    profiler = SignalHorizonProfiler(window=WINDOW, minimum_claims=MINIMUM_CLAIMS)
    for label in real_labels:
        profiler.observe_label(label)
    profile = profiler.profile(DETECTOR)
    assert profile.is_fitted, f"only {profile.trades_observed} claims were recorded"
    assert profile.median_seconds > 0
    durations = sorted(label.seconds_to_resolve for label in real_labels)
    assert durations[0] <= profile.median_seconds <= durations[-1], (
        "a median outside the sample is not a median of it"
    )
    assert profile.source == FROM_SETTLED_CLAIMS


def test_each_detector_gets_its_own_horizon(real_prices):
    """How long a move takes is a property of what was noticed, not of the book."""
    profiler = SignalHorizonProfiler(window=WINDOW, minimum_claims=2)
    for detector in (DETECTOR, "momentum-burst-detector"):
        for label in labels_from_real_prices(real_prices, detector=detector, wanted=8):
            profiler.observe_label(label)
    profiles = {profile.detector: profile for profile in profiler.profile_all()}
    assert set(profiles) == {DETECTOR, "momentum-burst-detector"}


def test_a_horizon_with_too_few_claims_reports_itself_unfitted(real_labels):
    profiler = SignalHorizonProfiler(window=WINDOW, minimum_claims=len(real_labels) + 100)
    for label in real_labels:
        profiler.observe_label(label)
    profile = profiler.profile(DETECTOR)
    assert profile.is_fitted is False
    assert profile.median_seconds == 0.0
    assert "no plan may name a horizon from it" in profile.reason


def test_a_claim_that_resolved_in_no_time_is_refused():
    """It did not resolve; it was opened at a price already through a barrier.

    Counting it would pull every horizon towards zero, and an exit plan would then
    give a trade no time at all to work.
    """
    profiler = SignalHorizonProfiler(window=WINDOW, minimum_claims=2)
    instant = TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector=DETECTOR, regime="any",
        labels={THE_SETUP_WAS_RIGHT: True}, horizon_seconds=HORIZON_SECONDS,
        seconds_to_resolve=0.0, resolved_within_horizon=True, features={},
        built_at_ns=1, claimed_at_ns=0, direction=LONG,
    )
    assert profiler.observe_label(instant) == REFUSED_NOT_A_DURATION
    assert describe_horizon_profiling(profiler)["refused"] == 1


def test_the_window_forgets_the_market_that_ended(real_labels):
    """A rolling window, not all of history: an old horizon describes an old market."""
    profiler = SignalHorizonProfiler(window=3, minimum_claims=2)
    for label in real_labels:
        profiler.observe_label(label)
    assert profiler.profile(DETECTOR).trades_observed == 3
