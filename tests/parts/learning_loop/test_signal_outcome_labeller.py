"""Labelling a detector's claim against what the market actually did next.

The prices here are real trades captured from Binance and Bybit (RL-063). The
claims are synthetic on purpose: what is under test is the judgement, so the claim
has to be placed where the market's own behaviour makes the answer knowable --
which is only possible by choosing the moment, not by waiting for a detector to
happen to fire during a fixture.

The property that matters most is the one about silence: a claim whose horizon
expires with neither barrier reached is dropped, never labelled false. "The move
did not happen" and "the move went the other way" are different facts, and a model
trained on their union learns that a quiet market is a wrong prediction.
"""

from __future__ import annotations

import json

import pytest

from parts.learning_loop.signal_outcome_labeller import (
    CLAIM_OPENED,
    PART_DECLARATION,
    PART_ID,
    REFUSED_ALREADY_OPEN,
    REFUSED_AT_CAPACITY,
    REFUSED_NO_PRICE,
    REFUSED_STALE_PRICE,
    SignalOutcomeLabeller,
    describe_labelling,
)
from runtime.learned_estimator import Estimate
from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.market_signal import LONG, REVERSION, SHORT, make_candidate
from runtime.part_declaration import load_declaration_from_blueprint

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
# Deliberately not the shipped 0.002. The captured run is 2000 trades spanning
# 0.133% of price, so no claim in it can reach a 20-basis-point barrier; the tests
# below place theirs where this fixture can actually reach them. That the shipped
# threshold resolves 78-100% of claims within its horizon is measured on the tape
# instead -- measurements/2026-08-22-vertical-numbers/README.md.
MOVE_FRACTION = 0.0002
MAXIMUM_OPEN_CLAIMS = 100
HORIZON_SECONDS = 60.0
NANOSECONDS = 1_000_000_000


class Clock:
    """A clock the test moves deliberately, so a horizon can expire on demand."""

    def __init__(self, at_ns: int = 1_787_000_000_000_000_000) -> None:
        self.at_ns = at_ns

    def __call__(self) -> int:
        return self.at_ns

    def advance_seconds(self, seconds: float) -> None:
        self.at_ns += int(seconds * NANOSECONDS)


@pytest.fixture
def real_prices(read_captured_payloads):
    """Prices from real Binance aggTrade frames, in the order they arrived."""
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, "2026-08-22-btcusdt-aggtrade-run.jsonl")
    prices = [trade.price for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(prices) > 100, "the captured run must hold enough prices to move a barrier"
    return prices


def an_untested_confidence() -> Estimate:
    """What a detector's confidence looks like before it has ever been scored."""
    return Estimate(
        value=0.5,
        is_fitted=False,
        observations=0,
        prior=0.5,
        was_clamped=False,
        bound_low=None,
        bound_high=None,
        reason="no outcomes observed yet",
    )


def a_claim(
    direction: str = LONG,
    horizon_seconds: float = HORIZON_SECONDS,
    calibration_key: str = CALIBRATION_KEY,
):
    return make_candidate(
        detector=DETECTOR,
        venue_id=VENUE,
        symbol=SYMBOL,
        direction=direction,
        expectation=REVERSION,
        signal_strength=2.5,
        confidence=an_untested_confidence(),
        horizon_seconds=horizon_seconds,
        evidence={"spread_z": 2.5},
        reason="the spread is stretched",
        calibration_key=calibration_key,
    )


def a_labeller(clock: Clock) -> SignalOutcomeLabeller:
    return SignalOutcomeLabeller(
        move_fraction=MOVE_FRACTION,
        maximum_open_claims=MAXIMUM_OPEN_CLAIMS,
        now_ns=clock,
    )


def test_the_built_wiring_equals_the_blueprint():
    """RL-067: what was built consumes and produces what the blueprint declares."""
    declared = load_declaration_from_blueprint(PART_ID)
    assert PART_DECLARATION.consumes == declared.consumes
    assert PART_DECLARATION.produces == declared.produces


def test_a_claim_with_no_price_behind_it_is_refused(real_prices):
    """Measuring a move needs the price the claim was made at, not the next one."""
    labeller = a_labeller(Clock())
    claim, reason = labeller.observe_candidate(a_claim())

    assert claim is None
    assert reason == REFUSED_NO_PRICE
    assert describe_labelling(labeller)["by_refusal"] == {REFUSED_NO_PRICE: 1}


def test_a_move_in_the_claimed_direction_labels_the_setup_right(real_prices):
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)

    claim, reason = labeller.observe_candidate(a_claim(LONG))
    assert reason == CLAIM_OPENED
    assert claim is not None

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.1), clock.at_ns)
    labels = labeller.resolve_settled_claims()

    assert len(labels) == 1
    label = labels[0]
    assert label.labels == {THE_SETUP_WAS_RIGHT: True}
    assert label.detector == DETECTOR
    assert label.venue_id == VENUE
    assert label.symbol == SYMBOL
    assert label.resolved_within_horizon is True
    assert label.seconds_to_resolve == pytest.approx(1.0)
    assert label.claimed_at_ns > 0
    # NOT the detector's own evidence. A pair detector's spread_z is meaningful
    # only inside that detector, so a formula mined over it names something no
    # scanner can evaluate on an arbitrary symbol -- which is why every mined
    # formula was refused as NO_MEASUREMENT until 2026-08-26.
    assert "spread_z" not in label.features
    assert labeller.standing.resolved_right == 1
    assert labeller.standing.measured_hit_rate == 1.0


def test_a_move_against_the_claim_labels_the_setup_wrong(real_prices):
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)
    labeller.observe_candidate(a_claim(LONG))

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 - MOVE_FRACTION * 1.1), clock.at_ns)
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: False}]
    assert labeller.standing.resolved_wrong == 1
    assert labeller.standing.measured_hit_rate == 0.0


def test_a_short_claim_is_right_when_the_price_falls(real_prices):
    """The sign is held in one place, so nothing downstream has to know the direction."""
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)
    labeller.observe_candidate(a_claim(SHORT))

    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 - MOVE_FRACTION * 1.1), clock.at_ns)
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: True}]


def test_a_quiet_market_produces_no_label_at_all(real_prices):
    """The property this part would be most tempting to get wrong."""
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)
    labeller.observe_candidate(a_claim())

    # Real prices, drifting less than the barrier.
    for price in real_prices[:50]:
        nudged = opening_price * (1 + (price / opening_price - 1) * 0.01)
        labeller.observe_price(VENUE, SYMBOL, nudged, clock.at_ns)

    clock.advance_seconds(HORIZON_SECONDS + 1)
    labels = labeller.resolve_settled_claims()

    assert labels == (), "an unresolved claim must not be labelled false"
    assert labeller.standing.unresolved == 1
    assert labeller.standing.labels_published == 0
    assert labeller.standing.measured_hit_rate is None
    assert labeller.open_claims == (), "a claim past its horizon is not still open"


def test_a_claim_is_labelled_when_the_barrier_is_hit_not_when_the_horizon_ends(real_prices):
    """A move that happened and reversed is a move that happened."""
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)
    labeller.observe_candidate(a_claim(LONG))

    clock.advance_seconds(2.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.2), clock.at_ns)
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)  # gave it all back
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: True}]


def test_one_open_claim_per_detector_per_symbol(real_prices):
    """A detector firing every tick must not teach one move a thousand times."""
    labeller = a_labeller(Clock())
    labeller.observe_price(VENUE, SYMBOL, real_prices[0], labeller._now_ns())
    labeller.observe_candidate(a_claim())
    claim, reason = labeller.observe_candidate(a_claim())

    assert claim is None
    assert reason == REFUSED_ALREADY_OPEN
    assert labeller.standing.open_claims == 1


def test_claims_are_bounded_and_the_refusal_is_counted(real_prices):
    labeller = SignalOutcomeLabeller(
        move_fraction=MOVE_FRACTION, maximum_open_claims=2, now_ns=Clock()
    )
    for index in range(4):
        symbol = f"SYMBOL{index}"
        labeller.observe_price(VENUE, symbol, 100.0, labeller._now_ns())
        labeller.observe_candidate(
            make_candidate(
                detector=DETECTOR,
                venue_id=VENUE,
                symbol=symbol,
                direction=LONG,
                expectation=REVERSION,
                signal_strength=1.0,
                confidence=an_untested_confidence(),
                horizon_seconds=HORIZON_SECONDS,
                evidence={},
                reason="test",
                calibration_key=CALIBRATION_KEY,
            )
        )

    assert labeller.standing.open_claims == 2
    assert labeller.standing.by_refusal.get(REFUSED_AT_CAPACITY) == 2


def test_a_move_fraction_of_zero_is_refused_at_construction():
    with pytest.raises(ValueError):
        SignalOutcomeLabeller(move_fraction=0.0, maximum_open_claims=10)


def test_real_captured_prices_resolve_claims_both_ways(real_prices):
    """Placed against the real series, the labeller produces both labels.

    Not a property of the code so much as of the market -- and that is the point:
    a labeller that could only ever say one thing would train a model to say it.
    On the tape the split at the shipped threshold is 0.48 to 0.56 across six
    symbols, which is close enough to a coin flip that anything the model learns
    has to come from the features rather than from the base rate.
    """
    clock = Clock()
    labeller = a_labeller(clock)
    verdicts = []
    for index in range(0, len(real_prices) - 1, 40):
        price = real_prices[index]
        labeller.observe_price(VENUE, SYMBOL, price, clock.at_ns)
        labeller.observe_candidate(a_claim(LONG))
        for later in real_prices[index + 1 : index + 40]:
            labeller.observe_price(VENUE, SYMBOL, later, clock.at_ns)
        clock.advance_seconds(HORIZON_SECONDS + 1)
        for label in labeller.resolve_settled_claims():
            verdicts.append(label.labels[THE_SETUP_WAS_RIGHT])

    assert verdicts, "no claim resolved against the real series at all"
    standing = describe_labelling(labeller)
    assert standing["resolved_right"] + standing["resolved_wrong"] == len(verdicts)
    assert standing["measured_hit_rate"] is not None


def test_a_claim_is_refused_when_the_price_it_would_be_measured_against_is_stale():
    """A wrong label is worse than a wrong trade: it survives every trade after it.

    The claim's whole meaning is "the detector called this move from here". Opened
    against a price the market had already left, it records a starting point that
    never existed at that moment, and the conviction model learns a detector's
    skill from the difference.
    """
    from runtime.price_staleness import PriceStalenessEstimator

    clock = Clock()
    pinned_to_one_second = PriceStalenessEstimator(
        materiality_fraction=0.0011,
        anchor_seconds=1.0,
        quantile=0.95,
        window=3_600,
        observations_needed=300,
        prior_one_second_move=0.000898,
        minimum_age_seconds=1.0,
        maximum_age_seconds=1.0,
    )
    labeller = SignalOutcomeLabeller(
        move_fraction=MOVE_FRACTION,
        maximum_open_claims=10,
        price_staleness=pinned_to_one_second,
        now_ns=clock,
    )
    labeller.observe_price(VENUE, SYMBOL, 100.0, clock.at_ns)

    fresh, reason = labeller.observe_candidate(a_claim())
    assert fresh is not None, reason

    clock.advance_seconds(3_360.0)
    # A different detector, so the only thing that can refuse this claim is the
    # age of the price -- not the one-claim-per-detector rule.
    import dataclasses

    stale, reason = labeller.observe_candidate(
        dataclasses.replace(a_claim(), detector="a-detector-with-no-claim-open")
    )
    assert stale is None
    assert reason == REFUSED_STALE_PRICE


def test_a_labeller_told_no_bound_labels_against_whatever_it_has():
    """RL-061: the bound is a named setting, and this part invents none."""
    clock = Clock()
    labeller = a_labeller(clock)
    labeller.observe_price(VENUE, SYMBOL, 100.0, clock.at_ns)
    clock.advance_seconds(3_360.0)

    claim, reason = labeller.observe_candidate(a_claim())
    assert claim is not None, reason


def test_a_label_carries_the_universal_vocabulary_not_the_detector_s_own_evidence(real_prices):
    """What the miner may mine over has to be what the scanner can watch.

    The measurements are computed by the caller -- start_part holds a window per
    symbol and the whole universe at once -- and handed in, because the
    cross-sectional half of the vocabulary is a statement about every symbol and
    no single claim can make it.
    """
    from runtime.sweep_measurements import KNOWN_MEASUREMENTS, RETURN_OVER_WINDOW

    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)

    measured = {RETURN_OVER_WINDOW: 0.031, "return_rank": 0.94}
    claim, reason = labeller.observe_candidate(a_claim(LONG), measurements=measured)
    assert reason == CLAIM_OPENED
    assert claim is not None

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.1), clock.at_ns)
    label = labeller.resolve_settled_claims()[0]

    assert label.features == measured
    assert set(label.features) <= set(KNOWN_MEASUREMENTS)


def test_a_claim_with_no_measurements_still_resolves(real_prices):
    """It trains the conviction model on its outcome; it just carries no features.

    That is a lesser thing than a claim that never opened, and the difference is
    why an absent measurement is not a reason to refuse.
    """
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price, clock.at_ns)

    claim, reason = labeller.observe_candidate(a_claim(LONG))
    assert reason == CLAIM_OPENED

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.1), clock.at_ns)
    label = labeller.resolve_settled_claims()[0]

    assert label.features == {}
    assert label.labels == {THE_SETUP_WAS_RIGHT: True}


# ---- the regime every label ever carried -------------------------------------
#
# observe_candidate took `regime_name: str = "any"` and start_part never passed
# one, so TrainingLabel.regime has been that constant on every label the system
# has ever built -- and everything that learns per regime (the signal calibrator,
# the regime tagger, the conviction models) was pooling regimes it could not tell
# apart into one number that describes none of them.

def test_an_unpassed_regime_reads_as_not_known_rather_than_as_a_market(real_prices):
    """"any" reads like a claim about the market; this is an admission about the reader."""
    from parts.learning_loop.signal_outcome_labeller import REGIME_NOT_KNOWN

    clock = Clock()
    labeller = a_labeller(clock)
    labeller.observe_price(VENUE, SYMBOL, real_prices[0], clock.at_ns)
    claim, _ = labeller.observe_candidate(a_claim(LONG))

    assert claim.regime == REGIME_NOT_KNOWN
    assert claim.regime != "any"


def test_a_classified_regime_reaches_the_label(real_prices):
    clock = Clock()
    labeller = a_labeller(clock)
    opening = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening, clock.at_ns)
    labeller.observe_candidate(a_claim(LONG), regime_name="trending")

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening * (1 + MOVE_FRACTION * 1.1), clock.at_ns)
    label = labeller.resolve_settled_claims()[0]

    assert label.regime == "trending"


# ---- a staleness bound answering the wrong question --------------------------

def test_the_labellers_price_bound_is_its_own_barrier_not_a_round_trip_fee():
    """The labeller judges against its own barrier, not against a round-trip fee.

    Measured live when this was written: 1,053 of 1,397 claims refused for a
    stale price. The bound was not wrong, it was answering a different question
    -- how old may a price be before ACTING on it costs more than the round trip.
    This part never acts; its price is contaminated when it has drifted far
    enough to distort the barrier the claim will be judged against. The bound
    goes as the SQUARE of the materiality, which is what this pins.

    **This test could not run at all between 2026-09-07 and 2026-09-08**: the
    stub below was missing `reference_price_materiality_fraction` from the day
    `price_staleness_from` began reading it, so it raised KeyError instead of
    asserting anything. Completing it exposed a finding bigger than the test.

    With the crypto numbers it was written against (prior 0.000898, materiality
    0.0011) the trading bound is 1.50 s and the labelling bound 4.96 s -- the
    labeller's barrier is the wider one, which is the property this test exists
    to protect. With the numbers actually deployed for NSE (prior 0.002324,
    materiality 0.008532, both re-derived 2026-09-07) the trading bound is
    13.48 s and the labelling bound is **1.00 s -- its floor.**

    `signal_label_move_fraction` is 0.002, still the crypto-era barrier: on an
    NSE option a round trip costs 0.8532%, so a claim is marked right or wrong on
    a move four times smaller than the cost of taking it, and the bound derived
    from it has bottomed out on `reference_price_minimum_age_seconds`. That is
    exactly the collapse `instrument-selector` warns about in its own standing --
    "a bound that quietly collapsed to its floor would stop every trade while
    looking exactly like a market nobody wanted to trade". Recorded in
    docs/feature-audit.md; re-deriving that barrier for NSE is its own change
    with its own evidence, and asserting a number here would be picking one.
    """
    from runtime.price_staleness import price_staleness_from

    def settings_for(prior, materiality):
        class Settings:
            health_interval_seconds = 1.0

            def number(self, name):
                return {
                    "taker_fee_rate": 0.00055,
                    "signal_label_move_fraction": 0.002,
                    "reference_price_move_anchor_seconds": 1.0,
                    "reference_price_move_quantile": 0.95,
                    "reference_price_move_window": 3600,
                    "reference_price_move_observations_needed": 300,
                    "reference_price_prior_one_second_move": prior,
                    "reference_price_materiality_fraction": materiality,
                    "reference_price_minimum_age_seconds": 1.0,
                    "reference_price_maximum_age_seconds": 60.0,
                }[name]

        return Settings()

    def bounds_under(prior, materiality):
        context = settings_for(prior, materiality)
        trading = price_staleness_from(context).believable_age_seconds(VENUE, SYMBOL)
        labelling = price_staleness_from(
            context, materiality_fraction=context.number("signal_label_move_fraction")
        ).believable_age_seconds(VENUE, SYMBOL)
        return trading, labelling

    # The property, on the numbers this test was written against: the labeller's
    # own barrier is wider than a round-trip fee, and the bound goes as the
    # square of the materiality so the gap is threefold rather than marginal.
    trading, labelling = bounds_under(prior=0.000898, materiality=0.0011)
    assert trading.value == pytest.approx(1.50, abs=0.01)
    assert labelling.value == pytest.approx(4.96, abs=0.01)
    assert labelling.value > trading.value
    # Still derived and still bounded -- not a number chosen to admit more claims.
    assert labelling.bound_high == 60.0

    # On the numbers actually deployed for NSE the property has inverted, and
    # the labelling bound has collapsed onto its floor. See the docstring: the
    # barrier is a crypto-era number, not this bound.
    trading, labelling = bounds_under(prior=0.002324, materiality=0.008532)
    assert trading.value == pytest.approx(13.48, abs=0.01)
    assert labelling.value == pytest.approx(1.00, abs=0.01), (
        "signal_label_move_fraction has stopped producing a bound of its own"
    )
