"""What the market said about a detector's claim reaches the detector that made it.

The loop this closes was open from the day the scanner was written until
2026-08-28. Nine detectors each carry a `SignalCalibrator` and each define
`observe_outcome`, `signal-outcome-labeller` measures exactly the outcome that
method wants, and **nothing carried one to the other**: none of the nine declared
an input that could carry an outcome, so no `start_part` had anything to call it
with. All nine reported `outcomes_learned: 0`, `EntryCandidate.confidence` was
the untested prior on every candidate ever raised, and `detector_hit_rate` was
missing on 100% of the 5,922 feature vectors the two bots had built.
`docs/proposals/nine-detectors-that-never-learn-whether-they-were-right.md`.

**Only the whole chain is a test of it.** Each part in isolation was correct and
each reported itself healthy: the detector raised claims, the labeller judged
them, and the wire between the two did not exist. So every part here is the real
one, the prices are real captured Binance trades (RL-063), and nothing is stubbed
but the clock and the regime the detector is handed.

The property under test is not "the confidence goes up". Whether this detector's
measured hit rate is better or worse than its prior is the market's business. It
is that the record arrives at all, that it is keyed so it reaches the estimator
that made the claim, and that the two labels which are *not* about this
detector's setup are refused.
"""

from __future__ import annotations

import pytest

from parts.learning_loop.signal_outcome_labeller import SignalOutcomeLabeller
from parts.opportunity_scanner.mean_reversion_detector import (
    FIRED,
    PART_ID as DETECTOR_PART_ID,
    MeanReversionDetector,
)
from runtime.learning_types import THE_SETUP_WAS_RIGHT, TrainingLabel
from runtime.market_signal import REVERSION, SignalCalibrator, settle_claims_from

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
CAPTURED_RUN = "2026-08-22-btcusdt-aggtrade-run.jsonl"

# The regime this detector calibrates on, and the one that favours reversion.
# Six of the nine detectors key their record on the regime; the sweeper keys on
# its condition, the whale reader on flow direction and the sentiment reader on
# the relationship it found -- which is why the key travels on the claim rather
# than being re-derived when the claim settles.
REVERTING = "reverting"

# Placed where this capture can actually reach them. The run spans 0.133% of
# price across 2,000 trades, so a barrier at the shipped 20 basis points is never
# touched inside it; what the shipped numbers resolve on the real tape is
# measured elsewhere (measurements/2026-08-22-vertical-numbers/README.md).
MOVE_FRACTION = 0.0001
WINDOW_LENGTH = 64
MINIMUM_OBSERVATIONS = 32
Z_THRESHOLD = 2.0
MINIMUM_VOLATILITY_FRACTION = 1e-6
HORIZON_SECONDS = 30.0
MAXIMUM_OPEN_CLAIMS = 100
NANOSECONDS = 1_000_000_000

# What the calibrator needs before it will call its rate fitted rather than the
# prior. Stated here so the assertion below is about a number this test set.
CALIBRATOR_MINIMUM_OBSERVATIONS = 5


class Clock:
    """A clock the test moves, so a horizon can expire without waiting for one."""

    def __init__(self, at_ns: int = 1_787_000_000_000_000_000) -> None:
        self.at_ns = at_ns

    def __call__(self) -> int:
        return self.at_ns

    def advance_seconds(self, seconds: float) -> None:
        self.at_ns += int(seconds * NANOSECONDS)


class Reverting:
    """The regime the detector is handed: a market that reverts, and how strongly.

    A stand-in for `regime-classifier`'s own payload, which is what the running
    part reads. The Hurst exponent is what the classifier measures and what the
    detector puts in its evidence; below 0.5 is a series that reverts.
    """

    regime = REVERTING
    hurst = 0.3

    def favours(self, expectation: str) -> bool:
        return expectation == REVERSION


@pytest.fixture
def real_prices(read_captured_payloads):
    """Prices from real Binance aggTrade frames, in the order they arrived."""
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, CAPTURED_RUN)
    prices = [trade.price for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(prices) > MINIMUM_OBSERVATIONS * 4, (
        "the captured run must hold enough prices for a window to fill and a claim to settle"
    )
    return prices


def a_detector(clock: Clock) -> MeanReversionDetector:
    return MeanReversionDetector(
        window_length=WINDOW_LENGTH,
        minimum_observations=MINIMUM_OBSERVATIONS,
        z_threshold=Z_THRESHOLD,
        minimum_volatility_fraction=MINIMUM_VOLATILITY_FRACTION,
        horizon_seconds=HORIZON_SECONDS,
        calibrator=SignalCalibrator(
            prior_hit_rate=0.5,
            prior_weight=1.0,
            half_life_observations=50.0,
            minimum_observations=CALIBRATOR_MINIMUM_OBSERVATIONS,
        ),
        now_ns=clock,
    )


def run_the_loop(prices, seconds_per_print: float = 1.0):
    """The scanner, the labeller and the record going back, over one real series.

    One pass: every price reaches the detector and the labeller, every candidate
    the detector raises becomes a claim, and every label the market settles is
    handed straight back to the detector that claimed it.
    """
    clock = Clock()
    detector = a_detector(clock)
    labeller = SignalOutcomeLabeller(
        move_fraction=MOVE_FRACTION,
        maximum_open_claims=MAXIMUM_OPEN_CLAIMS,
        now_ns=clock,
    )
    regime = Reverting()
    labels_seen = []

    for price in prices:
        clock.advance_seconds(seconds_per_print)
        detector.observe_price(VENUE, SYMBOL, price, clock())
        labeller.observe_price(VENUE, SYMBOL, price, clock())

        candidate, outcome = detector.detect(VENUE, SYMBOL, regime)
        if outcome == FIRED:
            labeller.observe_candidate(candidate, regime_name=REVERTING)

        settled = labeller.resolve_settled_claims()
        labels_seen.extend(settled)
        settle_claims_from(settled, detector, DETECTOR_PART_ID)

    return detector, labeller, labels_seen


def test_a_settled_claim_reaches_the_detector_that_made_it(real_prices):
    """The edge that did not exist: outcomes_learned climbing off zero."""
    detector, _labeller, labels = run_the_loop(real_prices)

    assert labels, (
        "the real series settled no claim at all, so this test proves nothing about "
        "the record arriving -- widen the capture or the barrier, never assert on zero"
    )
    assert detector.standing.candidates > 0
    assert detector.standing.outcomes_learned > 0
    assert detector.standing.outcomes_learned == len(
        [label for label in labels if label.label_for(THE_SETUP_WAS_RIGHT) is not None]
    )


def test_the_label_carries_the_key_the_claim_was_made_under(real_prices):
    """Carried, not re-derived: three of the nine key on something else entirely."""
    _detector, _labeller, labels = run_the_loop(real_prices)

    assert labels
    assert {label.calibration_key for label in labels} == {REVERTING}
    assert {label.detector for label in labels} == {DETECTOR_PART_ID}


def test_the_confidence_a_candidate_carries_stops_being_the_prior(real_prices):
    """What the bots read. Before this the estimate was untested on every claim."""
    detector, _labeller, labels = run_the_loop(real_prices)

    assert len(labels) >= CALIBRATOR_MINIMUM_OBSERVATIONS, (
        "fewer settled claims than the calibrator's own minimum, so an unfitted "
        "estimate here would say nothing about whether the record was used"
    )
    estimate = detector._calibrator.confidence(DETECTOR_PART_ID, REVERTING)
    assert estimate.is_fitted
    assert estimate.observations == detector.standing.outcomes_learned


def test_a_label_about_another_detector_is_not_learned_from(real_prices):
    """One wire carries every detector's record; a part takes only its own."""
    clock = Clock()
    detector = a_detector(clock)

    taken = settle_claims_from(
        [a_label(detector="momentum-burst-detector", was_right=True)],
        detector,
        DETECTOR_PART_ID,
    )

    assert taken == 0
    assert detector.standing.outcomes_learned == 0


def test_a_label_from_a_closed_trade_is_not_learned_from(real_prices):
    """`label-builder` publishes on the same wire, and it is judging a trade.

    It sets `THE_SETUP_WAS_RIGHT` from realised PnL after costs, and a trade sized
    badly, entered late or stopped early is not evidence about the setup -- which
    is what `SignalCalibrator`'s own docstring rules out. It has no candidate and
    so carries no calibration key, and that emptiness is the filter.
    """
    clock = Clock()
    detector = a_detector(clock)

    taken = settle_claims_from(
        [a_label(detector=DETECTOR_PART_ID, was_right=True, calibration_key="")],
        detector,
        DETECTOR_PART_ID,
    )

    assert taken == 0
    assert detector.standing.outcomes_learned == 0


def test_a_label_that_says_nothing_about_the_setup_is_not_read_as_false(real_prices):
    """Silence is not a wrong call, and training on None as False would say it is."""
    clock = Clock()
    detector = a_detector(clock)
    label = a_label(detector=DETECTOR_PART_ID, was_right=True)
    label.labels.pop(THE_SETUP_WAS_RIGHT)

    taken = settle_claims_from([label], detector, DETECTOR_PART_ID)

    assert taken == 0
    assert detector.standing.outcomes_learned == 0


def a_label(detector: str, was_right: bool, calibration_key: str = REVERTING) -> TrainingLabel:
    return TrainingLabel(
        venue_id=VENUE,
        symbol=SYMBOL,
        detector=detector,
        regime=REVERTING,
        labels={THE_SETUP_WAS_RIGHT: was_right},
        horizon_seconds=HORIZON_SECONDS,
        seconds_to_resolve=HORIZON_SECONDS / 2,
        resolved_within_horizon=True,
        features={},
        built_at_ns=Clock()(),
        calibration_key=calibration_key,
    )
