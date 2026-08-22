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


def a_claim(direction: str = LONG, horizon_seconds: float = HORIZON_SECONDS):
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
    labeller.observe_price(VENUE, SYMBOL, opening_price)

    claim, reason = labeller.observe_candidate(a_claim(LONG))
    assert reason == CLAIM_OPENED
    assert claim is not None

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.1))
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
    assert label.features == {"spread_z": 2.5}
    assert labeller.standing.resolved_right == 1
    assert labeller.standing.measured_hit_rate == 1.0


def test_a_move_against_the_claim_labels_the_setup_wrong(real_prices):
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price)
    labeller.observe_candidate(a_claim(LONG))

    clock.advance_seconds(1.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 - MOVE_FRACTION * 1.1))
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: False}]
    assert labeller.standing.resolved_wrong == 1
    assert labeller.standing.measured_hit_rate == 0.0


def test_a_short_claim_is_right_when_the_price_falls(real_prices):
    """The sign is held in one place, so nothing downstream has to know the direction."""
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price)
    labeller.observe_candidate(a_claim(SHORT))

    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 - MOVE_FRACTION * 1.1))
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: True}]


def test_a_quiet_market_produces_no_label_at_all(real_prices):
    """The property this part would be most tempting to get wrong."""
    clock = Clock()
    labeller = a_labeller(clock)
    opening_price = real_prices[0]
    labeller.observe_price(VENUE, SYMBOL, opening_price)
    labeller.observe_candidate(a_claim())

    # Real prices, drifting less than the barrier.
    for price in real_prices[:50]:
        nudged = opening_price * (1 + (price / opening_price - 1) * 0.01)
        labeller.observe_price(VENUE, SYMBOL, nudged)

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
    labeller.observe_price(VENUE, SYMBOL, opening_price)
    labeller.observe_candidate(a_claim(LONG))

    clock.advance_seconds(2.0)
    labeller.observe_price(VENUE, SYMBOL, opening_price * (1 + MOVE_FRACTION * 1.2))
    labeller.observe_price(VENUE, SYMBOL, opening_price)  # gave it all back
    labels = labeller.resolve_settled_claims()

    assert [label.labels for label in labels] == [{THE_SETUP_WAS_RIGHT: True}]


def test_one_open_claim_per_detector_per_symbol(real_prices):
    """A detector firing every tick must not teach one move a thousand times."""
    labeller = a_labeller(Clock())
    labeller.observe_price(VENUE, SYMBOL, real_prices[0])
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
        labeller.observe_price(VENUE, symbol, 100.0)
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
        labeller.observe_price(VENUE, SYMBOL, price)
        labeller.observe_candidate(a_claim(LONG))
        for later in real_prices[index + 1 : index + 40]:
            labeller.observe_price(VENUE, SYMBOL, later)
        clock.advance_seconds(HORIZON_SECONDS + 1)
        for label in labeller.resolve_settled_claims():
            verdicts.append(label.labels[THE_SETUP_WAS_RIGHT])

    assert verdicts, "no claim resolved against the real series at all"
    standing = describe_labelling(labeller)
    assert standing["resolved_right"] + standing["resolved_wrong"] == len(verdicts)
    assert standing["measured_hit_rate"] is not None
