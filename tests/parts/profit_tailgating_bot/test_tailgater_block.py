"""The tailgater: joining what works, and the four ways that goes wrong.

This bot's premise is that a move already under way is easier to be right about
than one that has not started. Its risks are all consequences of that: being
late, being crowded, adding to a winner until the book is one position, and
naming a target for a move whose size it cannot know.

Every one of those has a test that fails if the protection against it is dropped,
because each is invisible in a passing backtest and expensive in a live one.
"""

import importlib

import pytest

from parts.profit_tailgating_bot.tail_copy_selector import (
    CANNOT_EXIT_INDEPENDENTLY, NO_LATENCY_RECORD, SCORE_TOO_LOW, TOO_CROWDED, TOO_LATE,
    TRADER_NOT_SCORED_HERE, ExternalPosition, TailCopySelector,
)
from parts.profit_tailgating_bot.tail_crowding_detector import TailCrowdingDetector
from parts.profit_tailgating_bot.tail_follow_conviction_model import (
    CHALLENGER, CHAMPION, CROWDING_UNKNOWN, IS_CROWDED, NOTHING_LEFT, VIEWS_DISAGREE,
    TailFollowConvictionModel,
)
from parts.profit_tailgating_bot.tail_move_remaining_estimator import (
    TailMoveRemainingEstimator,
)
from parts.profit_tailgating_bot.tail_mover_qualifier import (
    ALREADY_FINISHED, BARELY_STARTED, COST_EXCEEDS_WHAT_IS_LEFT, NOT_A_CONTINUATION,
    NOT_SUSTAINED, SETUP_DISCOUNTED, TailMoverQualifier,
)
from parts.profit_tailgating_bot.tail_opinion_composer import (
    PLAN_NAMES_A_TARGET, WOULD_BE_A_REVERSAL, TailOpinionComposer,
)
from parts.profit_tailgating_bot.tail_setup_weight_learner import TailSetupWeightLearner
from parts.profit_tailgating_bot.tail_trailing_exit_planner import (
    NO_EXCURSION_PROFILE, TRAIL_WOULD_EXCEED_WHAT_IS_LEFT, TAIL_TRAIL_RULE_NAME,
    NO_MOVE_REMAINING, RetracementProfile, TailTrailingExitPlanner,
    describe_trailing,
)
from parts.profit_tailgating_bot.tail_winner_selector import (
    ALREADY_CONCENTRATED, GIVING_PROFIT_BACK, NOT_IN_PROFIT, NOT_THE_WINNING_SIDE,
    NO_PEAK_RECORD, NO_VERDICT, SETTLED_ON_NEITHER_SIDE, PeakExcursion,
    TailWinnerSelector,
)
from runtime.trade_decoding_types import (
    ExitCounterfactual, LONG_SIDE_WON, NO_DIRECTIONAL_EDGE, PairVerdict, SHORT_SIDE_WON,
)
from runtime.bot_opinion import (
    CROWDED, CROWDING_NOT_MEASURED, ENTER_NOW, ESTIMATES_AGREE, ESTIMATES_DISAGREE,
    FROM_A_SCANNER_MOVE, FROM_A_TRACKED_TRADER, FROM_FUNDING, FROM_OUR_OWN_WINNER,
    FROM_THE_BOOK, LONG, NOT_CROWDED, NO_EXIT_PLAN, ONLY_ONE_ESTIMATE, SHORT, STAND_DOWN,
    BotScorecard, CrowdingReading, ExitTarget, FollowCandidate, MoveRemaining,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.market_signal import CONTINUATION, make_candidate
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "tail-mover-qualifier": "parts.profit_tailgating_bot.tail_mover_qualifier",
    "tail-winner-selector": "parts.profit_tailgating_bot.tail_winner_selector",
    "tail-copy-selector": "parts.profit_tailgating_bot.tail_copy_selector",
    "tail-move-remaining-estimator": "parts.profit_tailgating_bot.tail_move_remaining_estimator",
    "tail-crowding-detector": "parts.profit_tailgating_bot.tail_crowding_detector",
    "tail-follow-conviction-model": "parts.profit_tailgating_bot.tail_follow_conviction_model",
    "tail-trailing-exit-planner": "parts.profit_tailgating_bot.tail_trailing_exit_planner",
    "tail-opinion-composer": "parts.profit_tailgating_bot.tail_opinion_composer",
    "tail-setup-weight-learner": "parts.profit_tailgating_bot.tail_setup_weight_learner",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
SECOND = 1_000_000_000
DETECTOR = "momentum-burst-detector"
# What the detector named above calibrates on, carried on the claim so the label
# can be routed back to the estimator that made it. Six of the nine detectors key
# on the market regime and three do not -- the sweeper on its condition, the whale
# reader on flow direction, the sentiment reader on the relationship it found --
# which is why the key travels with the candidate rather than being re-derived
# from the regime at settlement.
CALIBRATION_KEY = "trending"


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_seconds(self, seconds):
        self.now_ns += int(seconds * 1e9)


def an_estimate(value, observations, is_fitted, reason="measured"):
    return Estimate(
        value=value, is_fitted=is_fitted, observations=observations, prior=0.5,
        was_clamped=False, bound_low=None, bound_high=None, reason=reason,
    )


def a_follow(direction=LONG, source=FROM_A_SCANNER_MOVE, done=0.5, cost=0.001, weight=1.0):
    normal = 0.04
    return FollowCandidate(
        bot="profit-tailgating-bot", source=source, venue_id=VENUE, symbol=SYMBOL,
        direction=direction, move_so_far=normal * done, move_normal=normal,
        observations_in_move=8, entry_cost_fraction=cost, setup_weight=weight,
        detector=DETECTOR, evidence={}, reason="joined", qualified_at_ns=Clock()(),
    )


def a_remaining(fraction=0.02, agreement=ESTIMATES_AGREE, lowest=0.018, highest=0.022, count=3):
    return MoveRemaining(
        bot="profit-tailgating-bot", venue_id=VENUE, symbol=SYMBOL, direction=LONG,
        remaining_fraction=fraction, lowest=lowest, highest=highest,
        estimates={f"view-{index}": fraction for index in range(count)},
        agreement=agreement, reason="estimated", estimated_at_ns=Clock()(),
    )


def a_crowding(state=NOT_CROWDED, readings=None, tripped=()):
    return CrowdingReading(
        bot="profit-tailgating-bot", venue_id=VENUE, symbol=SYMBOL, direction=LONG,
        state=state, tripped_by=tuple(tripped),
        readings=readings if readings is not None else {FROM_THE_BOOK: 0.1, FROM_FUNDING: 0.2},
        sources_measured=2, sources_unavailable=(), reason="read", read_at_ns=Clock()(),
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_a_peer_bot(part_id):
    """R-03: bull, bear and tailgater are peers with no wire between them."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "parts.bull_bot" not in text
    assert "parts.bear_bot" not in text


# ---- tail-mover-qualifier ---------------------------------------------------

def a_qualifier(minimum_observations=3, floor=0.2, ceiling=0.8, remaining_over_cost=2.0):
    return TailMoverQualifier(
        window_length=50, minimum_observations_in_move=minimum_observations,
        minimum_fraction_of_normal_move=floor, maximum_fraction_of_normal_move=ceiling,
        move_quantile=0.5, move_window=200, prior_normal_move_fraction=0.04,
        minimum_remaining_over_cost=remaining_over_cost, default_setup_weight=1.0,
        minimum_setup_weight=0.2,
    )


def a_scanner_candidate(direction=LONG, detector=DETECTOR):
    return make_candidate(
        detector=detector, venue_id=VENUE, symbol=SYMBOL, direction=direction,
        expectation=CONTINUATION, signal_strength=3.0, confidence=an_estimate(0.6, 100, True),
        horizon_seconds=600.0, evidence={}, reason="moving", calibration_key=CALIBRATION_KEY,
    )


def teach_normal_moves(qualifier, fraction=0.04, count=30):
    for _ in range(count):
        qualifier.observe_completed_move(VENUE, SYMBOL, fraction)


def run_a_move(qualifier, steps, start=100.0):
    price = start
    qualifier.observe_price(VENUE, SYMBOL, price, qualifier._now_ns())
    for step in steps:
        price *= 1.0 + step
        qualifier.observe_price(VENUE, SYMBOL, price, qualifier._now_ns())
    return price


def test_one_print_is_not_a_move():
    """A single trade three deviations out is a print; a move is a sequence."""
    subject = a_qualifier(minimum_observations=4)
    teach_normal_moves(subject)
    run_a_move(subject, [0.0, 0.0, 0.02])
    follow, outcome = subject.qualify(a_scanner_candidate())
    assert follow is None
    assert outcome == NOT_SUSTAINED


def test_a_sustained_move_inside_the_band_qualifies():
    subject = a_qualifier(minimum_observations=3, floor=0.2, ceiling=0.9)
    teach_normal_moves(subject, fraction=0.04)
    run_a_move(subject, [0.005] * 4)
    follow, _ = subject.qualify(a_scanner_candidate())
    assert follow is not None
    assert follow.source == FROM_A_SCANNER_MOVE
    assert 0.2 <= follow.fraction_of_a_normal_move_done <= 0.9


def test_a_move_that_has_barely_started_is_not_joined_yet():
    subject = a_qualifier(minimum_observations=3, floor=0.5)
    teach_normal_moves(subject, fraction=0.10)
    run_a_move(subject, [0.001] * 4)
    assert subject.qualify(a_scanner_candidate())[1] == BARELY_STARTED


def test_a_move_past_what_this_symbol_normally_does_is_finished():
    subject = a_qualifier(minimum_observations=3, ceiling=0.8)
    teach_normal_moves(subject, fraction=0.02)
    run_a_move(subject, [0.02] * 4)
    assert subject.qualify(a_scanner_candidate())[1] == ALREADY_FINISHED


def test_a_candidate_pointing_against_the_move_is_not_a_tailgating_setup():
    """Letting one through would make this a third directional bot."""
    subject = a_qualifier(minimum_observations=3)
    teach_normal_moves(subject)
    run_a_move(subject, [0.005] * 4)
    follow, outcome = subject.qualify(a_scanner_candidate(direction=SHORT))
    assert follow is None
    assert outcome in (NOT_A_CONTINUATION, NOT_SUSTAINED)


def test_a_move_whose_remainder_costs_more_than_it_is_worth_is_refused():
    subject = a_qualifier(minimum_observations=3, ceiling=0.99, remaining_over_cost=5.0)
    teach_normal_moves(subject, fraction=0.04)
    subject.observe_symbol_profile(VENUE, SYMBOL, round_trip_cost_fraction=0.01)
    run_a_move(subject, [0.009] * 4)
    assert subject.qualify(a_scanner_candidate())[1] == COST_EXCEEDS_WHAT_IS_LEFT


def test_a_discounted_detector_stops_costing_the_bot_anything():
    subject = a_qualifier()
    subject.observe_setup_weight(DETECTOR, 0.05)
    assert subject.qualify(a_scanner_candidate())[1] == SETUP_DISCOUNTED


def test_a_move_measured_against_this_symbols_own_history_not_a_percentage():
    """The same 2% is enormous in BTCUSDT and trivial in a new listing."""
    quiet = a_qualifier(minimum_observations=3, ceiling=0.8)
    wild = a_qualifier(minimum_observations=3, ceiling=0.8)
    teach_normal_moves(quiet, fraction=0.01)
    teach_normal_moves(wild, fraction=0.20)
    for subject in (quiet, wild):
        run_a_move(subject, [0.005] * 4)
    assert quiet.qualify(a_scanner_candidate())[1] == ALREADY_FINISHED
    assert wild.qualify(a_scanner_candidate())[1] == BARELY_STARTED


# ---- tail-winner-selector ---------------------------------------------------

class PositionStub:
    def __init__(self, quantity=1.0, average_entry_price=20_000.0):
        self.venue_id, self.symbol, self.quantity = VENUE, SYMBOL, quantity
        self.average_entry_price = average_entry_price


def a_winner_selector(maximum_retraced=0.3, maximum_share=0.25, minimum_profit=0.005):
    return TailWinnerSelector(
        maximum_retraced_fraction=maximum_retraced, maximum_symbol_share_of_book=maximum_share,
        minimum_profit_fraction=minimum_profit, default_setup_weight=1.0,
    )


def a_resolved_verdict(verdict=LONG_SIDE_WON, symbol=SYMBOL, is_conclusive=True):
    """A verdict in the shape `exploration-pair-decoder` actually publishes.

    Built from the wire type on purpose. This file built a second class of the
    same name, declared inside the part, until 2026-08-28: it carried
    `winning_symbol`, `losing_symbol` and `has_resolved`, none of which are on
    the payload, so every test here passed against a shape nobody sends.
    """
    return PairVerdict(
        pair_id="pair-1", question="does this setup have directional edge",
        verdict=verdict, long_realised=12.0, short_realised=-4.0, difference=16.0,
        is_conclusive=is_conclusive, reason="the long side is ahead, clear of costs",
        decided_at_ns=SECOND, venue_id=VENUE, symbol=symbol,
    )


POSITION_NOTIONAL = 20_000.0  # PositionStub's default quantity (1.0) * average_entry_price


def a_peak(peak=0.05, current=0.045):
    """A peak excursion in the shape `peak-excursion-tracker` actually publishes:
    unrealised account-currency amounts against a cost basis, not a fraction of the
    position (TailWinnerSelector.select computes the fraction itself, against the
    notional of the position being judged). `peak`/`current` here are the fractions
    the caller wants to see once that division happens, scaled up by
    `POSITION_NOTIONAL` so a `PositionStub()` (quantity=1.0, entry=20_000.0) recovers
    them exactly.
    """
    best_unrealised = peak * POSITION_NOTIONAL
    current_unrealised = current * POSITION_NOTIONAL
    return PeakExcursion(
        venue_id=VENUE, symbol=SYMBOL,
        best_unrealised=best_unrealised, worst_unrealised=min(0.0, current_unrealised),
        best_price=20_000.0 + best_unrealised, worst_price=20_000.0 + min(0.0, current_unrealised),
        current_unrealised=current_unrealised, samples=40, observed_at_ns=SECOND,
    )


def a_prepared_winner_selector(**kwargs):
    subject = a_winner_selector(**kwargs)
    subject.observe_pair_verdict(a_resolved_verdict())
    subject.observe_peak_excursion(a_peak())
    subject.observe_book_value(100_000.0)
    subject.observe_exposure(VENUE, SYMBOL, 10_000.0)
    return subject


def test_the_winning_leg_of_a_resolved_pair_is_selected():
    follow, _ = a_prepared_winner_selector().select(PositionStub())
    assert follow is not None
    assert follow.source == FROM_OUR_OWN_WINNER
    assert follow.direction == LONG


def test_an_unresolved_experiment_is_not_a_winner():
    """Adding on unrealised profit before the verdict is adding on noise."""
    subject = a_winner_selector()
    subject.observe_pair_verdict(a_resolved_verdict(is_conclusive=False))
    assert subject.select(PositionStub())[1] == NO_VERDICT


def test_a_pair_that_found_for_neither_side_is_not_waiting_for_more_evidence():
    """Settled-on-neither is an answer; it must not read as "no verdict yet"."""
    subject = a_prepared_winner_selector()
    subject.observe_pair_verdict(a_resolved_verdict(verdict=NO_DIRECTIONAL_EDGE))
    assert subject.select(PositionStub())[1] == SETTLED_ON_NEITHER_SIDE


def test_a_verdict_naming_no_instrument_cannot_be_filed_against_a_position():
    subject = a_winner_selector()
    subject.observe_pair_verdict(a_resolved_verdict(symbol=None))
    assert subject.standing.verdicts_without_an_instrument == 1
    assert subject.select(PositionStub())[1] == NO_VERDICT


def test_the_losing_side_is_never_added_to():
    """A pair is two directions on one instrument, so the loser is a side."""
    subject = a_prepared_winner_selector()
    subject.observe_pair_verdict(a_resolved_verdict(verdict=SHORT_SIDE_WON))
    assert subject.select(PositionStub(quantity=1.0))[1] == NOT_THE_WINNING_SIDE


def test_a_position_giving_profit_back_is_a_move_that_has_finished():
    """RL-042: up 3% having been up 8% is not the same trade as up 3%."""
    subject = a_prepared_winner_selector(maximum_retraced=0.2)
    subject.observe_peak_excursion(a_peak(peak=0.08, current=0.03))
    assert subject.select(PositionStub())[1] == GIVING_PROFIT_BACK


def test_a_symbol_that_already_holds_too_much_of_the_book_is_refused():
    """Adding to a winner is how a diversified book becomes a single bet."""
    subject = a_prepared_winner_selector(maximum_share=0.05)
    assert subject.select(PositionStub())[1] == ALREADY_CONCENTRATED
    assert subject.standing.concentration_refusals == 1


def test_a_leg_that_is_not_ahead_is_not_a_winner():
    subject = a_prepared_winner_selector(minimum_profit=0.02)
    subject.observe_peak_excursion(a_peak(peak=0.02, current=0.001))
    assert subject.select(PositionStub())[1] == NOT_IN_PROFIT


def test_no_peak_record_means_no_selection():
    subject = a_winner_selector()
    subject.observe_pair_verdict(a_resolved_verdict())
    assert subject.select(PositionStub())[1] == NO_PEAK_RECORD


def test_a_selector_with_no_concentration_ceiling_is_refused_at_construction():
    with pytest.raises(ValueError):
        TailWinnerSelector(
            maximum_retraced_fraction=0.3, maximum_symbol_share_of_book=1.5,
            minimum_profit_fraction=0.005, default_setup_weight=1.0,
        )


# ---- tail-copy-selector -----------------------------------------------------

def a_copy_selector(minimum_score=0.55, maximum_lost=0.3, maximum_followers=50):
    return TailCopySelector(
        minimum_copy_score=minimum_score, maximum_fraction_of_move_lost_to_latency=maximum_lost,
        maximum_followers=maximum_followers, latency_quantile=0.8, latency_window=100,
        prior_latency_seconds=2.0, prior_copy_hit_rate=0.5, prior_weight=4.0,
        half_life_observations=200, minimum_observations=10, default_setup_weight=1.0,
    )


def an_external_position(followers=5, exitable=True, trader="alice"):
    return ExternalPosition(
        trader_id=trader, venue_id=VENUE, symbol=SYMBOL, direction=LONG,
        notional=50_000.0, opened_at_ns=Clock()(), followers_observed=followers,
        is_exitable_by_us=exitable,
    )


def a_tracked_trader(selector, trader="alice", wins=40, losses=10, latency=1.0):
    for _ in range(wins):
        selector.observe_copy_outcome(trader, VENUE, SYMBOL, True)
    for _ in range(losses):
        selector.observe_copy_outcome(trader, VENUE, SYMBOL, False)
    for _ in range(30):
        selector.observe_copy_latency(trader, VENUE, SYMBOL, latency)
    selector.observe_move_speed(VENUE, SYMBOL, fraction_per_second=0.001)
    return selector


def test_a_tracked_position_worth_copying_is_selected():
    subject = a_tracked_trader(a_copy_selector())
    follow, _ = subject.select(an_external_position(), expected_move_fraction=0.02)
    assert follow is not None
    assert follow.source == FROM_A_TRACKED_TRADER


def test_a_trader_we_would_be_too_slow_for_is_refused():
    """Past some fraction of the move we are providing their exit, not copying."""
    subject = a_tracked_trader(a_copy_selector(maximum_lost=0.2), latency=30.0)
    assert subject.select(an_external_position(), expected_move_fraction=0.02)[1] == TOO_LATE


def test_a_crowded_copy_is_refused_not_scored_low():
    """A small position in a crowded copy meets the same exit as a large one."""
    subject = a_tracked_trader(a_copy_selector(maximum_followers=10))
    assert subject.select(an_external_position(followers=500), expected_move_fraction=0.02)[1] == TOO_CROWDED


def test_a_traders_record_does_not_travel_between_symbols():
    """Excellent in majors and reckless in new listings is two records."""
    subject = a_tracked_trader(a_copy_selector())
    elsewhere = ExternalPosition(
        trader_id="alice", venue_id=VENUE, symbol="NEWCOINUSDT", direction=LONG,
        notional=1000.0, opened_at_ns=Clock()(), followers_observed=1, is_exitable_by_us=True,
    )
    assert subject.select(elsewhere, expected_move_fraction=0.02)[1] == TRADER_NOT_SCORED_HERE


def test_a_position_we_cannot_exit_independently_is_never_copied():
    """Following someone in is a decision; following them out is not available."""
    subject = a_tracked_trader(a_copy_selector())
    assert subject.select(
        an_external_position(exitable=False), expected_move_fraction=0.02
    )[1] == CANNOT_EXIT_INDEPENDENTLY


def test_a_poorly_scored_trader_is_refused():
    subject = a_copy_selector(minimum_score=0.6)
    a_tracked_trader(subject, wins=5, losses=45)
    assert subject.select(an_external_position(), expected_move_fraction=0.02)[1] == SCORE_TOO_LOW


def test_a_symbol_whose_speed_is_unknown_cannot_be_priced_for_latency():
    subject = a_copy_selector()
    for _ in range(20):
        subject.observe_copy_outcome("alice", VENUE, SYMBOL, True)
        subject.observe_copy_latency("alice", VENUE, SYMBOL, 1.0)
    assert subject.select(an_external_position(), expected_move_fraction=0.02)[1] == NO_LATENCY_RECORD


# ---- tail-move-remaining-estimator ------------------------------------------

def an_estimator(agreement=0.4, minimum=5, lookback=5):
    return TailMoveRemainingEstimator(
        window_length=50, minimum_observations=minimum, move_quantile=0.5, move_window=200,
        prior_normal_move_fraction=0.04, agreement_fraction=agreement, decay_lookback=lookback,
    )


def test_nothing_to_estimate_from_says_so_rather_than_guessing():
    reading = an_estimator().estimate(a_follow())
    assert reading.remaining_fraction is None
    assert reading.is_sizeable is False


def test_the_three_views_are_reported_separately():
    subject = an_estimator(minimum=5)
    for _ in range(20):
        subject.observe_completed_move(VENUE, SYMBOL, 0.04)
    price = 100.0
    for _ in range(10):
        price *= 1.002
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    subject.observe_price_forecast(VENUE, SYMBOL, 0.02)
    reading = subject.estimate(a_follow(done=0.5))
    assert len(reading.estimates) == 3


def test_disagreeing_views_report_the_smallest_and_say_they_disagree():
    """An average of estimates that disagree is a number no evidence supports."""
    subject = an_estimator(agreement=0.1, minimum=5)
    for _ in range(20):
        subject.observe_completed_move(VENUE, SYMBOL, 0.10)
    price = 100.0
    for _ in range(10):
        price *= 1.0001
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    subject.observe_price_forecast(VENUE, SYMBOL, 0.09)
    reading = subject.estimate(a_follow(done=0.1))
    assert reading.agreement == ESTIMATES_DISAGREE
    assert reading.remaining_fraction == reading.lowest
    assert reading.is_sizeable is False


def test_a_single_view_is_marked_as_uncheckable():
    subject = an_estimator(minimum=5)
    for _ in range(20):
        subject.observe_completed_move(VENUE, SYMBOL, 0.04)
    reading = subject.estimate(a_follow(done=0.5))
    assert reading.agreement == ONLY_ONE_ESTIMATE
    assert "cannot be checked" in reading.reason


def test_a_move_that_has_stopped_making_progress_has_nothing_left_from_decay():
    subject = an_estimator(minimum=5, lookback=5)
    for _ in range(10):
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    reading = subject.estimate(a_follow())
    assert reading.estimates.get("the-move's-own-rate-of-progress") == 0.0


def test_an_estimate_never_exceeds_the_largest_move_ever_recorded():
    """An estimate past that is the model having left the data."""
    subject = an_estimator(agreement=1.0, minimum=5)
    for _ in range(20):
        subject.observe_completed_move(VENUE, SYMBOL, 0.03)
    price = 100.0
    for _ in range(10):
        price *= 1.05
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    subject.observe_price_forecast(VENUE, SYMBOL, 5.0)
    reading = subject.estimate(a_follow(done=0.1))
    assert reading.remaining_fraction <= 0.03
    assert subject.standing.clamped_to_observed == 1


# ---- tail-crowding-detector -------------------------------------------------

def a_crowding_detector(book=0.6, funding=2.0, sentiment=2.0, minimum_sources=2):
    return TailCrowdingDetector(
        book_imbalance_threshold=book, funding_deviation_threshold=funding,
        sentiment_deviation_threshold=sentiment, minimum_observations=10,
        half_life_observations=200, minimum_sources=minimum_sources,
    )


def teach_crowding_normal(detector, count=50):
    for index in range(count):
        detector.observe_funding(VENUE, SYMBOL, 0.0001 * (1 if index % 2 else -1))
        detector.observe_sentiment(VENUE, SYMBOL, 0.5 + (0.01 if index % 2 else -0.01))


def test_an_uncrowded_move_reads_as_uncrowded():
    subject = a_crowding_detector()
    teach_crowding_normal(subject)
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 10.0),), asks=((101.0, 10.0),))
    reading = subject.read(a_follow())
    assert reading.state == NOT_CROWDED
    assert reading.sources_measured == 3


def test_any_one_extreme_source_is_a_refusal_not_a_third_of_one():
    """Averaging three readings is how a crowded trade gets taken."""
    subject = a_crowding_detector(book=0.5)
    teach_crowding_normal(subject)
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 100.0),), asks=((101.0, 1.0),))
    reading = subject.read(a_follow())
    assert reading.is_crowded
    assert reading.tripped_by == (FROM_THE_BOOK,)


def test_extreme_funding_is_the_crowd_paying_to_stay_in():
    subject = a_crowding_detector(funding=2.0)
    teach_crowding_normal(subject)
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 10.0),), asks=((101.0, 10.0),))
    subject.observe_funding(VENUE, SYMBOL, 0.02)
    reading = subject.read(a_follow())
    assert FROM_FUNDING in reading.tripped_by


def test_a_symbol_nobody_could_read_is_not_an_uncrowded_symbol():
    """Rule 8: absence renders as its own state."""
    reading = a_crowding_detector(minimum_sources=2).read(a_follow())
    assert reading.state == CROWDING_NOT_MEASURED
    assert reading.is_crowded is False
    assert reading.is_measured is False


def test_crowding_is_read_in_the_direction_of_the_move():
    subject = a_crowding_detector(book=0.5)
    teach_crowding_normal(subject)
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 100.0),), asks=((101.0, 1.0),))
    assert subject.read(a_follow(direction=LONG)).is_crowded
    assert subject.read(a_follow(direction=SHORT)).is_crowded is False


# ---- tail-follow-conviction-model -------------------------------------------

def a_conviction_model(refuse_on_disagreement=True, minimum_training=20, calibration_minimum=50):
    return TailFollowConvictionModel(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=minimum_training,
        calibration_bin_count=10, calibration_minimum_observations=calibration_minimum,
        calibration_half_life=5000, default_sample_weight=1.0, maximum_sample_weight=5.0,
        refuse_when_views_disagree=refuse_on_disagreement,
    )


def test_a_losing_trade_does_not_take_the_follow_model_down():
    """The crash this replaced, measured on the spine on 2026-08-28.

    `reward-shaper` shapes a closed trade into USDT times its components, so a
    loss shapes to a negative figure. It was handed straight to
    `observe_learning_reward`, whose guard is right -- a non-positive multiplier
    would unlearn the example -- and the raise happened inside the tick. Refused
    and counted now: a losing trade is ordinary traffic.
    """
    from parts.learning_loop.reward_shaper import RewardShaper
    from parts.profit_tailgating_bot.tail_follow_conviction_model import (
        describe_follow_conviction,
    )

    shaper = RewardShaper(
        maximum_reward=10.0, risk_reference_fraction=0.02,
        horizon_half_life_seconds=86400.0, minimum_significance=1.0,
    )
    shaper.observe_usdt_result(VENUE, SYMBOL, 0, -100.0, 1.0)
    shaper.observe_peak_adverse_excursion(VENUE, SYMBOL, 0, 0.01)
    reward = shaper.shape(VENUE, SYMBOL, "scanner-continuation", 0, 60.0)
    assert reward.reward < 0

    model = a_conviction_model()
    model.note_uninterpretable_reward(
        f"{reward.detector} sent a shaped reward of {reward.reward!r}"
    )
    assert model.standing.rewards_uninterpretable == 1
    assert describe_follow_conviction(model)["rewards_uninterpretable"] == 1


def teach_the_conviction_model(model, rounds=200):
    for index in range(rounds):
        left = 0.03 if index % 2 else 0.001
        model.train(
            {
                "fraction_of_a_normal_move_done": 0.5,
                "move_remaining_fraction": left,
                "view_disagreement": 0.05,
            },
            was_profitable=left > 0.01,
            source=FROM_A_SCANNER_MOVE,
        )


def test_a_crowded_move_is_refused_before_the_model_sees_it():
    """Crowding inverts the setup rather than weakening it."""
    subject = a_conviction_model()
    teach_the_conviction_model(subject)
    conviction, outcome = subject.form_conviction(
        a_follow(), a_remaining(), a_crowding(state=CROWDED, tripped=(FROM_THE_BOOK,))
    )
    assert conviction is None
    assert outcome == IS_CROWDED


def test_unreadable_crowding_is_also_a_refusal():
    subject = a_conviction_model()
    teach_the_conviction_model(subject)
    outcome = subject.form_conviction(
        a_follow(), a_remaining(), a_crowding(state=CROWDING_NOT_MEASURED)
    )[1]
    assert outcome == CROWDING_UNKNOWN


def test_no_estimate_of_what_is_left_means_no_conviction():
    subject = a_conviction_model()
    outcome = subject.form_conviction(a_follow(), a_remaining(fraction=None), a_crowding())[1]
    assert outcome == NOTHING_LEFT


def test_disagreeing_views_can_be_configured_to_refuse():
    subject = a_conviction_model(refuse_on_disagreement=True)
    teach_the_conviction_model(subject)
    outcome = subject.form_conviction(
        a_follow(), a_remaining(agreement=ESTIMATES_DISAGREE), a_crowding()
    )[1]
    assert outcome == VIEWS_DISAGREE


def test_the_disagreement_between_views_is_its_own_feature():
    """A midpoint cannot tell 2%-agreed from 0.5%-to-4%."""
    subject = a_conviction_model()
    features = subject.features_for(
        a_follow(), a_remaining(fraction=0.02, lowest=0.005, highest=0.04), a_crowding()
    )
    assert features["view_disagreement"] == pytest.approx(0.875)
    assert features["move_remaining_fraction"] == 0.02


def test_the_model_learns_that_a_move_with_more_left_pays_more_often():
    subject = a_conviction_model(minimum_training=20, calibration_minimum=10_000)
    teach_the_conviction_model(subject, rounds=400)
    plenty = subject.form_conviction(a_follow(), a_remaining(fraction=0.03), a_crowding())[0]
    scraps = subject.form_conviction(a_follow(), a_remaining(fraction=0.001), a_crowding())[0]
    assert plenty.raw_probability > scraps.raw_probability


def test_calibration_is_kept_per_source():
    """The three ways of finding a move fail differently."""
    subject = a_conviction_model(calibration_minimum=50)
    for index in range(200):
        subject.observe_outcome(0.8, index % 5 == 0, FROM_A_SCANNER_MOVE)
        subject.observe_outcome(0.8, True, FROM_OUR_OWN_WINNER)
    teach_the_conviction_model(subject)
    scanner = subject.form_conviction(a_follow(source=FROM_A_SCANNER_MOVE), a_remaining(), a_crowding())[0]
    winner = subject.form_conviction(a_follow(source=FROM_OUR_OWN_WINNER), a_remaining(), a_crowding())[0]
    assert winner.probability > scanner.probability


def test_promotion_is_delivered_and_the_live_model_cannot_be_wiped():
    subject = a_conviction_model()
    teach_the_conviction_model(subject)
    subject.apply_champion_choice(CHALLENGER)
    assert subject.live_model_name == CHALLENGER
    with pytest.raises(ValueError):
        subject.apply_retrain_request(CHALLENGER)
    subject.apply_retrain_request(CHAMPION)
    assert subject.model(CHAMPION).observations == 0


# ---- tail-trailing-exit-planner ---------------------------------------------

def a_planner(trail_multiple=1.5, minimum_trail=0.002, tighten_after=0.02, tightened=0.5):
    return TailTrailingExitPlanner(
        trail_safety_multiple=trail_multiple, minimum_trail_fraction=minimum_trail,
        tighten_after_gain_fraction=tighten_after, tightened_trail_multiple=tightened,
        counterfactual_window=100, counterfactual_quantile=0.6, prior_trail_fraction=0.01,
    )


def a_prepared_planner(retracement=0.01, **kwargs):
    subject = a_planner(**kwargs)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_retracement_profile(
        RetracementProfile(venue_id=VENUE, symbol=SYMBOL,
                           normal_retracement_fraction=retracement,
                           moves_observed=100, is_fitted=True)
    )
    return subject


def test_the_plan_is_a_trail_and_names_no_target():
    """The whole bot: it joined a move whose size it cannot know."""
    plan, _ = a_prepared_planner().plan(a_follow(), a_remaining(fraction=0.03))
    assert len(plan.targets) == 1
    assert plan.targets[0].price == plan.stop_price
    assert plan.targets[0].fraction == 1.0
    assert "no target" in plan.reason


def test_the_trail_width_comes_from_what_this_symbol_retraces():
    plan, _ = a_prepared_planner(retracement=0.01, trail_multiple=2.0).plan(
        a_follow(), a_remaining(fraction=0.05)
    )
    assert plan.risk_fraction == pytest.approx(0.02)
    assert plan.stop_price == pytest.approx(98.0)


def test_a_symbol_with_no_retracement_record_gets_no_plan():
    subject = a_planner()
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    assert subject.plan(a_follow(), a_remaining())[1] == NO_EXCURSION_PROFILE


def test_a_trail_wider_than_the_move_has_left_is_refused():
    subject = a_prepared_planner(retracement=0.05, trail_multiple=2.0)
    assert subject.plan(a_follow(), a_remaining(fraction=0.01))[1] == TRAIL_WOULD_EXCEED_WHAT_IS_LEFT


def test_a_trail_only_ever_moves_in_the_trades_favour():
    """A trail that could loosen lets a losing move argue for more room."""
    subject = a_prepared_planner(retracement=0.01, tighten_after=10.0)
    subject.plan(a_follow(), a_remaining(fraction=0.05))
    first = subject.standing_trail(VENUE, SYMBOL)
    advanced = subject.advance_trail(VENUE, SYMBOL, LONG, price=110.0)
    assert advanced > first
    retreated = subject.advance_trail(VENUE, SYMBOL, LONG, price=90.0)
    assert retreated == advanced


def test_a_short_follow_trails_from_above():
    subject = a_prepared_planner(retracement=0.01, tighten_after=10.0)
    subject.plan(a_follow(direction=SHORT), a_remaining(fraction=0.05))
    assert subject.standing_trail(VENUE, SYMBOL) > 100.0
    advanced = subject.advance_trail(VENUE, SYMBOL, SHORT, price=90.0)
    assert advanced < subject._trail_price((VENUE, SYMBOL), SHORT, 100.0, 0.015)


def test_the_trail_tightens_once_the_follow_is_far_enough_ahead():
    subject = a_prepared_planner(retracement=0.01, tighten_after=0.05, tightened=0.5)
    subject.plan(a_follow(), a_remaining(fraction=0.20))
    subject.advance_trail(VENUE, SYMBOL, LONG, price=101.0)
    assert subject.standing.trails_tightened == 0
    subject.advance_trail(VENUE, SYMBOL, LONG, price=110.0)
    assert subject.standing.trails_tightened == 1


def a_tail_trail_counterfactual(trail_fraction=0.03, difference=0.05, rule_name=TAIL_TRAIL_RULE_NAME):
    return ExitCounterfactual(
        trade_id="t1", rule_name=rule_name, exit_price=99.0, realised_pnl=1.0,
        difference=difference, would_have_been_reachable=True, is_hindsight=True,
        reason="r", replayed_at_ns=1, venue_id=VENUE, symbol=SYMBOL,
        trail_fraction=trail_fraction,
    )


def test_the_counterfactual_widens_a_trail_that_kept_cutting_moves_short():
    """Without it a trailing stop is a rule nobody is checking."""
    subject = a_prepared_planner(retracement=0.01, trail_multiple=1.5)
    narrow, _ = subject.trail_width(VENUE, SYMBOL)
    for _ in range(20):
        # difference > 0 means this trail width would have beaten the actual
        # exit -- the actual trail was too tight.
        subject.observe_exit_counterfactual(a_tail_trail_counterfactual(trail_fraction=0.03, difference=0.05))
    wider, counterfactual = subject.trail_width(VENUE, SYMBOL)
    assert wider > narrow
    assert counterfactual.is_fitted


def test_observe_exit_counterfactual_only_learns_from_the_trailing_job():
    planner = a_planner()
    fixed_target = ExitCounterfactual(
        trade_id="t1", rule_name="tail-exit-plan:target-0", exit_price=101.0,
        realised_pnl=1.0, difference=5.0, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id=VENUE, symbol=SYMBOL, trail_fraction=None,
    )
    planner.observe_exit_counterfactual(fixed_target)
    assert planner.standing.counterfactuals_seen == 0

    trailing = ExitCounterfactual(
        trade_id="t1", rule_name=TAIL_TRAIL_RULE_NAME, exit_price=99.0,
        realised_pnl=1.0, difference=2.0, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id=VENUE, symbol=SYMBOL, trail_fraction=0.03,
    )
    planner.observe_exit_counterfactual(trailing)
    assert planner.standing.counterfactuals_seen == 1


def a_prepared_planner_with_an_open_position(retracement=0.01, tighten_after=10.0, **kwargs):
    """A planner that has already planned once, so _standing_trails[key] exists
    and advance_trail has something to ratchet."""
    subject = a_prepared_planner(retracement=retracement, tighten_after=tighten_after, **kwargs)
    subject.plan(a_follow(), a_remaining(fraction=0.05))
    return subject


def test_plan_does_not_reset_a_trail_that_has_already_advanced():
    planner = a_prepared_planner_with_an_open_position()
    key = (VENUE, SYMBOL)
    first_stop = planner._standing_trails[key]
    planner.advance_trail(VENUE, SYMBOL, LONG, price=first_stop * 1.05)
    advanced_stop = planner._standing_trails[key]
    assert advanced_stop > first_stop

    plan, _ = planner.plan(a_follow(), a_remaining())
    assert planner._standing_trails[key] == advanced_stop
    assert plan.stop_price == advanced_stop


def test_plans_risk_fraction_matches_the_ratcheted_stops_real_distance():
    """risk_fraction must describe stop_price's actual distance from price, not
    the freshly-measured retracement width -- stop_target_placer.py inverts
    stop_price/(1 -+ risk_fraction) to recover a reference price on the
    assumption that the two agree, which only holds if risk_fraction is the
    real distance rather than a number computed independently of stop_price."""
    planner = a_prepared_planner_with_an_open_position()
    key = (VENUE, SYMBOL)
    planner.advance_trail(VENUE, SYMBOL, LONG, price=planner._standing_trails[key] * 1.05)
    advanced_stop = planner._standing_trails[key]

    plan, _ = planner.plan(a_follow(), a_remaining())
    current_price = planner._prices[key].price
    assert plan.risk_fraction == pytest.approx((current_price - advanced_stop) / current_price)
    # The invariant stop_target_placer.py relies on: recovering a reference
    # price from stop_price and risk_fraction must round-trip.
    recovered = plan.stop_price / (1.0 - plan.risk_fraction)
    assert recovered == pytest.approx(current_price)


def test_apply_positions_and_prices_advances_a_held_positions_trail():
    from parts.profit_tailgating_bot.tail_trailing_exit_planner import _apply_positions_and_prices

    planner = a_prepared_planner_with_an_open_position()
    key = (VENUE, SYMBOL)
    first_stop = planner._standing_trails[key]

    class PositionStub:
        venue_id, symbol, direction, is_flat = VENUE, SYMBOL, LONG, False

    class TradeStub:
        venue_id, symbol, observed_at_ns = VENUE, SYMBOL, 2
        price = first_stop * 1.05

    _apply_positions_and_prices(planner, [PositionStub()], [TradeStub()])
    assert planner._standing_trails[key] > first_stop


def test_apply_positions_and_prices_forgets_a_flat_position():
    from parts.profit_tailgating_bot.tail_trailing_exit_planner import _apply_positions_and_prices

    planner = a_prepared_planner_with_an_open_position()
    key = (VENUE, SYMBOL)
    assert key in planner._standing_trails

    class FlatPositionStub:
        venue_id, symbol, is_flat = VENUE, SYMBOL, True

    _apply_positions_and_prices(planner, [FlatPositionStub()], [])
    assert key not in planner._standing_trails


def test_a_closed_follow_releases_its_trail():
    subject = a_prepared_planner()
    subject.plan(a_follow(), a_remaining(fraction=0.05))
    subject.forget_position(VENUE, SYMBOL)
    assert subject.standing_trail(VENUE, SYMBOL) is None


def test_a_trail_that_could_loosen_is_refused_at_construction():
    with pytest.raises(ValueError):
        TailTrailingExitPlanner(
            trail_safety_multiple=1.5, minimum_trail_fraction=0.002,
            tighten_after_gain_fraction=0.02, tightened_trail_multiple=1.5,
            counterfactual_window=100, counterfactual_quantile=0.6, prior_trail_fraction=0.01,
        )


# ---- tail-opinion-composer --------------------------------------------------

class CalibratedStub:
    def __init__(self, probability, side=LONG, measured=True,
                 model_is_trained=True, model_observations=1000):
        self.venue_id, self.symbol, self.side = VENUE, SYMBOL, side
        self.calibrated = an_estimate(probability, 200 if measured else 0, measured)
        self.probability = probability
        self.is_measured = measured
        self.model_observations = model_observations
        self.model_is_trained = model_is_trained
        self.reason = "conviction"


def a_tail_composer(margin=0.0, require_measured=False):
    # The plan's own break-even plus a margin, as for the bull and bear bots.
    return TailOpinionComposer(
        conviction_floor=ConvictionFloor(fee_rate=0.00055, margin=margin, fallback_reward_to_risk=1.5),
        require_trained_model=require_measured,
    )


def a_trail_plan():
    plan, _ = a_prepared_planner().plan(a_follow(), a_remaining(fraction=0.05))
    return plan


def test_a_complete_follow_becomes_one_opinion():
    opinion = a_tail_composer().compose(a_follow(), CalibratedStub(0.8), a_trail_plan())
    assert opinion.is_a_call_to_act
    assert opinion.action == ENTER_NOW
    assert opinion.features_summary["source"] == FROM_A_SCANNER_MOVE


def test_this_bot_can_never_propose_a_reversal():
    """Without this it is a third directional bot with none of the checks."""
    subject = a_tail_composer()
    opinion = subject.compose(a_follow(direction=LONG), CalibratedStub(0.9, side=SHORT), a_trail_plan())
    assert opinion.action == STAND_DOWN
    assert opinion.refusal == WOULD_BE_A_REVERSAL
    assert subject.standing.reversals_refused == 1


def test_a_plan_that_names_a_target_is_refused():
    """A target arriving from a replaced planner would make this a momentum bot."""
    subject = a_tail_composer()
    plan = a_trail_plan()
    with_target = type(plan)(
        **{
            **{field: getattr(plan, field) for field in plan.__dataclass_fields__},
            "targets": (ExitTarget(price=120.0, fraction=1.0, reason="a target"),),
        }
    )
    opinion = subject.compose(a_follow(), CalibratedStub(0.9), with_target)
    assert opinion.refusal == PLAN_NAMES_A_TARGET
    assert subject.standing.targets_refused == 1


def test_a_conviction_below_the_floor_and_a_missing_plan_both_stand_down():
    subject = a_tail_composer(margin=0.5)
    assert subject.compose(a_follow(), CalibratedStub(0.5), a_trail_plan()).action == STAND_DOWN
    assert subject.compose(a_follow(), CalibratedStub(0.9), None).refusal == NO_EXIT_PLAN


def test_a_bot_can_be_told_to_act_only_on_a_trained_model():
    """The gate tests the model, not the calibration.

    It tested the calibration until 2026-08-23, and calibration needs a scorecard,
    which needs closed trades, which need a trade -- so it could never pass. 653
    opinions were refused by it on the live run before this was found. An
    uncalibrated conviction from a trained model is now allowed through; one from
    a model that has never been trained is not, which is what RL-060 asks for.
    """
    composer = a_tail_composer(require_measured=True)
    untrained = composer.compose(
        a_follow(),
        CalibratedStub(0.9, measured=False, model_is_trained=False, model_observations=12),
        a_trail_plan(),
    )
    assert untrained.refusal is not None
    assert "12 outcome(s)" in untrained.reason

    # Trained but never calibrated: allowed, because calibration is what trading
    # produces rather than what it requires.
    trained = composer.compose(
        a_follow(),
        CalibratedStub(0.9, measured=False, model_is_trained=True, model_observations=1408),
        a_trail_plan(),
    )
    assert trained.refusal is None, trained.reason


# ---- tail-setup-weight-learner ----------------------------------------------

def a_tail_weight_learner(minimum=10, floor=0.1, cap=3.0):
    return TailSetupWeightLearner(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum, minimum_weight=floor, maximum_weight=cap,
    )


def test_the_three_sources_are_weighted_separately():
    """One weight over all three would average three unrelated problems."""
    subject = a_tail_weight_learner()
    for index in range(200):
        subject.observe_closed_follow(FROM_OUR_OWN_WINNER, DETECTOR, index % 5 != 0)
        subject.observe_closed_follow(FROM_A_TRACKED_TRADER, "copy:alice", index % 5 == 0)
    assert subject.weight_for(FROM_OUR_OWN_WINNER).weight > subject.weight_for(FROM_A_TRACKED_TRADER).weight


def test_a_detector_inside_a_working_source_can_still_be_discounted():
    subject = a_tail_weight_learner()
    for index in range(200):
        subject.observe_closed_follow(FROM_A_SCANNER_MOVE, "good-detector", True)
        subject.observe_closed_follow(FROM_A_SCANNER_MOVE, "poor-detector", False)
    source = subject.weight_for(FROM_A_SCANNER_MOVE)
    poor = subject.weight_for(FROM_A_SCANNER_MOVE, "poor-detector")
    assert poor.weight < source.weight
    assert "the profit-tailgating" not in poor.reason


def test_a_source_is_measured_against_the_others_not_against_itself():
    subject = a_tail_weight_learner()
    for index in range(300):
        subject.observe_closed_follow(FROM_A_SCANNER_MOVE, DETECTOR, index % 5 == 0)
    for _ in range(20):
        subject.observe_closed_follow(FROM_OUR_OWN_WINNER, DETECTOR, True)
    assert subject.weight_for(FROM_A_SCANNER_MOVE).weight < 1.0


def test_a_failing_source_is_floored_not_deleted():
    subject = a_tail_weight_learner(floor=0.1)
    for index in range(300):
        subject.observe_closed_follow(FROM_A_TRACKED_TRADER, "copy:bob", False)
        subject.observe_closed_follow(FROM_A_SCANNER_MOVE, DETECTOR, index % 2 == 0)
    weight = subject.weight_for(FROM_A_TRACKED_TRADER)
    assert weight.weight == 0.1
    assert "could clear it" in weight.reason


def test_a_restarted_learner_adopts_the_scorecard():
    scorecard = BotScorecard(bot="profit-tailgating-bot")
    for index in range(60):
        scorecard.record_closed_trade(
            f"{FROM_A_SCANNER_MOVE}:{DETECTOR}", "trending", 0.7, index % 3 != 0, 1.0
        )
    subject = a_tail_weight_learner()
    subject.observe_scorecard(scorecard)
    assert subject.weight_for(FROM_A_SCANNER_MOVE).trades_judged == 60


# ---- the crash that needed a candidate to become visible ---------------------
#
# start_part handed every candidate a literal None and plan() read
# `remaining.remaining_fraction` off it unconditionally, so the first
# follow-candidate tail-mover-qualifier ever produced crash-looped this part with
# AttributeError: 'NoneType' object has no attribute 'remaining_fraction'.
#
# It had never arrived before. The qualifier had seen 1,197 entry-candidates and
# qualified its first 3 on the day the scanner's vocabulary was widened -- the
# defect was written long before and only became reachable when something
# upstream started working.

def test_a_candidate_with_no_estimate_of_the_move_left_is_refused_not_crashed_on():
    """Refused and named rather than planned without the check.

    A tailgater joining a move already running has no target of its own, which is
    exactly why how much of the move is left is the one thing it must not guess
    at. tail-move-remaining-estimator publishes it; until 2026-08-26 nothing but
    the conviction model consumed it.
    """
    subject = a_prepared_planner()
    plan, reason = subject.plan(a_follow(), None)

    assert plan is None
    assert reason == NO_MOVE_REMAINING


def test_the_refusal_is_counted_so_a_bot_planning_nothing_is_visible():
    """A tailgater that refuses every candidate looks identical to a quiet market."""
    subject = a_prepared_planner()
    for _ in range(3):
        subject.plan(a_follow(), None)

    described = describe_trailing(subject)
    assert described["plans_requested"] == 3
    assert described["plans_built"] == 0
    assert described["refused_by_reason"][NO_MOVE_REMAINING] == 3


def test_an_estimate_that_is_present_still_plans():
    """The guard must not have made the ordinary path unreachable."""
    plan, reason = a_prepared_planner().plan(a_follow(), a_remaining(fraction=0.05))
    assert plan is not None, reason
