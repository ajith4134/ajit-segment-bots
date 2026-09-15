"""The learning loop: what the system is taught, and the ways teaching goes wrong.

Almost every test here is about a specific way a learning system quietly learns
the wrong thing: profit used as a label, a reward that pays for risk, a scorecard
crediting a bot for a trade the arbiter modified, a promotion made on the best of
twenty coin flips, a regret record that only counts missed profit.

Each of those produces a system that looks like it is learning and is not, so
each has a test that fails if the protection is removed.
"""

import importlib

import pytest

from parts.learning_loop.bot_scorekeeper import (
    CLUSTERED, FROM_A_COUNTERFACTUAL, FROM_A_TAKEN_TRADE, BotScorekeeper,
    evidence_weight_of,
)
from parts.learning_loop.champion_challenger_gate import (
    DID_NOT_BEAT_IT, HAS_FORGOTTEN, KEEP, NOTHING_TO_COMPARE, PROMOTE,
    ChampionChallengerGate,
)
from parts.learning_loop.champion_challenger_gate import ONLINE_MODEL_NAMES
from parts.learning_loop.champion_challenger_gate import (
    DOES_NOT_CLEAR_ITS_TRIALS as MODEL_DOES_NOT_CLEAR_ITS_TRIALS,
    NOT_REFUTATION_TESTED as MODEL_NOT_REFUTATION_TESTED,
    WAS_REFUTED as MODEL_WAS_REFUTED,
)
from parts.learning_loop.edge_graduation_gate import (
    COVERAGE_TOO_THIN, DECISION_QUALITY_TOO_LOW, EXPLORING, GRADUATED,
    RETURNED_TO_EXPLORATION, TOO_FEW_TRADES, EdgeGraduationGate,
)
from parts.learning_loop.edge_graduation_gate import (
    DOES_NOT_CLEAR_ITS_TRIALS as BOT_DOES_NOT_CLEAR_ITS_TRIALS,
    NOT_REFUTATION_TESTED as BOT_NOT_REFUTATION_TESTED,
    WAS_REFUTED as BOT_WAS_REFUTED,
)
from parts.learning_loop.exit_timing_learner import (
    EXITS_ARE_EARLY, EXITS_ARE_LATE, EXITS_ARE_TIMED, MEASURED as EXIT_MEASURED,
    NOT_MEASURED as EXIT_NOT_MEASURED, ExitTimingLearner,
)
from parts.learning_loop.feature_attribution_tracker import FeatureAttributionTracker
from parts.learning_loop.feature_reliability_scorer import (
    NOT_MEASURED as RELIABILITY_NOT_MEASURED, REGIME_CONDITIONAL, RELIABLE, UNRELIABLE,
    FeatureReliabilityScorer,
)
from parts.learning_loop.forecast_trust_learner import (
    NOT_MEASURED as TRUST_NOT_MEASURED, NOT_TRUSTED, TRUSTED, ForecastTrustLearner,
)
from parts.learning_loop.instruction_performance_tracker import (
    LIVE, REPLAY, InstructionPerformanceTracker,
)
from parts.learning_loop.label_builder import (
    ClosedTradeRecord, ExcursionRecord, LABELLED, LabelBuilder, NO_COST_ESTIMATE,
    NO_EXCURSION_RECORD,
)
from parts.learning_loop.model_registry import ModelRegistry, REGISTERED, REJECTED_DUPLICATE
from parts.learning_loop.regret_tracker import (
    MEASURED as REGRET_MEASURED, NOT_ACTED_ON, OVERRULED, RegretTracker,
)
from parts.learning_loop.retrain_scheduler import (
    ALREADY_RUNNING, BECAUSE_OF_CADENCE, BECAUSE_OF_DRIFT, BECAUSE_OF_FORGETTING,
    NOT_ENOUGH_NEW_LABELS, NO_DUTY_CYCLE, NO_REASON, SCHEDULED, RetrainScheduler,
)
from parts.learning_loop.reward_shaper import (
    FOR_RISK_TAKEN, FOR_TIME_HELD, NO_EXCURSION, NO_INR_STATEMENT, SHAPED, RewardShaper,
)
from parts.learning_loop.sample_weight_assigner import (
    FOR_AGE, FOR_A_BROKEN_REGIME, FOR_FILL_QUALITY, FOR_RARITY, SampleWeightAssigner,
)
from parts.learning_loop.slippage_learner import (
    LIMIT, MARKET, MEASURED as SLIPPAGE_MEASURED, NOT_MEASURED as SLIPPAGE_NOT_MEASURED,
    SlippageLearner,
)
from runtime.learning_types import (
    THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED, THE_SETUP_WAS_RIGHT, THE_SIZE_WAS_RIGHT,
    TrainingLabel,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "bot-scorekeeper": "parts.learning_loop.bot_scorekeeper",
    "feature-reliability-scorer": "parts.learning_loop.feature_reliability_scorer",
    "edge-graduation-gate": "parts.learning_loop.edge_graduation_gate",
    "instruction-performance-tracker": "parts.learning_loop.instruction_performance_tracker",
    "forecast-trust-learner": "parts.learning_loop.forecast_trust_learner",
    "slippage-learner": "parts.learning_loop.slippage_learner",
    "exit-timing-learner": "parts.learning_loop.exit_timing_learner",
    "label-builder": "parts.learning_loop.label_builder",
    "sample-weight-assigner": "parts.learning_loop.sample_weight_assigner",
    "retrain-scheduler": "parts.learning_loop.retrain_scheduler",
    "model-registry": "parts.learning_loop.model_registry",
    "champion-challenger-gate": "parts.learning_loop.champion_challenger_gate",
    "reward-shaper": "parts.learning_loop.reward_shaper",
    "feature-attribution-tracker": "parts.learning_loop.feature_attribution_tracker",
    "regret-tracker": "parts.learning_loop.regret_tracker",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
BULL, BEAR = "bull-bot", "bear-bot"
SECOND_NS = 1_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_seconds(self, seconds):
        self.now_ns += int(seconds * 1e9)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_learning_part_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- label-builder ----------------------------------------------------------

def a_label_builder(favourable=0.005, adverse_entry=0.01, capture=0.5, size_multiple=1.0):
    return LabelBuilder(
        favourable_threshold=favourable, adverse_entry_threshold=adverse_entry,
        exit_capture_threshold=capture, size_survival_multiple=size_multiple,
    )


def a_trade(entry=100.0, exit_=102.0, side="long", horizon=600.0, held=300.0, opened=0,
            stop_distance=0.0):
    return ClosedTradeRecord(
        venue_id=VENUE, symbol=SYMBOL, detector="a-detector", regime="trending", side=side,
        entry_price=entry, exit_price=exit_, quantity=1.0, opened_at_ns=opened,
        closed_at_ns=opened + int(held * 1e9), horizon_seconds=horizon,
        features={"price_z_score": 1.0},
        stop_distance_fraction=stop_distance,
    )


def a_prepared_builder(peak=0.08, adverse=0.005, cost=0.001, **kwargs):
    subject = a_label_builder(**kwargs)
    subject.observe_excursion(
        VENUE, SYMBOL, 0,
        ExcursionRecord(
            peak_favourable_fraction=peak, peak_adverse_fraction=adverse,
            seconds_to_peak_favourable=100.0, seconds_to_peak_adverse=10.0, observations=50,
        ),
    )
    subject.observe_cost_estimate(VENUE, SYMBOL, cost)
    return subject


def test_feature_lookup_at_ns_picks_opened_at_ns_when_calibration_key_is_empty():
    label = TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector="d", regime="r", labels={},
        horizon_seconds=1.0, seconds_to_resolve=1.0, resolved_within_horizon=True,
        features={}, built_at_ns=1, claimed_at_ns=0, opened_at_ns=999,
    )
    assert label.calibration_key == ""
    assert label.feature_lookup_at_ns == 999


def test_feature_lookup_at_ns_picks_claimed_at_ns_when_a_calibration_key_is_present():
    label = TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector="d", regime="r", labels={},
        horizon_seconds=1.0, seconds_to_resolve=1.0, resolved_within_horizon=True,
        features={}, built_at_ns=1, claimed_at_ns=555, opened_at_ns=0,
        calibration_key="a-detector:setup",
    )
    assert label.feature_lookup_at_ns == 555


def test_a_labels_own_opened_at_ns_survives_into_the_training_label():
    """The live bug (2026-08-30): a label built from a closed trade carries
    claimed_at_ns=0 by design (calibration_key is empty, not this), but
    bull/bear-conviction-model looked up claimed_at_ns unconditionally and so
    never matched a remembered vector for one -- neither model ever trained
    on a real closed trade. opened_at_ns is the field a reader must use
    instead when calibration_key is empty."""
    subject = a_label_builder()
    subject.observe_excursion(
        VENUE, SYMBOL, 12345,
        ExcursionRecord(
            peak_favourable_fraction=0.08, peak_adverse_fraction=0.005,
            seconds_to_peak_favourable=100.0, seconds_to_peak_adverse=10.0, observations=50,
        ),
    )
    subject.observe_cost_estimate(VENUE, SYMBOL, 0.001)
    label, _ = subject.build(a_trade(opened=12345))
    assert label.opened_at_ns == 12345
    assert label.claimed_at_ns == 0
    assert label.calibration_key == ""


def test_a_right_setup_with_a_bad_exit_is_labelled_as_both():
    """A model trained on profit alone cannot tell them apart."""
    subject = a_prepared_builder(peak=0.08, capture=0.5)
    label, _ = subject.build(a_trade(entry=100.0, exit_=100.5))
    assert label.label_for(THE_SETUP_WAS_RIGHT) is True
    assert label.label_for(THE_EXIT_WAS_TIMED) is False


def test_a_right_setup_entered_early_is_still_a_right_setup():
    subject = a_prepared_builder(peak=0.08, adverse=0.03, adverse_entry=0.01)
    label, _ = subject.build(a_trade(exit_=107.0))
    assert label.label_for(THE_SETUP_WAS_RIGHT) is True
    assert label.label_for(THE_ENTRY_WAS_TIMED) is False


def test_costs_are_subtracted_before_labelling():
    """A setup right only before fees is not right."""
    generous = a_prepared_builder(peak=0.004, cost=0.0001, favourable=0.002)
    expensive = a_prepared_builder(peak=0.004, cost=0.003, favourable=0.002)
    assert generous.build(a_trade(exit_=100.4))[0].label_for(THE_SETUP_WAS_RIGHT) is True
    assert expensive.build(a_trade(exit_=100.4))[0].label_for(THE_SETUP_WAS_RIGHT) is False


def test_a_trade_that_did_not_resolve_is_its_own_outcome():
    """Folding it into a win or a loss teaches holding or cutting for the wrong reason."""
    subject = a_prepared_builder()
    label, _ = subject.build(a_trade(horizon=100.0, held=500.0))
    assert label.resolved_within_horizon is False
    assert subject.standing.unresolved_within_horizon == 1


def test_no_excursion_record_means_no_label():
    subject = a_label_builder()
    subject.observe_cost_estimate(VENUE, SYMBOL, 0.001)
    assert subject.build(a_trade())[1] == NO_EXCURSION_RECORD


def test_no_cost_estimate_means_no_label():
    subject = a_label_builder()
    subject.observe_excursion(
        VENUE, SYMBOL, 0, ExcursionRecord(0.08, 0.005, 100.0, 10.0, 50)
    )
    assert subject.build(a_trade())[1] == NO_COST_ESTIMATE


def test_a_short_is_labelled_from_its_own_direction():
    subject = a_prepared_builder(peak=0.08)
    label, _ = subject.build(a_trade(entry=100.0, exit_=93.0, side="short"))
    assert label.label_for(THE_SETUP_WAS_RIGHT) is True


def test_direction_and_excursion_survive_into_the_training_label():
    """The live gap (2026-08-30): a closed-trade label carried no direction at
    all, so bull/bear-setup-weight-learner could not tell a symbol's short
    outcome from its long one without it -- both already computed here for the
    label's own components, just never carried out."""
    subject = a_prepared_builder(peak=0.08, adverse=0.02)
    label, _ = subject.build(a_trade(side="short"))
    assert label.direction == "short"
    assert label.best_favourable_fraction == 0.08
    assert label.worst_adverse_fraction == 0.02


def test_every_component_is_labelled_when_the_stop_distance_is_known():
    label, _ = a_prepared_builder().build(a_trade(stop_distance=0.02))
    assert label.is_complete
    assert set(label.labels) == {
        THE_SETUP_WAS_RIGHT, THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED, THE_SIZE_WAS_RIGHT
    }


def test_size_is_omitted_rather_than_assumed_right_with_no_stop_distance():
    """This test used to assert all four components always (2026-09-12).

    That was only ever true because `_stop_distance` read a field
    `ClosedTradeRecord` did not declare: the `getattr` default returned 0.0 for
    every trade, and the branch beneath it asserted the size was right. The
    suite passed, and the assertion it was making was that the defect was
    present. The size of a position with no known stop is not judgeable, and an
    unjudgeable component is left out.
    """
    builder = a_prepared_builder()
    label, _ = builder.build(a_trade(stop_distance=0.0))

    assert label.is_complete, "setup, entry and exit are still all judged"
    assert set(label.labels) == {
        THE_SETUP_WAS_RIGHT, THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED
    }
    assert label.label_for(THE_SIZE_WAS_RIGHT) is None
    assert builder.standing.size_not_judgeable == 1


# ---- sample-weight-assigner -------------------------------------------------

def an_assigner(half_life=86400.0, floor=0.05, cap=5.0, partial=0.3, broken=0.2,
                unresolved=0.5, rarity=1.0, clock=None):
    assigner = SampleWeightAssigner(
        half_life_seconds=half_life, minimum_weight=floor, maximum_weight=cap,
        partial_fill_multiple=partial, broken_regime_multiple=broken,
        unresolved_multiple=unresolved, rarity_power=rarity,
    )
    if clock is not None:
        assigner._now_ns = clock
    return assigner


def a_label(regime="trending", resolved=True, built_at_ns=0, labels=None):
    return TrainingLabel(
        venue_id=VENUE, symbol=SYMBOL, detector="a-detector", regime=regime,
        labels=labels or {THE_SETUP_WAS_RIGHT: True}, horizon_seconds=600.0,
        seconds_to_resolve=300.0, resolved_within_horizon=resolved, features={},
        built_at_ns=built_at_ns,
    )


def test_an_old_example_counts_less_and_does_not_fall_off_a_cliff():
    clock = Clock()
    subject = an_assigner(half_life=100.0, clock=clock)
    fresh = subject.assign(a_label(built_at_ns=clock())).weight
    clock.advance_seconds(100)
    halved = subject.assign(a_label(built_at_ns=clock() - 100 * SECOND_NS)).weight
    assert halved == pytest.approx(fresh * 0.5, rel=0.05)


def test_a_label_from_a_badly_filled_trade_teaches_less():
    clock = Clock()
    subject = an_assigner(partial=0.3, clock=clock)
    fresh = a_label(built_at_ns=clock())
    clean = subject.assign(fresh, fill_quality=1.0).weight
    slipped = subject.assign(fresh, fill_quality=0.0).weight
    assert slipped < clean
    assert FOR_FILL_QUALITY in subject.assign(fresh, fill_quality=0.0).reasons


def test_a_broken_regimes_examples_are_down_weighted_not_deleted():
    """They will matter again if that regime returns."""
    subject = an_assigner(broken=0.2, floor=0.01)
    subject.observe_regime_break("trending", True)
    weight = subject.assign(a_label(regime="trending"))
    assert weight.weight > 0
    assert FOR_A_BROKEN_REGIME in weight.reasons


def test_a_rare_class_is_lifted():
    """The rare class is where the learner has least data and most to learn."""
    clock = Clock()
    subject = an_assigner(rarity=1.0, clock=clock)
    for _ in range(90):
        subject.observe_class(THE_SETUP_WAS_RIGHT, True)
    for _ in range(10):
        subject.observe_class(THE_SETUP_WAS_RIGHT, False)
    common = subject.assign(
        a_label(labels={THE_SETUP_WAS_RIGHT: True}, built_at_ns=clock()),
        primary_component=THE_SETUP_WAS_RIGHT,
    ).weight
    rare = subject.assign(
        a_label(labels={THE_SETUP_WAS_RIGHT: False}, built_at_ns=clock()),
        primary_component=THE_SETUP_WAS_RIGHT,
    ).weight
    assert rare > common


def test_a_weight_is_never_zero():
    """Zero deletes the example, and deleting is irreversible."""
    clock = Clock()
    subject = an_assigner(half_life=1.0, floor=0.01, clock=clock)
    clock.advance_seconds(10_000)
    assert subject.assign(a_label(built_at_ns=0)).weight >= 0.01


def test_a_weight_is_capped():
    """An uncapped weight lets one badly labelled trade dominate a batch."""
    clock = Clock()
    subject = an_assigner(cap=2.0, rarity=5.0, clock=clock)
    for _ in range(999):
        subject.observe_class(THE_SETUP_WAS_RIGHT, True)
    subject.observe_class(THE_SETUP_WAS_RIGHT, False)
    weight = subject.assign(
        a_label(labels={THE_SETUP_WAS_RIGHT: False}, built_at_ns=clock()),
        primary_component=THE_SETUP_WAS_RIGHT,
    )
    assert weight.weight == 2.0
    assert weight.was_clamped


def test_a_floor_of_zero_is_refused_at_construction():
    with pytest.raises(ValueError):
        SampleWeightAssigner(
            half_life_seconds=86400.0, minimum_weight=0.0, maximum_weight=5.0,
            partial_fill_multiple=0.3, broken_regime_multiple=0.2,
            unresolved_multiple=0.5, rarity_power=1.0,
        )


# ---- bot-scorekeeper --------------------------------------------------------

def a_scorekeeper(minimum=10):
    return BotScorekeeper(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum,
    )


def test_an_opinion_the_arbiter_did_not_act_on_still_gets_a_record():
    """A bot whose good calls were overruled looks identical to one with none."""
    subject = a_scorekeeper()
    subject.record_opinion_outcome(
        BULL, "a-detector", "trending", 0.8, True, 0.02, source=FROM_A_COUNTERFACTUAL
    )
    assert subject.standing.from_counterfactuals == 1
    assert subject.scorecard_for(BULL).trades == 1


def test_ten_correlated_entries_are_one_bet():
    """Recording ten wins turns one lucky call into a track record."""
    subject = a_scorekeeper()
    for _ in range(10):
        subject.record_opinion_outcome(
            BULL, "a-detector", "trending", 0.8, True, 0.02, cluster_id="cluster-1"
        )
    assert subject.scorecard_for(BULL).trades == 1
    assert subject.standing.clustered_entries_collapsed == 9


def test_a_losing_trade_is_weighed_by_how_far_it_stood_from_noise_not_refused():
    """luck-skill-separator standardises the signed return, so a loss arrives negative.

    The scorekeeper refused any significance at or below zero, and on the first
    live losses of 2026-09-15 the part crash-looped on every one -- so no bot
    could learn that an opinion had been wrong. The sign is already carried by
    whether the opinion was right; the weight is the distance from noise.
    """
    from runtime.trade_decoding_types import OutcomeSignificance

    def assessed(standardised, measurable=True):
        return OutcomeSignificance(
            trade_id="t", realised=-1.0, expected_noise=0.02 if measurable else None,
            standardised=standardised, is_significant=False, is_measurable=measurable,
            sample_size=10, reason="", assessed_at_ns=1,
        )

    assert evidence_weight_of(assessed(-2.5)) == 2.5
    assert evidence_weight_of(assessed(1.5)) == 1.5
    assert evidence_weight_of(None) == 1.0
    assert evidence_weight_of(assessed(None, measurable=False)) == 1.0
    # A flat trade, or one on a symbol with no measured move, is not evidence either way.
    assert evidence_weight_of(assessed(0.0)) is None

    subject = a_scorekeeper(minimum=1)
    subject.record_opinion_outcome(
        BULL, "a-detector", "trending", 0.8, False, -0.02, significance=evidence_weight_of(assessed(-2.5))
    )
    assert subject.scorecard_for(BULL).trades == 1


def test_a_weak_outcome_counts_less_than_a_strong_one():
    subject = a_scorekeeper(minimum=1)
    for _ in range(5):
        subject.record_opinion_outcome(
            BULL, "a-detector", "trending", 0.8, True, 0.02, significance=3.0
        )
        subject.record_opinion_outcome(
            BEAR, "a-detector", "trending", 0.8, True, 0.02, significance=1.0
        )
    assert (
        subject.hit_rate(BULL, "a-detector", "trending").observations
        > subject.hit_rate(BEAR, "a-detector", "trending").observations
    )


def test_the_record_is_split_by_regime_and_detector():
    subject = a_scorekeeper(minimum=1)
    subject.record_opinion_outcome(BULL, "detector-a", "trending", 0.8, True, 0.02)
    subject.record_opinion_outcome(BULL, "detector-b", "reverting", 0.8, False, -0.02)
    assert subject.hit_rate(BULL, "detector-a", "trending").value > 0.5
    assert subject.hit_rate(BULL, "detector-b", "reverting").value < 0.5


def test_a_zero_significance_outcome_is_refused():
    subject = a_scorekeeper()
    with pytest.raises(ValueError):
        subject.record_opinion_outcome(
            BULL, "a-detector", "trending", 0.8, True, 0.02, significance=0.0
        )


# ---- instruction-performance-tracker ----------------------------------------

def a_performance_tracker(minimum=10, gap=0.001):
    return InstructionPerformanceTracker(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_trades=minimum, gap_threshold=gap,
    )


def test_an_instruction_that_wins_often_and_loses_big_is_seen():
    """A hit-rate-only tracker calls it the best one."""
    subject = a_performance_tracker(minimum=5)
    for index in range(10):
        if index < 7:
            subject.observe_trade("i-1", True, 0.001)
        else:
            subject.observe_trade("i-1", False, -0.01)
    scorecard = subject.score("i-1")
    assert scorecard.hit_rate.value > 0.5
    assert scorecard.expectancy < 0
    assert "would call an instruction that wins often and loses big the best one" in scorecard.reason


def test_the_live_replay_gap_is_the_measurement():
    subject = a_performance_tracker(minimum=5, gap=0.001)
    for _ in range(10):
        subject.observe_trade("i-1", True, 0.001, source=LIVE)
        subject.observe_trade("i-1", True, 0.01, source=REPLAY)
    scorecard = subject.score("i-1")
    assert scorecard.live_versus_replay_gap == pytest.approx(0.009)
    assert "optimistic in a way that will repeat" in scorecard.reason


def test_a_regime_break_restarts_the_since_break_count():
    """A record spanning a regime change is two records."""
    subject = a_performance_tracker(minimum=1)
    for _ in range(10):
        subject.observe_trade("i-1", True, 0.01)
    subject.observe_regime_break()
    subject.observe_trade("i-1", True, 0.01)
    assert subject._records["i-1"].trades_since_break == 1
    assert subject._records["i-1"].trades == 11


def test_the_tracker_retires_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["instruction-performance-tracker"]
    ).describe_instruction_performance(a_performance_tracker())["retires_anything"] is False


# ---- reward-shaper ----------------------------------------------------------

def a_shaper(maximum=10.0, risk_reference=0.02, horizon_half_life=86400.0, minimum_significance=1.0):
    return RewardShaper(
        maximum_reward=maximum, risk_reference_fraction=risk_reference,
        horizon_half_life_seconds=horizon_half_life,
        minimum_significance=minimum_significance,
    )


def a_prepared_shaper(inr=100.0, rate=1.0, adverse=0.01, **kwargs):
    subject = a_shaper(**kwargs)
    subject.observe_inr_result(VENUE, SYMBOL, 0, inr, rate)
    subject.observe_peak_adverse_excursion(VENUE, SYMBOL, 0, adverse)
    return subject


def test_a_losing_trade_does_not_take_the_scorekeeper_down():
    """The crash this replaced: 13 restarts on 2026-08-28, one per losing trade.

    A shaped reward is USDT times its components and is negative for a loss. It
    was passed straight in as a weight multiplier, which raises -- correctly, a
    non-positive weight would erase a bot's record -- inside the tick. The value
    is refused now and counted; falling over on ordinary traffic is not a
    response a part is allowed to have.
    """
    reward = a_prepared_shaper(inr=-100.0).shape(VENUE, SYMBOL, "momentum-burst", 0, 60.0)
    assert reward.reward < 0

    subject = a_scorekeeper()
    subject.note_uninterpretable_reward(
        f"{reward.detector} sent a shaped reward of {reward.reward!r} in state {reward.state!r}"
    )
    assert subject.standing.rewards_uninterpretable == 1
    assert subject.standing.last_uninterpretable_reward is not None


def test_a_refused_reward_leaves_every_bot_weighted_as_it_was():
    """Refusing must not become a silent rescale of its own."""
    from parts.learning_loop.bot_scorekeeper import describe_scorekeeping

    subject = a_scorekeeper(minimum=1)
    subject.record_opinion_outcome("bull-bot", "d", "trend", 0.7, True, 10.0)
    before = describe_scorekeeping(subject)["scorecards"]
    assert before, "the scorecard this compares must exist for the comparison to mean anything"

    subject.note_uninterpretable_reward("a shaped reward of -3.0")

    assert describe_scorekeeping(subject)["scorecards"] == before
    assert subject.standing.rewards_applied == 0


def test_the_same_profit_earned_with_more_risk_is_worth_less():
    """Profit alone teaches the system to take enormous risk."""
    safe = a_prepared_shaper(inr=100.0, adverse=0.005, risk_reference=0.01)
    risky = a_prepared_shaper(inr=100.0, adverse=0.40, risk_reference=0.01)
    assert (
        risky.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
        < safe.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    )


def test_holding_a_position_longer_is_not_free():
    """Time is capital that could not be used elsewhere."""
    subject = a_prepared_shaper(horizon_half_life=3600.0)
    quick = subject.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    slow = subject.shape(VENUE, SYMBOL, "d", 0, 86400.0).reward
    assert slow < quick
    assert FOR_TIME_HELD in subject.shape(VENUE, SYMBOL, "d", 0, 60.0).components


def test_a_result_indistinguishable_from_noise_teaches_less():
    subject = a_prepared_shaper(inr=1.0, minimum_significance=2.0, horizon_half_life=1e9)
    subject.observe_significance(VENUE, SYMBOL, 0, 0.5)
    discounted = subject.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    subject.observe_significance(VENUE, SYMBOL, 0, 3.0)
    full = subject.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    assert discounted < full


def test_a_result_that_came_from_the_market_teaches_the_setup_less():
    subject = a_prepared_shaper(inr=1.0, horizon_half_life=1e9)
    subject.observe_attribution(VENUE, SYMBOL, 0, 0.1)
    mostly_market = subject.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    subject.observe_attribution(VENUE, SYMBOL, 0, 1.0)
    mostly_setup = subject.shape(VENUE, SYMBOL, "d", 0, 60.0).reward
    assert mostly_market < mostly_setup


def test_the_reward_is_in_inr_and_carries_its_rate():
    """RL-028 and RL-029."""
    subject = a_prepared_shaper(inr=100.0, rate=1.0004)
    reward = subject.shape(VENUE, SYMBOL, "d", 0, 60.0)
    assert reward.conversion_rate == 1.0004
    assert reward.raw_inr == 100.0


def test_no_inr_result_means_no_reward():
    subject = a_shaper()
    subject.observe_peak_adverse_excursion(VENUE, SYMBOL, 0, 0.01)
    assert subject.shape(VENUE, SYMBOL, "d", 0, 60.0).state == NO_INR_STATEMENT


def test_no_excursion_record_means_no_reward():
    subject = a_shaper()
    subject.observe_inr_result(VENUE, SYMBOL, 0, 100.0, 1.0)
    assert subject.shape(VENUE, SYMBOL, "d", 0, 60.0).state == NO_EXCURSION


def test_the_reward_is_bounded():
    """The extraordinary trade is the one most likely mismeasured."""
    subject = a_prepared_shaper(inr=1_000_000.0, maximum=10.0, horizon_half_life=1e12)
    reward = subject.shape(VENUE, SYMBOL, "d", 0, 1.0)
    assert reward.reward == 10.0
    assert reward.was_bounded


def test_an_unbounded_reward_is_refused_at_construction():
    with pytest.raises(ValueError):
        RewardShaper(
            maximum_reward=0.0, risk_reference_fraction=0.02,
            horizon_half_life_seconds=86400.0, minimum_significance=1.0,
        )


# ---- edge-graduation-gate ---------------------------------------------------

def a_graduation_gate(hit_rate=0.55, quality=0.6, coverage=0.2, required=40):
    return EdgeGraduationGate(
        minimum_hit_rate=hit_rate, minimum_decision_quality=quality,
        minimum_coverage=coverage, default_required_trades=required,
    )


def a_qualified_bot(gate, bot=BULL, regime="trending", trades=50, wins=35):
    for index in range(trades):
        gate.observe_closed_trade(bot, regime, index < wins)
    gate.observe_required_sample_size(bot, regime, 40)
    gate.observe_decision_quality(bot, regime, 0.8)
    gate.observe_refutation_verdict(bot, "not-refuted")
    gate.observe_trial_verdict(bot, True)
    gate.observe_coverage(bot, regime, 0.5)
    return gate


def test_a_bot_meeting_every_condition_graduates():
    maturity = a_qualified_bot(a_graduation_gate()).judge(BULL, "trending")
    assert maturity.state == GRADUATED
    assert maturity.may_trade_live


def test_profit_alone_does_not_graduate_a_bot():
    """A bot that made money badly has not demonstrated anything repeatable."""
    gate = a_qualified_bot(a_graduation_gate(quality=0.9))
    maturity = gate.judge(BULL, "trending")
    assert maturity.state == EXPLORING
    assert DECISION_QUALITY_TOO_LOW in maturity.failing_conditions


def test_an_untested_edge_does_not_graduate():
    gate = a_graduation_gate()
    for index in range(50):
        gate.observe_closed_trade(BULL, "trending", index < 35)
    gate.observe_decision_quality(BULL, "trending", 0.8)
    gate.observe_trial_verdict(BULL, True)
    gate.observe_coverage(BULL, "trending", 0.5)
    assert BOT_NOT_REFUTATION_TESTED in gate.judge(BULL, "trending").failing_conditions


def test_the_best_of_two_hundred_coin_flips_does_not_graduate():
    gate = a_qualified_bot(a_graduation_gate())
    gate.observe_trial_verdict(BULL, False)
    assert BOT_DOES_NOT_CLEAR_ITS_TRIALS in gate.judge(BULL, "trending").failing_conditions


def test_thin_coverage_does_not_graduate():
    gate = a_qualified_bot(a_graduation_gate(coverage=0.9))
    assert COVERAGE_TOO_THIN in gate.judge(BULL, "trending").failing_conditions


def test_the_required_sample_size_comes_from_the_power_estimate():
    gate = a_graduation_gate(required=1000)
    a_qualified_bot(gate, trades=50, wins=35)
    gate.observe_required_sample_size(BULL, "trending", 200)
    maturity = gate.judge(BULL, "trending")
    assert maturity.required_trades == 200
    assert TOO_FEW_TRADES in maturity.failing_conditions


def test_graduation_is_reversible_rather_than_final():
    """Retirement would end the evidence, and a bot with no evidence cannot return."""
    gate = a_qualified_bot(a_graduation_gate())
    assert gate.judge(BULL, "trending").state == GRADUATED
    gate.observe_decision_quality(BULL, "trending", 0.1)
    back = gate.judge(BULL, "trending")
    assert back.state == RETURNED_TO_EXPLORATION
    assert gate.standing.returns_to_exploration == 1


def test_clustered_trades_count_once():
    gate = a_graduation_gate()
    for _ in range(10):
        gate.observe_closed_trade(BULL, "trending", True, cluster_id="c-1")
    assert gate.standing.clustered_trades_collapsed == 9


# ---- feature-reliability-scorer ---------------------------------------------

def a_reliability_scorer(extreme=1.5, minimum_extreme=10, threshold=0.6):
    return FeatureReliabilityScorer(
        extreme_deviation=extreme, minimum_extreme_observations=minimum_extreme,
        reliability_threshold=threshold, prior_reliability=0.5, prior_weight=4.0,
        half_life_observations=500, moments_half_life=500, minimum_moment_observations=5,
    )


def teach_feature(scorer, feature="a-feature", count=50):
    for index in range(count):
        scorer.observe_vector({feature: 1.0 if index % 2 else -1.0})


def test_a_feature_is_scored_only_where_it_was_extreme():
    """Most of a feature's information is in its tails."""
    subject = a_reliability_scorer(extreme=2.0, minimum_extreme=5)
    teach_feature(subject)
    for _ in range(20):
        subject.observe_outcome("a-feature", 0.0, "trending", True)
    assert subject.score("a-feature", "trending").state == RELIABILITY_NOT_MEASURED


def test_a_reliable_feature_is_named_reliable():
    subject = a_reliability_scorer(extreme=1.5, minimum_extreme=5, threshold=0.6)
    teach_feature(subject)
    for _ in range(20):
        subject.observe_outcome("a-feature", 10.0, "trending", True)
    assert subject.score("a-feature", "trending").state == RELIABLE


def test_a_feature_that_works_in_one_regime_is_regime_conditional_not_unreliable():
    """The fix is to condition on regime, not to drop it."""
    subject = a_reliability_scorer(extreme=1.5, minimum_extreme=5, threshold=0.6)
    teach_feature(subject)
    for _ in range(20):
        subject.observe_outcome("a-feature", 10.0, "trending", True)
        subject.observe_outcome("a-feature", 10.0, "chop", False)
    assert subject.score("a-feature", "chop").state == REGIME_CONDITIONAL


def test_availability_is_reported():
    """A reliable feature missing half the time is worth less than a weaker one always there."""
    subject = a_reliability_scorer(minimum_extreme=5)
    for index in range(100):
        subject.observe_vector({"always": 1.0} if index % 2 else {"always": -1.0, "sometimes": 1.0})
    assert subject.availability("always") == 1.0
    assert subject.availability("sometimes") == pytest.approx(0.5)


def test_a_threshold_of_zero_deviation_is_refused():
    with pytest.raises(ValueError):
        FeatureReliabilityScorer(
            extreme_deviation=0.0, minimum_extreme_observations=10, reliability_threshold=0.6,
            prior_reliability=0.5, prior_weight=4.0, half_life_observations=500,
            moments_half_life=500, minimum_moment_observations=5,
        )


# ---- forecast-trust-learner -------------------------------------------------

def a_trust_learner(minimum=10, threshold=0.55, maximum=0.9):
    return ForecastTrustLearner(
        minimum_follows=minimum, trust_threshold=threshold, maximum_trust=maximum,
        prior_accuracy=0.5, prior_weight=4.0, half_life_observations=500,
    )


def test_an_unmeasured_forecaster_has_zero_trust_not_average_trust():
    """Average is what lets an untested model into every decision."""
    trust = a_trust_learner(minimum=100).trust_in("ensemble", "trending")
    assert trust.trust == 0.0
    assert trust.state == TRUST_NOT_MEASURED


def test_a_forecaster_right_often_that_loses_money_is_not_trusted():
    """A hit rate alone cannot see this."""
    subject = a_trust_learner(minimum=5, threshold=0.5)
    for index in range(50):
        subject.observe_follow("ensemble", "trending", index % 10 != 0, -0.01)
    trust = subject.trust_in("ensemble", "trending")
    assert trust.state == NOT_TRUSTED


def test_trust_is_capped_so_no_forecaster_becomes_the_decision():
    subject = a_trust_learner(minimum=5, maximum=0.7)
    for _ in range(50):
        subject.observe_follow("ensemble", "trending", True, 0.01)
    trust = subject.trust_in("ensemble", "trending")
    assert trust.trust == 0.7
    assert trust.was_capped


def test_trust_is_conditioned_on_the_situation():
    subject = a_trust_learner(minimum=5, threshold=0.6)
    for _ in range(50):
        subject.observe_follow("ensemble", "trending", True, 0.01)
        subject.observe_follow("ensemble", "chop", False, -0.01)
    assert subject.trust_in("ensemble", "trending").is_trusted
    assert subject.trust_in("ensemble", "chop").is_trusted is False


def test_an_unbounded_trust_is_refused():
    with pytest.raises(ValueError):
        ForecastTrustLearner(
            minimum_follows=10, trust_threshold=0.55, maximum_trust=1.5, prior_accuracy=0.5,
            prior_weight=4.0, half_life_observations=500,
        )


# ---- slippage-learner -------------------------------------------------------

def a_slippage_learner(bands=(10_000.0, 100_000.0), minimum=10):
    return SlippageLearner(
        size_bands=bands, typical_quantile=0.5, tail_quantile=0.9, window=200,
        minimum_fills=minimum, prior_cost_fraction=0.001, prior_fill_rate=0.9,
        prior_weight=4.0, half_life_observations=500,
    )


def test_slippage_is_a_property_of_a_symbol_at_a_size():
    subject = a_slippage_learner(minimum=5)
    for _ in range(20):
        subject.observe_fill(VENUE, SYMBOL, 1_000.0, MARKET, "calm", 0.0002)
        subject.observe_fill(VENUE, SYMBOL, 500_000.0, MARKET, "calm", 0.01)
    small = subject.profile(VENUE, SYMBOL, 1_000.0, MARKET, "calm")
    large = subject.profile(VENUE, SYMBOL, 500_000.0, MARKET, "calm")
    assert large.typical_cost > small.typical_cost


def test_unfilled_orders_are_counted_so_patient_execution_is_not_free():
    subject = a_slippage_learner(minimum=5)
    for _ in range(20):
        subject.observe_fill(VENUE, SYMBOL, 1_000.0, LIMIT, "calm", 0.0)
    for _ in range(20):
        subject.observe_unfilled(VENUE, SYMBOL, 1_000.0, LIMIT, "calm")
    profile = subject.profile(VENUE, SYMBOL, 1_000.0, LIMIT, "calm")
    assert profile.unfilled_observed == 20
    assert profile.fill_rate.value < 0.9
    assert profile.expected_cost(include_unfilled_opportunity=0.01) > profile.typical_cost


def test_the_tail_is_reported_not_just_the_mean():
    """Sizing must be done against what happens when it goes badly."""
    subject = a_slippage_learner(minimum=5)
    for index in range(50):
        subject.observe_fill(
            VENUE, SYMBOL, 1_000.0, MARKET, "calm", 0.05 if index % 4 == 0 else 0.0001
        )
    profile = subject.profile(VENUE, SYMBOL, 1_000.0, MARKET, "calm")
    assert profile.tail_cost > profile.typical_cost * 10


def test_conditions_are_kept_apart():
    subject = a_slippage_learner(minimum=5)
    for _ in range(20):
        subject.observe_fill(VENUE, SYMBOL, 1_000.0, MARKET, "calm", 0.0001)
        subject.observe_fill(VENUE, SYMBOL, 1_000.0, MARKET, "fast", 0.005)
    assert (
        subject.profile(VENUE, SYMBOL, 1_000.0, MARKET, "fast").typical_cost
        > subject.profile(VENUE, SYMBOL, 1_000.0, MARKET, "calm").typical_cost
    )


def test_a_learner_with_no_size_bands_is_refused():
    with pytest.raises(ValueError):
        SlippageLearner(
            size_bands=(), typical_quantile=0.5, tail_quantile=0.9, window=200,
            minimum_fills=10, prior_cost_fraction=0.001, prior_fill_rate=0.9,
            prior_weight=4.0, half_life_observations=500,
        )


# ---- exit-timing-learner ----------------------------------------------------

def an_exit_learner(good_capture=0.6, minimum=5):
    return ExitTimingLearner(
        good_capture=good_capture, window=200, minimum_trades=minimum, prior_capture=0.5,
        prior_early_rate=0.5, prior_weight=4.0, half_life_observations=500,
    )


def test_a_good_edge_with_a_bad_exit_is_named_as_an_exit_problem():
    """Retiring the instruction would fix the wrong thing."""
    subject = an_exit_learner(good_capture=0.6, minimum=5)
    for _ in range(20):
        subject.observe_exit("i-1", "trending", realised=0.01, peak_favourable=0.08, exited_before_peak=True)
        subject.observe_counterfactual("i-1", "trending", realised=0.01, would_have_made=0.06)
    report = subject.report("i-1", "trending")
    assert report.verdict == EXITS_ARE_EARLY
    assert report.the_exit_is_the_problem


def test_a_low_capture_that_waiting_would_not_have_helped_is_not_an_exit_problem():
    """In a mean-reverting symbol the peak was unreachable."""
    subject = an_exit_learner(good_capture=0.9, minimum=5)
    for _ in range(20):
        subject.observe_exit("i-1", "trending", realised=0.01, peak_favourable=0.03, exited_before_peak=True)
        subject.observe_counterfactual("i-1", "trending", realised=0.01, would_have_made=0.005)
    assert subject.report("i-1", "trending").verdict == EXITS_ARE_TIMED


def test_a_trade_with_nothing_to_capture_is_excluded():
    """Counting it as zero capture makes every losing strategy look like an exit problem."""
    subject = an_exit_learner(minimum=5)
    for _ in range(20):
        subject.observe_exit("i-1", "trending", realised=-0.02, peak_favourable=0.0, exited_before_peak=False)
    report = subject.report("i-1", "trending")
    assert report.state == EXIT_NOT_MEASURED
    assert report.trades_excluded == 20


def test_late_exits_are_a_different_verdict_from_early_ones():
    subject = an_exit_learner(good_capture=0.8, minimum=5)
    for _ in range(20):
        subject.observe_exit("i-1", "trending", realised=0.01, peak_favourable=0.08, exited_before_peak=False)
        subject.observe_counterfactual("i-1", "trending", realised=0.01, would_have_made=0.05)
    assert subject.report("i-1", "trending").verdict == EXITS_ARE_LATE


def test_the_exit_writes_to_the_instructions_own_scorecard():
    subject = an_exit_learner(minimum=5)
    for _ in range(20):
        subject.observe_exit("i-1", "trending", 0.01, 0.08, True)
    assert subject.scorecard("i-1", "trending").instruction_id == "i-1"


# ---- retrain-scheduler ------------------------------------------------------

def a_retrain_scheduler(drift=100, forgetting=20, cadence=500, duty=0.2):
    return RetrainScheduler(
        labels_for_drift=drift, labels_for_forgetting=forgetting,
        labels_for_cadence=cadence, minimum_duty_cycle=duty,
    )


def test_drift_alone_does_not_trigger_a_retrain():
    """A retrain on drift alone fits the change, and the change is often noise."""
    subject = a_retrain_scheduler(drift=100)
    subject.observe_drift_alert("kronos", "accuracy fell")
    request = subject.consider("kronos")
    assert request.state == NOT_ENOUGH_NEW_LABELS
    assert request.because == BECAUSE_OF_DRIFT


def test_drift_with_enough_new_labels_schedules_a_retrain():
    subject = a_retrain_scheduler(drift=10, forgetting=5)
    subject.observe_drift_alert("kronos", "accuracy fell")
    for _ in range(20):
        subject.observe_label("kronos")
    assert subject.consider("kronos").state == SCHEDULED


def test_forgetting_needs_less_new_data_than_drift():
    """The fix is to replay what was lost rather than to learn something new."""
    subject = a_retrain_scheduler(drift=100, forgetting=5)
    subject.observe_forgetting_report("kronos", "the-crash")
    for _ in range(10):
        subject.observe_label("kronos")
    request = subject.consider("kronos")
    assert request.state == SCHEDULED
    assert request.because == BECAUSE_OF_FORGETTING


def test_a_model_retrains_on_cadence_without_a_crisis():
    """A model that only retrains on a crisis only ever learns from crises."""
    subject = a_retrain_scheduler(cadence=10)
    for _ in range(20):
        subject.observe_label("kronos")
    request = subject.consider("kronos")
    assert request.state == SCHEDULED
    assert request.because == BECAUSE_OF_CADENCE


def test_only_the_challenger_is_ever_retrained():
    """A retrain is never a live change and never has to be rushed."""
    subject = a_retrain_scheduler(cadence=1)
    subject.observe_label("kronos")
    request = subject.consider("kronos")
    assert request.role == "challenger"
    assert request.touches_the_live_model is False


def test_two_retrains_are_never_scheduled_at_once():
    subject = a_retrain_scheduler(cadence=1)
    subject.observe_label("kronos")
    subject.retrain_started("kronos")
    assert subject.consider("kronos").state == ALREADY_RUNNING


def test_the_duty_cycle_is_respected():
    subject = a_retrain_scheduler(cadence=1, duty=0.5)
    subject.observe_label("kronos")
    subject.observe_duty_cycle(0.1)
    assert subject.consider("kronos").state == NO_DUTY_CYCLE


def test_forgetting_needing_more_data_than_drift_is_refused():
    with pytest.raises(ValueError):
        RetrainScheduler(
            labels_for_drift=10, labels_for_forgetting=100, labels_for_cadence=500,
            minimum_duty_cycle=0.2,
        )


# ---- model-registry ---------------------------------------------------------

def a_registry():
    return ModelRegistry()


def test_a_version_is_immutable():
    """A registry that can be edited is one that will be."""
    subject = a_registry()
    subject.register("kronos", "v1", "champion", 1000, 0.6, "kronos-versions")
    assert subject.register("kronos", "v1", "champion", 1000, 0.9, "kronos-versions")[1] == REJECTED_DUPLICATE


def test_a_promotion_without_a_refutation_verdict_is_recorded_as_such():
    subject = a_registry()
    record, _ = subject.register("kronos", "v1", "champion", 1000, 0.6, "f", promoted=True)
    assert record.was_promoted_on_evidence is False
    assert "cannot be recorded as promoted on evidence" in record.reason
    assert subject.standing.promoted_without_a_verdict == 1


def test_lineage_makes_a_chain_of_small_overfits_legible():
    subject = a_registry()
    subject.register("kronos", "v1", "champion", 1000, 0.60, "f")
    subject.register("kronos", "v2", "champion", 1000, 0.61, "f", parent_version="v1")
    subject.register("kronos", "v3", "champion", 1000, 0.62, "f", parent_version="v2")
    assert subject.lineage("kronos", "v3") == ("v1", "v2", "v3")


def test_the_registry_can_say_whether_selection_added_anything():
    subject = a_registry()
    subject.observe_refutation_verdict("kronos", "not-refuted")
    subject.register("kronos", "v1", "champion", 1000, 0.6, "f", promoted=True)
    subject.register("kronos", "v2", "champion", 1000, 0.7, "f", parent_version="v1", promoted=True)
    subject.record_outcome("kronos", "v1", 100, 0.55, 1.0)
    subject.record_outcome("kronos", "v2", 100, 0.60, 5.0)
    improving, reason = subject.selection_has_added_something("kronos")
    assert improving is True
    assert "adding something" in reason


def test_selection_that_added_nothing_says_so():
    subject = a_registry()
    subject.observe_refutation_verdict("kronos", "not-refuted")
    subject.register("kronos", "v1", "champion", 1000, 0.6, "f", promoted=True)
    subject.register("kronos", "v2", "champion", 1000, 0.7, "f", parent_version="v1", promoted=True)
    subject.record_outcome("kronos", "v1", 100, 0.6, 5.0)
    subject.record_outcome("kronos", "v2", 100, 0.5, 1.0)
    improving, reason = subject.selection_has_added_something("kronos")
    assert improving is False
    assert "luckiest on the validation tail" in reason


def test_outcomes_are_appended_never_edited():
    subject = a_registry()
    subject.register("kronos", "v1", "champion", 1000, 0.6, "f")
    subject.record_outcome("kronos", "v1", 10, 0.5, 1.0)
    subject.record_outcome("kronos", "v1", 20, 0.6, 2.0)
    assert len(subject.outcomes_for("kronos", "v1")) == 2


# ---- champion-challenger-gate -----------------------------------------------

def a_champion_gate(improvement=0.01, recall=0.7):
    return ChampionChallengerGate(minimum_improvement=improvement, minimum_recall=recall)


def a_qualified_challenger(gate, champion_score=0.60, challenger_score=0.70):
    gate.observe_champion("kronos", "v1", champion_score)
    gate.observe_challenger("kronos", "v2", challenger_score)
    gate.observe_refutation_verdict("kronos", "not-refuted")
    gate.observe_trial_verdict("kronos", True)
    gate.observe_forgetting_report("kronos", 0.95)
    return gate


def test_a_challenger_meeting_every_condition_is_promoted():
    choice = a_qualified_challenger(a_champion_gate()).decide("kronos")
    assert choice.decision == PROMOTE
    assert choice.promotes


def test_a_tie_keeps_the_champion():
    """The incumbent has a live record and the challenger has a validation score."""
    gate = a_qualified_challenger(a_champion_gate(improvement=0.05), 0.60, 0.61)
    choice = gate.decide("kronos")
    assert choice.decision == KEEP
    assert DID_NOT_BEAT_IT in choice.failing_conditions


def test_the_best_of_twenty_challengers_does_not_promote():
    gate = a_qualified_challenger(a_champion_gate())
    gate.observe_trial_verdict("kronos", False)
    choice = gate.decide("kronos")
    assert MODEL_DOES_NOT_CLEAR_ITS_TRIALS in choice.failing_conditions
    assert "beats the champion by chance regularly" in choice.reason


def test_a_challenger_that_forgot_an_era_does_not_promote():
    """The era it forgot is by construction one the recent data does not test."""
    gate = a_qualified_challenger(a_champion_gate(recall=0.9))
    gate.observe_forgetting_report("kronos", 0.4)
    choice = gate.decide("kronos")
    assert HAS_FORGOTTEN in choice.failing_conditions


def test_an_untested_challenger_does_not_promote():
    gate = a_champion_gate()
    gate.observe_champion("kronos", "v1", 0.6)
    gate.observe_challenger("kronos", "v2", 0.9)
    gate.observe_trial_verdict("kronos", True)
    gate.observe_forgetting_report("kronos", 1.0)
    assert MODEL_NOT_REFUTATION_TESTED in gate.decide("kronos").failing_conditions


def test_a_refuted_challenger_does_not_promote():
    gate = a_qualified_challenger(a_champion_gate())
    gate.observe_refutation_verdict("kronos", "refuted")
    assert MODEL_WAS_REFUTED in gate.decide("kronos").failing_conditions


def test_a_promotion_is_undone_by_promoting_back():
    """Rather than by retraining from nothing."""
    gate = a_qualified_challenger(a_champion_gate())
    gate.decide("kronos")
    assert gate.live_version("kronos") == "v2"
    gate.revert("kronos")
    assert gate.live_version("kronos") == "v1"
    assert gate.standing.reversals == 1


def test_an_online_model_promotes_on_score_improvement_alone():
    """bull/bear-conviction-model have no discrete version for a refutation
    battery or a trial ledger to test -- promotion is score improvement alone."""
    model_name = ONLINE_MODEL_NAMES[0]
    gate = a_champion_gate(improvement=0.01)
    gate.observe_champion(model_name, "v3", -0.30)
    gate.observe_challenger(model_name, "v4", -0.20)
    choice = gate.decide(model_name)
    assert choice.decision == PROMOTE
    assert choice.failing_conditions == ()
    assert "promoted on score improvement alone" in choice.reason


def test_an_online_model_still_needs_to_beat_the_champion():
    model_name = ONLINE_MODEL_NAMES[0]
    gate = a_champion_gate(improvement=0.05)
    gate.observe_champion(model_name, "v3", -0.20)
    gate.observe_challenger(model_name, "v4", -0.19)
    choice = gate.decide(model_name)
    assert choice.decision == KEEP
    assert choice.failing_conditions == (DID_NOT_BEAT_IT,)


def test_a_non_online_model_is_unaffected_by_the_online_bypass():
    """The full four conditions still apply to a model with a discrete version."""
    assert "kronos" not in ONLINE_MODEL_NAMES
    gate = a_champion_gate()
    gate.observe_champion("kronos", "v1", 0.6)
    gate.observe_challenger("kronos", "v2", 0.9)
    gate.observe_trial_verdict("kronos", True)
    gate.observe_forgetting_report("kronos", 1.0)
    assert MODEL_NOT_REFUTATION_TESTED in gate.decide("kronos").failing_conditions


def test_nothing_to_compare_is_its_own_answer():
    assert a_champion_gate().decide("kronos").decision == NOTHING_TO_COMPARE


def test_a_zero_improvement_bar_is_refused():
    with pytest.raises(ValueError):
        ChampionChallengerGate(minimum_improvement=0.0, minimum_recall=0.7)


# ---- feature-attribution-tracker --------------------------------------------

class Belief:
    def __init__(self, contributions, probability=0.7):
        self.contributions = dict(contributions)
        self.probability = probability
        self.features_used = len(contributions)
        self.features_unusable = ()

    @property
    def strongest_reason(self):
        if not self.contributions:
            return None
        name = max(self.contributions, key=lambda key: abs(self.contributions[key]))
        return name, self.contributions[name]


class Conviction:
    def __init__(self, contributions, probability=0.7):
        self.venue_id, self.symbol = VENUE, SYMBOL
        self.belief = Belief(contributions, probability)


def an_attribution_tracker(minimum=5, recent=10):
    return FeatureAttributionTracker(
        half_life_observations=500, recent_kept=recent, minimum_observations=minimum
    )


def test_a_conviction_is_decomposed_into_what_moved_it():
    subject = an_attribution_tracker()
    attribution = subject.record(BULL, "v1", Conviction({"a": 0.8, "b": -0.2}))
    assert attribution.is_decomposable
    assert attribution.strongest[0] == "a"
    assert attribution.share_of("a") == pytest.approx(0.8)


def test_attribution_is_kept_per_model_version():
    """A retrained model is a different model."""
    subject = an_attribution_tracker(minimum=2)
    for _ in range(5):
        subject.record(BULL, "v1", Conviction({"a": 1.0}))
        subject.record(BULL, "v2", Conviction({"a": -1.0}))
    assert subject.summary(BULL, "v1", "a").mean_contribution > 0
    assert subject.summary(BULL, "v2", "a").mean_contribution < 0


def test_the_two_bots_are_tracked_separately():
    """The same feature can be one bot's strongest signal and the other's noise."""
    subject = an_attribution_tracker(minimum=2)
    for _ in range(5):
        subject.record(BULL, "v1", Conviction({"a": 1.0}))
        subject.record(BEAR, "v1", Conviction({"a": 0.01}))
    assert (
        subject.summary(BULL, "v1", "a").mean_contribution
        > subject.summary(BEAR, "v1", "a").mean_contribution
    )


def test_attribution_is_not_importance():
    subject = an_attribution_tracker(minimum=2)
    for _ in range(5):
        subject.record(BULL, "v1", Conviction({"a": 1.0}))
    assert "not whether moving it was right" in subject.summary(BULL, "v1", "a").reason


def test_the_tracker_is_bounded():
    subject = an_attribution_tracker(recent=5)
    for index in range(50):
        subject.record(BULL, "v1", Conviction({"a": float(index)}))
    assert len(subject.recent) == 5


# ---- regret-tracker ---------------------------------------------------------

def a_regret_tracker(minimum=5, cost=0.001):
    return RegretTracker(window=200, minimum_observations=minimum, cost_fraction=cost)


def test_a_bot_right_about_trades_nobody_took_accumulates_regret():
    """Without it, that bot looks identical to one right about nothing."""
    subject = a_regret_tracker(minimum=5, cost=0.0)
    for _ in range(20):
        subject.record(BEAR, "trending", OVERRULED, what_was_done=0.0, what_it_wanted=0.02)
    regret = subject.regret_for(BEAR, "trending")
    assert regret.was_ignored_wrongly
    assert regret.total_regret > 0


def test_negative_regret_says_the_refusals_were_right():
    """A tracker recording only missed profit would push toward taking everything."""
    subject = a_regret_tracker(minimum=5, cost=0.0)
    for _ in range(20):
        subject.record(BEAR, "trending", NOT_ACTED_ON, what_was_done=0.0, what_it_wanted=-0.02)
    regret = subject.regret_for(BEAR, "trending")
    assert regret.refusals_were_right
    assert "the half a missed-profit-only tracker would never record" in regret.reason


def test_costs_are_charged_to_the_alternative():
    """Otherwise every untaken trade looks better and the system blames the desk."""
    free = a_regret_tracker(cost=0.0)
    charged = a_regret_tracker(cost=0.01)
    assert charged.record(BEAR, "trending", OVERRULED, 0.0, 0.005) < free.record(
        BEAR, "trending", OVERRULED, 0.0, 0.005
    )


def test_regret_is_kept_per_regime():
    subject = a_regret_tracker(minimum=5, cost=0.0)
    for _ in range(20):
        subject.record(BEAR, "trending", OVERRULED, 0.0, 0.02)
        subject.record(BEAR, "chop", OVERRULED, 0.0, -0.02)
    assert subject.regret_for(BEAR, "trending").was_ignored_wrongly
    assert subject.regret_for(BEAR, "chop").refusals_were_right


def test_the_tracker_does_not_measure_hindsight_regret():
    """It is unbounded, always large, and teaches only that the system is not omniscient."""
    assert importlib.import_module(
        BLOCK_PARTS["regret-tracker"]
    ).describe_regret(a_regret_tracker())["measures_hindsight_regret"] is False
