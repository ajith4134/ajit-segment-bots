"""The hypothesis block: turning what closed trades taught into instructions.

This block is where a system convinces itself of things. Every part in it has one
job and one specific failure available -- a search that counts only its winners, a
mutation that starts a fresh family, a hypothesis with nothing that could refute
it, an instruction with no way to retire -- and each of those produces a system
that gets more confident without getting more right.

The tests are mostly about the counting: trials counted before judgement,
mutations counted in the parent's family, formulas counted whether or not
anything came of them.
"""

import importlib

import pytest

from parts.hypothesis.expectancy_decomposer import ExpectancyDecomposer, TradeContribution
from parts.hypothesis.hypothesis_deduplicator import (
    AN_EXACT_DUPLICATE, A_REGIME_VARIANT, A_TUNED_DUPLICATE, HypothesisDeduplicator,
    HypothesisShape, NOVEL,
)
from parts.hypothesis.hypothesis_falsifier import (
    HypothesisFalsifier, NOT_OBSERVABLE, NOT_YET_DECIDABLE, NO_SAMPLE_SIZE, REFUTED,
    STANDING, UNREACHABLE, WRITTEN,
)
from parts.hypothesis.hypothesis_mutator import (
    ADD_A_REGIME_CONDITION, ALREADY_MUTATED, CHANGE_THE_HORIZON, FAMILY_IS_EXHAUSTED,
    HypothesisMutator, MOVE_THE_THRESHOLD, MUTATED, NOT_A_MUTATION, ParentInstruction,
)
from parts.hypothesis.hypothesis_ranker import (
    HypothesisRanker, NOT_ENOUGH_INPUTS, RANKED, UNREACHABLE as RANK_UNREACHABLE,
)
from parts.hypothesis.hypothesis_regime_tagger import (
    HypothesisRegimeTagger, REGIME_INDEPENDENT, TAGGED_FOR_ONE, TAGGED_FOR_SEVERAL, UNTAGGED,
)
from parts.hypothesis.instruction_retirer import (
    CRITERION_FIRED, EDGE_DECAYED, InstructionRetirer, ONLY_WORKS_IN_REPLAY,
    REGIME_BROKE, RESURRECTED, RETIRED, STANDING as INSTRUCTION_STANDING,
)
from parts.hypothesis.instruction_writer import (
    DOES_NOT_CLEAR_ITS_TRIALS, EDGE_DIES_BEFORE_IT_IS_CONFIRMED, InstructionWriter,
    NOT_NOVEL, NOT_REFUTATION_TESTED, NO_FALSIFICATION_CRITERION, NO_MEASUREMENT,
    NO_REGIME_TAG, NO_SAMPLE_SIZE as WRITER_NO_SAMPLE_SIZE, SAMPLE_UNREACHABLE, WAS_REFUTED,
)
from parts.hypothesis.loss_inverter import (
    INVERTED, LossCause, LossInverter, LOST_TO_COSTS, LOST_TO_EXECUTION, LOST_TO_NOISE,
    LOST_TO_TIMING, NOT_INVERTIBLE, NOT_SYSTEMATIC_ENOUGH, WRONG_ON_DIRECTION,
)
from parts.hypothesis.power_estimator import (
    ESTIMATED, NO_EFFECT_CLAIMED, PowerEstimator, UNTESTABLE_HERE,
)
from parts.hypothesis.symbolic_hypothesis_miner import (
    ABOVE, BELOW, MINED, NO_HELD_OUT_DATA, SUSPECT_PERFECT_FIT, SymbolicHypothesisMiner,
    TOO_LITTLE_DATA, Term,
)
from runtime.learning_types import (
    FROM_COSTS, FROM_ENTRY_TIMING, FROM_EXIT_TIMING, FROM_THE_SETUP, Hypothesis,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "expectancy-decomposer": "parts.hypothesis.expectancy_decomposer",
    "instruction-writer": "parts.hypothesis.instruction_writer",
    "loss-inverter": "parts.hypothesis.loss_inverter",
    "hypothesis-ranker": "parts.hypothesis.hypothesis_ranker",
    "instruction-retirer": "parts.hypothesis.instruction_retirer",
    "symbolic-hypothesis-miner": "parts.hypothesis.symbolic_hypothesis_miner",
    "hypothesis-deduplicator": "parts.hypothesis.hypothesis_deduplicator",
    "hypothesis-falsifier": "parts.hypothesis.hypothesis_falsifier",
    "power-estimator": "parts.hypothesis.power_estimator",
    "hypothesis-mutator": "parts.hypothesis.hypothesis_mutator",
    "hypothesis-regime-tagger": "parts.hypothesis.hypothesis_regime_tagger",
}

SECOND_NS = 1_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_hypothesis_part_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- expectancy-decomposer --------------------------------------------------

def a_decomposer(minimum=5):
    return ExpectancyDecomposer(
        minimum_trades=minimum, prior_win_rate=0.5, prior_weight=4.0,
        half_life_observations=500,
    )


def a_contribution(realised=0.01, peak=0.05, entry_slippage=0.0, costs=0.001, drift=0.0):
    return TradeContribution(
        detector="a-detector", regime="trending", realised=realised, peak_favourable=peak,
        entry_slippage=entry_slippage, costs=costs, market_drift=drift,
    )


def test_a_good_setup_with_bad_exits_is_visible_as_both():
    """Expectancy alone says only that it loses."""
    subject = a_decomposer(minimum=3)
    for _ in range(10):
        subject.observe_trade(a_contribution(realised=-0.002, peak=0.05, costs=0.001))
    breakdown = subject.decompose("a-detector", "trending")
    assert breakdown.by_component[FROM_THE_SETUP] > 0
    assert breakdown.by_component[FROM_EXIT_TIMING] < 0
    assert breakdown.total_expectancy < 0


def test_the_largest_drag_is_named_so_the_fix_goes_to_the_right_place():
    subject = a_decomposer(minimum=3)
    for _ in range(10):
        subject.observe_trade(a_contribution(realised=-0.002, peak=0.05, costs=0.001))
    breakdown = subject.decompose("a-detector", "trending")
    assert breakdown.largest_drag[0] == FROM_EXIT_TIMING
    assert breakdown.would_be_profitable_without_its_worst_component


def test_costs_are_never_netted_into_another_component():
    """Netting them into the setup makes an expensive strategy look like a bad one."""
    subject = a_decomposer(minimum=3)
    for _ in range(10):
        subject.observe_trade(a_contribution(realised=0.001, peak=0.01, costs=0.005))
    breakdown = subject.decompose("a-detector", "trending")
    assert breakdown.by_component[FROM_COSTS] == pytest.approx(-0.005)
    assert breakdown.by_component[FROM_THE_SETUP] > 0


def test_entry_slippage_appears_as_its_own_component():
    subject = a_decomposer(minimum=3)
    for _ in range(10):
        subject.observe_trade(a_contribution(entry_slippage=0.002))
    assert subject.decompose("a-detector", "trending").by_component[FROM_ENTRY_TIMING] < 0


def test_a_detector_with_no_trades_is_unmeasured():
    assert a_decomposer().decompose("nothing", "trending").is_measured is False


# ---- loss-inverter ----------------------------------------------------------

def an_inverter(minimum=10, wrong_rate=0.6, base_rate=0.5):
    return LossInverter(
        minimum_trades=minimum, minimum_wrong_rate=wrong_rate, base_rate=base_rate,
        prior_weight=4.0, half_life_observations=500,
    )


def a_loss_cause(cause=WRONG_ON_DIRECTION, instruction_id="i-1"):
    return LossCause(
        instruction_id=instruction_id, detector="a-detector", regime="trending", cause=cause,
        trades=100, losses=70, average_loss=-0.02, evidence={},
    )


def test_a_systematically_wrong_direction_inverts():
    """If a setup reliably loses, something reliably profits from it."""
    subject = an_inverter(minimum=5, wrong_rate=0.6)
    for index in range(50):
        subject.observe_trade("i-1", index % 10 != 0)
    hypothesis, outcome = subject.invert(a_loss_cause())
    assert outcome == INVERTED
    assert hypothesis.is_testable
    assert "opposite side" in hypothesis.statement


def test_a_loss_to_costs_does_not_invert():
    """Both sides pay the spread."""
    subject = an_inverter()
    assert subject.invert(a_loss_cause(cause=LOST_TO_COSTS))[1] == NOT_INVERTIBLE
    assert "both sides pay the spread" in subject.what_it_needs_instead(LOST_TO_COSTS)


def test_a_loss_to_noise_does_not_invert():
    """Inverting noise produces two instructions instead of none."""
    subject = an_inverter()
    assert subject.invert(a_loss_cause(cause=LOST_TO_NOISE))[1] == NOT_INVERTIBLE
    assert "no edge in either direction" in subject.what_it_needs_instead(LOST_TO_NOISE)


def test_a_loss_to_execution_does_not_invert():
    """Inverting it trades against a setup that was right."""
    subject = an_inverter()
    assert subject.invert(a_loss_cause(cause=LOST_TO_EXECUTION))[1] == NOT_INVERTIBLE


def test_a_loss_to_timing_does_not_invert():
    subject = an_inverter()
    assert subject.invert(a_loss_cause(cause=LOST_TO_TIMING))[1] == NOT_INVERTIBLE
    assert "discards a working setup" in subject.what_it_needs_instead(LOST_TO_TIMING)


def test_being_wrong_at_the_base_rate_is_not_information():
    subject = an_inverter(minimum=5, wrong_rate=0.7)
    for index in range(50):
        subject.observe_trade("i-1", index % 2 == 0)
    assert subject.invert(a_loss_cause())[1] == NOT_SYSTEMATIC_ENOUGH


def test_an_inverted_hypothesis_is_still_a_hypothesis():
    """A claim that skipped testing because its origin was a real loss is still untested."""
    subject = an_inverter(minimum=5)
    for _ in range(50):
        subject.observe_trade("i-1", True)
    hypothesis, _ = subject.invert(a_loss_cause())
    assert hypothesis.what_would_refute_it
    assert hypothesis.required_sample_size is None


def test_a_wrong_rate_below_half_is_refused_at_construction():
    with pytest.raises(ValueError):
        LossInverter(
            minimum_trades=10, minimum_wrong_rate=0.3, base_rate=0.5, prior_weight=4.0,
            half_life_observations=500,
        )


# ---- power-estimator --------------------------------------------------------

def a_power_estimator(power=0.8, maximum=100_000):
    return PowerEstimator(power=power, maximum_testable_trades=maximum)


def test_the_sample_scales_with_the_inverse_square_of_the_effect():
    """A 2-point claim costs far more than a 10-point one."""
    subject = a_power_estimator()
    big = subject.trades_for(0.60, 0.50, 0.05)
    small = subject.trades_for(0.52, 0.50, 0.05)
    assert small > big * 20


def test_a_corrected_significance_needs_a_larger_sample():
    """Estimating at an uncorrected level understates every sample size."""
    subject = a_power_estimator()
    uncorrected = subject.trades_for(0.55, 0.50, 0.05)
    corrected = subject.trades_for(0.55, 0.50, 0.00025)
    assert corrected > uncorrected


def test_a_claim_of_no_effect_has_no_sample_size():
    result = a_power_estimator().estimate("h-1", 0.50, 0.50, 0.05)
    assert result.state == NO_EFFECT_CLAIMED
    assert result.trades_required is None


def test_an_unreachable_sample_is_reported_as_untestable_not_as_a_long_wait():
    """Queueing it is deciding never to answer it."""
    subject = a_power_estimator(maximum=1000)
    result = subject.estimate("h-1", 0.505, 0.50, 0.05)
    assert result.state == UNTESTABLE_HERE
    assert "quietly decided never to answer" in result.reason


def test_an_unlisted_significance_rounds_toward_the_stricter_level():
    """So an unlisted alpha never produces a smaller sample than it should."""
    subject = a_power_estimator()
    assert subject.z_for(0.03) >= subject.z_for(0.05)


def test_trades_remaining_counts_down():
    result = a_power_estimator().estimate("h-1", 0.60, 0.50, 0.05)
    assert result.trades_remaining(0) == result.trades_required
    assert result.trades_remaining(result.trades_required) == 0


def test_an_arbitrary_power_is_refused():
    with pytest.raises(ValueError):
        PowerEstimator(power=0.83, maximum_testable_trades=1000)


# ---- hypothesis-falsifier ---------------------------------------------------

def a_falsifier(maximum=100_000):
    return HypothesisFalsifier(maximum_reachable_trades=maximum)


def test_a_criterion_nothing_measures_is_rejected():
    """It could never fire, and the hypothesis would read as unrefuted forever."""
    criterion = a_falsifier().write("h-1", "the-regime-was-unfavourable", "at-or-below", 0.5, 100)
    assert criterion.state == NOT_OBSERVABLE
    assert criterion.is_usable is False


def test_a_criterion_with_no_sample_size_is_rejected():
    """It is satisfied by whichever direction the first few trades went."""
    assert a_falsifier().write("h-1", "hit-rate", "at-or-below", 0.5, 0).state == NO_SAMPLE_SIZE


def test_an_unreachable_criterion_is_rejected():
    subject = a_falsifier(maximum=1000)
    assert subject.write("h-1", "hit-rate", "at-or-below", 0.5, 100_000).state == UNREACHABLE


def test_a_criterion_written_after_trades_says_so():
    """Worth knowing when it is evaluated."""
    subject = a_falsifier()
    criterion = subject.write("h-1", "hit-rate", "at-or-below", 0.5, 100, trades_already_taken=40)
    assert criterion.written_before_any_trade is False
    assert "after 40 trade(s)" in criterion.reason


def test_a_criterion_fires_when_it_is_met():
    """A falsifier that only writes criteria is a formality."""
    subject = a_falsifier()
    subject.write("h-1", "hit-rate", "at-or-below", 0.5, 100)
    verdict, reason = subject.evaluate("h-1", observed=0.42, trades=150)
    assert verdict == REFUTED
    assert "can be retired" in reason


def test_a_criterion_not_yet_reached_is_undecidable_rather_than_standing():
    subject = a_falsifier()
    subject.write("h-1", "hit-rate", "at-or-below", 0.5, 100)
    assert subject.evaluate("h-1", observed=0.42, trades=10)[0] == NOT_YET_DECIDABLE


def test_standing_is_not_the_same_as_demonstrated():
    subject = a_falsifier()
    subject.write("h-1", "hit-rate", "at-or-below", 0.5, 100)
    verdict, reason = subject.evaluate("h-1", observed=0.65, trades=150)
    assert verdict == STANDING
    assert "not the same as demonstrated" in reason


def test_a_hypothesis_with_no_criterion_cannot_be_refuted_at_all():
    verdict, reason = a_falsifier().evaluate("never-written", 0.1, 1000)
    assert verdict == NOT_YET_DECIDABLE
    assert "the state this part exists to prevent" in reason


# ---- hypothesis-deduplicator ------------------------------------------------

def a_deduplicator(threshold_tolerance=0.1, overlap_tolerance=0.8):
    return HypothesisDeduplicator(
        threshold_tolerance=threshold_tolerance, overlap_tolerance=overlap_tolerance
    )


def a_shape(measurement="z_score", comparison="below", threshold=-2.0, direction="long", regime=None):
    return HypothesisShape(
        measurement=measurement, comparison=comparison, threshold=threshold,
        direction=direction, regime=regime,
    )


def test_two_formulas_firing_on_the_same_ticks_are_one_hypothesis():
    """Counting them separately produces the appearance of independent confirmation."""
    subject = a_deduplicator(threshold_tolerance=0.01, overlap_tolerance=0.7)
    subject.remember("h-1", a_shape(threshold=-2.0), fires_on=set(range(1, 9)))
    # A far-apart threshold, so only the firing overlap can catch this.
    score = subject.score("h-2", a_shape(threshold=-9.0), fires_on=set(range(1, 8)) | {99})
    assert score.state == AN_EXACT_DUPLICATE
    assert score.fires_on_the_same_ticks_as == "h-1"


def test_a_tuned_threshold_is_the_same_hypothesis():
    """The commonest way a search defeats its own correction from inside."""
    subject = a_deduplicator(threshold_tolerance=0.5)
    subject.remember("h-1", a_shape(threshold=-2.0))
    score = subject.score("h-2", a_shape(threshold=-2.2))
    assert score.state == A_TUNED_DUPLICATE
    assert score.nearest_existing == "h-1"


def test_the_same_hypothesis_in_a_different_regime_is_genuinely_new():
    """A claim conditioned on a regime is a different claim."""
    subject = a_deduplicator()
    subject.remember("h-1", a_shape(regime="trending"))
    score = subject.score("h-2", a_shape(regime="chop"))
    assert score.state == A_REGIME_VARIANT
    assert score.novelty > 0.5


def test_a_genuinely_new_measurement_scores_full_novelty():
    subject = a_deduplicator()
    subject.remember("h-1", a_shape(measurement="z_score"))
    assert subject.score("h-2", a_shape(measurement="funding_rate")).state == NOVEL


def test_novelty_is_a_score_rather_than_a_refusal():
    """A near-duplicate is worth testing when the original has retired."""
    subject = a_deduplicator(threshold_tolerance=1.0)
    subject.remember("h-1", a_shape(threshold=-2.0))
    score = subject.score("h-2", a_shape(threshold=-2.9))
    assert score.state == A_TUNED_DUPLICATE
    assert 0.0 < score.novelty <= 1.0


def test_everything_ever_proposed_is_remembered():
    """A short memory reproposes last month's ideas and counts each as new."""
    subject = a_deduplicator()
    for index in range(50):
        subject.remember(f"h-{index}", a_shape(measurement=f"m-{index}"))
    assert subject.standing.remembered == 50


# ---- hypothesis-regime-tagger -----------------------------------------------

def a_tagger(minimum=10, threshold=0.55):
    return HypothesisRegimeTagger(
        minimum_trades_per_regime=minimum, working_threshold=threshold, prior_hit_rate=0.5,
        prior_weight=4.0, half_life_observations=500,
    )


def test_a_hypothesis_tested_in_one_regime_is_tagged_for_it():
    subject = a_tagger(minimum=5)
    for _ in range(20):
        subject.observe_outcome("h-1", "trending", True)
    tag = subject.tag("h-1")
    assert tag.state == TAGGED_FOR_ONE
    assert tag.may_fire_in("trending")
    assert tag.may_fire_in("chop") is False


def test_a_hypothesis_working_everywhere_it_was_tested_is_regime_independent():
    subject = a_tagger(minimum=5)
    for _ in range(20):
        subject.observe_outcome("h-1", "trending", True)
        subject.observe_outcome("h-1", "chop", True)
    tag = subject.tag("h-1")
    assert tag.state == REGIME_INDEPENDENT
    assert tag.may_fire_in("anything")


def test_a_regime_tested_and_failed_is_recorded_as_such():
    """Stronger evidence than never having been tested there."""
    subject = a_tagger(minimum=5, threshold=0.6)
    for index in range(20):
        subject.observe_outcome("h-1", "trending", True)
        subject.observe_outcome("h-1", "chop", index % 4 == 0)
    tag = subject.tag("h-1")
    assert "chop" in tag.regimes_tested_and_failed
    assert tag.may_fire_in("chop") is False


def test_an_untagged_hypothesis_may_fire_nowhere():
    """An untagged claim is a claim about every market and almost none are."""
    tag = a_tagger(minimum=100).tag("h-1")
    assert tag.state == UNTAGGED
    assert tag.is_tagged is False


def test_a_regime_break_makes_the_tag_be_re_earned():
    subject = a_tagger(minimum=5)
    for _ in range(20):
        subject.observe_outcome("h-1", "trending", True)
    assert subject.tag("h-1").may_fire_in("trending")
    subject.observe_regime_break("h-1", "trending")
    assert subject.tag("h-1").may_fire_in("trending") is False


# ---- hypothesis-mutator -----------------------------------------------------

def a_mutator(threshold_step=0.1, horizon_step=0.5, failures=3):
    return HypothesisMutator(
        threshold_step_fraction=threshold_step, horizon_step_fraction=horizon_step,
        consecutive_failures_before_stopping=failures,
    )


def a_parent(family="f-1", threshold=-2.0, horizon=600.0, regime=None, retired=False):
    return ParentInstruction(
        instruction_id="i-1", family=family, measurement="z_score", comparison="below",
        threshold=threshold, horizon_seconds=horizon, regime_tag=regime, trades=100,
        hit_rate=0.52, was_retired=retired,
    )


def test_every_mutation_counts_in_the_parents_family():
    """Letting each start a fresh family is how a search defeats its own correction."""
    subject = a_mutator()
    for _ in range(5):
        subject.mutate(a_parent(), MOVE_THE_THRESHOLD)
    assert subject.trials_in("f-1") == 5
    hypothesis, _ = subject.mutate(a_parent(), CHANGE_THE_HORIZON)
    assert hypothesis.family == "f-1"
    assert hypothesis.trials_in_family == 6


def test_a_retired_instruction_is_mutated_once():
    """Thirty variants of a dead edge is a system arguing with its own evidence."""
    subject = a_mutator()
    assert subject.mutate(a_parent(retired=True), MOVE_THE_THRESHOLD)[1] == MUTATED
    assert subject.mutate(a_parent(retired=True), CHANGE_THE_HORIZON)[1] == ALREADY_MUTATED


def test_a_family_whose_mutations_all_failed_stops():
    """Continuing burns the whole trial budget on one dead idea."""
    subject = a_mutator(failures=3)
    for _ in range(3):
        subject.observe_outcome("f-1", the_mutation_worked=False)
    assert subject.mutate(a_parent(), MOVE_THE_THRESHOLD)[1] == FAMILY_IS_EXHAUSTED


def test_a_success_resets_the_failure_count():
    subject = a_mutator(failures=2)
    subject.observe_outcome("f-1", False)
    subject.observe_outcome("f-1", True)
    subject.observe_outcome("f-1", False)
    assert subject.mutate(a_parent(), MOVE_THE_THRESHOLD)[1] == MUTATED


def test_changing_the_measurement_is_not_a_mutation():
    assert a_mutator().mutate(a_parent(), "change-the-measurement")[1] == NOT_A_MUTATION


def test_a_near_miss_says_where_the_threshold_should_have_been():
    """A specific mutation rather than a random step."""
    subject = a_mutator()
    for value in (-1.5, -1.6, -1.4):
        subject.observe_near_miss("i-1", value, would_have_worked=True)
    hypothesis, _ = subject.mutate(a_parent(threshold=-2.0), MOVE_THE_THRESHOLD)
    assert hypothesis.context["threshold"] == pytest.approx(-1.5)
    assert hypothesis.evidence["near_misses_used"] == 3


def test_a_regime_condition_can_be_added_and_removed():
    subject = a_mutator()
    added, _ = subject.mutate(a_parent(regime=None), ADD_A_REGIME_CONDITION)
    removed, _ = subject.mutate(a_parent(regime="trending"), "remove-a-regime-condition")
    assert added.regime_tag is not None
    assert removed.regime_tag is None


def test_a_mutation_of_zero_is_refused_at_construction():
    with pytest.raises(ValueError):
        HypothesisMutator(
            threshold_step_fraction=0.0, horizon_step_fraction=0.5,
            consecutive_failures_before_stopping=3,
        )


# ---- symbolic-hypothesis-miner ----------------------------------------------

def a_miner(maximum_terms=2, minimum_examples=50, held_out=20, penalty=0.01, perfect=0.95):
    return SymbolicHypothesisMiner(
        measurements=("z", "imbalance"), thresholds_per_measurement=(-1.0, 0.0, 1.0),
        maximum_terms=maximum_terms, minimum_examples=minimum_examples,
        minimum_held_out_examples=held_out, held_out_fraction=0.3,
        complexity_penalty_per_term=penalty, perfect_fit_threshold=perfect,
    )


def feed_miner(miner, count=200, signal=True):
    for index in range(count):
        z = 2.0 if index % 2 else -2.0
        miner.observe_example({"z": z, "imbalance": 0.1}, label=(z > 0) if signal else (index % 3 == 0), at_ns=index * SECOND_NS)


def test_the_declared_space_size_is_stated():
    """A trial count that cannot be stated cannot be corrected for."""
    subject = a_miner()
    assert subject.space_size() > 0
    assert subject.standing.space_size == subject.space_size()


def test_every_formula_evaluated_is_counted_before_it_is_judged():
    """A miner reporting only its best find reports one trial when it ran ten thousand."""
    subject = a_miner()
    feed_miner(subject)
    for threshold in (-1.0, 0.0, 1.0):
        subject.mine("f-1", (Term("z", ABOVE, threshold),))
    assert subject.trials_in("f-1") == 3
    assert subject.standing.formulas_evaluated == 3


def test_a_formula_is_scored_on_held_out_data():
    subject = a_miner()
    feed_miner(subject)
    formula, outcome = subject.mine("f-1", (Term("z", ABOVE, 0.0),))
    if outcome == MINED:
        assert formula.held_out_trades > 0
        assert formula.held_out_hit_rate is not None


def test_a_perfect_fit_is_reported_as_suspect_not_as_the_best_find():
    """In a space this size a perfect fit is what overfitting looks like."""
    subject = a_miner(perfect=0.9)
    feed_miner(subject, signal=True)
    formula, outcome = subject.mine("f-1", (Term("z", ABOVE, 0.0),))
    assert outcome == SUSPECT_PERFECT_FIT
    assert formula.state == SUSPECT_PERFECT_FIT
    assert formula.is_usable is False


def test_complexity_is_penalised():
    """A three-term formula had more ways to fit noise."""
    subject = a_miner(maximum_terms=3, penalty=0.05, perfect=1.1)
    feed_miner(subject, signal=False)
    simple, _ = subject.mine("f-1", (Term("z", ABOVE, 0.0),))
    complex_, _ = subject.mine(
        "f-1", (Term("z", ABOVE, -1.0), Term("imbalance", ABOVE, 0.0))
    )
    if simple and complex_:
        assert complex_.complexity_penalty > simple.complexity_penalty


def test_too_little_data_mines_nothing():
    subject = a_miner(minimum_examples=1000)
    feed_miner(subject, count=50)
    assert subject.mine("f-1", (Term("z", ABOVE, 0.0),))[1] == TOO_LITTLE_DATA


def test_formulas_are_readable():
    """A weight vector cannot be argued with or checked against a mechanism."""
    term = Term("z", BELOW, -2.0)
    assert str(term) == "z below -2"


def test_an_unbounded_space_is_refused():
    with pytest.raises(ValueError):
        SymbolicHypothesisMiner(
            measurements=(), thresholds_per_measurement=(0.0,), maximum_terms=2,
            minimum_examples=50, minimum_held_out_examples=20, held_out_fraction=0.3,
            complexity_penalty_per_term=0.01, perfect_fit_threshold=0.95,
        )


# ---- hypothesis-ranker ------------------------------------------------------

def a_ranker(maximum=10_000, minimum_half_life=10.0):
    return HypothesisRanker(
        maximum_reachable_trades=maximum, minimum_half_life_trades=minimum_half_life
    )


def test_ranking_is_by_information_per_trade_not_by_edge():
    """Ranking on edge alone always picks the largest claim with the least evidence."""
    subject = a_ranker()
    subject.observe_expected_edge("huge", 0.30)
    subject.observe_required_sample("huge", 9000)
    subject.observe_expected_edge("modest", 0.05)
    subject.observe_required_sample("modest", 300)
    ranked = subject.rank(["huge", "modest"])
    assert ranked[0].hypothesis_id == "modest"


def test_a_near_duplicate_buys_almost_no_information():
    subject = a_ranker()
    for name, novelty in (("novel", 1.0), ("duplicate", 0.05)):
        subject.observe_expected_edge(name, 0.10)
        subject.observe_required_sample(name, 500)
        subject.observe_novelty(name, novelty)
    assert subject.rank(["duplicate", "novel"])[0].hypothesis_id == "novel"


def test_a_fast_decaying_edge_is_barely_worth_confirming():
    subject = a_ranker()
    for name, half_life in (("durable", 5000.0), ("fleeting", 20.0)):
        subject.observe_expected_edge(name, 0.10)
        subject.observe_required_sample(name, 500)
        subject.observe_edge_half_life(name, half_life)
    assert subject.rank(["fleeting", "durable"])[0].hypothesis_id == "durable"


def test_an_unreachable_hypothesis_is_ranked_last_not_excluded():
    """Excluding it hides it; ranking it last means it reappears."""
    subject = a_ranker(maximum=1000)
    subject.observe_expected_edge("reachable", 0.10)
    subject.observe_required_sample("reachable", 500)
    subject.observe_expected_edge("unreachable", 0.50)
    subject.observe_required_sample("unreachable", 100_000)
    ranked = subject.rank(["unreachable", "reachable"])
    assert ranked[0].hypothesis_id == "reachable"
    assert ranked[-1].state == RANK_UNREACHABLE


def test_ties_break_toward_the_older_hypothesis():
    """Otherwise a stream of new ideas starves the queue into a stack."""
    subject = a_ranker()
    subject.observe_expected_edge("older", 0.10, proposed_at_ns=1)
    subject.observe_required_sample("older", 500)
    subject.observe_expected_edge("newer", 0.10, proposed_at_ns=999)
    subject.observe_required_sample("newer", 500)
    assert subject.rank(["newer", "older"])[0].hypothesis_id == "older"


def test_a_hypothesis_with_nothing_known_about_it_cannot_be_ranked():
    ranked = a_ranker().rank(["unknown"])
    assert ranked[0].state == NOT_ENOUGH_INPUTS
    assert ranked[0].rank is None


# ---- instruction-retirer ----------------------------------------------------

def a_retirer(gap=0.002):
    return InstructionRetirer(replay_gap_threshold=gap)


def test_a_fired_falsification_criterion_retires_an_instruction():
    """Arguing with it is arguing with a promise the system made to itself."""
    subject = a_retirer()
    subject.observe_criterion_fired("i-1")
    record = subject.check("i-1", trades=340, realised=-0.05)
    assert record.state == RETIRED
    assert record.because == CRITERION_FIRED
    assert "340 trade(s)" in record.reason


def test_a_broken_regime_retires_the_instructions_tagged_for_it():
    subject = a_retirer()
    subject.observe_regime_tag("i-1", "trending")
    subject.observe_regime_break("trending", True)
    assert subject.check("i-1", 100, 0.01).because == REGIME_BROKE


def test_an_instruction_is_retired_before_its_record_turns_negative():
    subject = a_retirer()
    subject.observe_edge_half_life("i-1", half_life_trades=200, trades_so_far=250)
    record = subject.check("i-1", trades=250, realised=0.10)
    assert record.because == EDGE_DECAYED
    assert record.realised_at_retirement > 0


def test_an_instruction_that_only_works_in_replay_is_retired():
    subject = a_retirer(gap=0.001)
    subject.observe_live_versus_replay_gap("i-1", 0.01)
    record = subject.check("i-1", 100, -0.01)
    assert record.because == ONLY_WORKS_IN_REPLAY
    assert "never worked" in record.reason


def test_retirement_is_not_deletion():
    """Deleting loses the evidence and makes the idea look novel next month."""
    subject = a_retirer()
    subject.observe_criterion_fired("i-1")
    record = subject.check("i-1", 100, -0.01)
    assert record.was_deleted is False
    assert record.trades_at_retirement == 100


def test_an_instruction_comes_back_when_its_regime_does():
    subject = a_retirer()
    subject.observe_regime_tag("i-1", "trending")
    subject.observe_regime_break("trending", True)
    assert subject.check("i-1", 100, 0.01).state == RETIRED
    subject.observe_regime_break("trending", False)
    assert subject.check("i-1", 100, 0.01).state == RESURRECTED


def test_a_retired_instruction_may_be_mutated_once():
    subject = a_retirer()
    subject.observe_criterion_fired("i-1")
    subject.check("i-1", 100, -0.01)
    assert subject.permit_mutation("i-1") is True
    assert subject.permit_mutation("i-1") is False


def test_an_instruction_with_nothing_wrong_stands():
    assert a_retirer().check("i-1", 100, 0.05).state == INSTRUCTION_STANDING


# ---- instruction-writer -----------------------------------------------------

class Criterion:
    measure = "hit-rate"
    comparison = "at-or-below"
    threshold = 0.5
    required_trades = 400


def a_writer(minimum_novelty=0.5, maximum=10_000):
    return InstructionWriter(
        minimum_novelty=minimum_novelty, maximum_reachable_trades=maximum,
        known_measurements=("z_score", "funding_rate"),
    )


def a_hypothesis(hypothesis_id="h-1", measurement="z_score", trials=3):
    return Hypothesis(
        hypothesis_id=hypothesis_id, statement="a claim", what_would_refute_it="a refutation",
        family="f-1", trials_in_family=trials, source="a-source",
        context={
            "measurement": measurement, "comparison": "below", "threshold": -2.0,
            "direction": "long", "expectation": "reversion", "horizon_seconds": 600.0,
        },
        required_sample_size=None, regime_tag=None, novelty=None, evidence={},
        proposed_at_ns=0,
    )


def a_ready_writer(**kwargs):
    subject = a_writer(**kwargs)
    subject.observe_falsification_criterion("h-1", Criterion())
    subject.observe_required_sample("h-1", 400)
    subject.observe_trial_verdict("h-1", True)
    subject.observe_novelty("h-1", 0.9)
    subject.observe_refutation_verdict("h-1", "not-refuted")
    subject.observe_regime_tag("h-1", "trending")
    subject.observe_edge_half_life("h-1", 5000.0)
    return subject


def test_an_instruction_is_written_when_every_condition_agrees():
    instruction, failing = a_ready_writer().write(a_hypothesis())
    assert failing == ()
    assert instruction.regime_tag == "trending"
    assert instruction.required_sample_size == 400


def test_the_instruction_carries_its_own_retirement_condition():
    """Without one it outlives the market it was learned in."""
    instruction, _ = a_ready_writer().write(a_hypothesis())
    assert "hit-rate" in instruction.retire_when
    assert "regime breaks" in instruction.retire_when


def test_a_refusal_names_every_failing_condition_not_the_first():
    """"It failed" sends the next attempt to fix the wrong thing."""
    subject = a_writer()
    instruction, failing = subject.write(a_hypothesis(measurement="nothing-measures-this"))
    assert instruction is None
    assert NO_MEASUREMENT in failing
    assert NO_FALSIFICATION_CRITERION in failing
    assert len(failing) > 2


def test_a_hypothesis_with_nothing_that_could_retire_it_is_refused():
    subject = a_ready_writer()
    subject._criteria.clear()
    assert NO_FALSIFICATION_CRITERION in subject.write(a_hypothesis())[1]


def test_the_best_of_many_variations_is_not_a_finding():
    subject = a_ready_writer()
    subject.observe_trial_verdict("h-1", False)
    assert DOES_NOT_CLEAR_ITS_TRIALS in subject.write(a_hypothesis())[1]


def test_the_same_idea_in_three_wordings_is_not_three_confirmations():
    subject = a_ready_writer(minimum_novelty=0.8)
    subject.observe_novelty("h-1", 0.1)
    assert NOT_NOVEL in subject.write(a_hypothesis())[1]


def test_an_untested_hypothesis_is_refused():
    subject = a_ready_writer()
    subject._verdicts.clear()
    assert NOT_REFUTATION_TESTED in subject.write(a_hypothesis())[1]


def test_a_refuted_hypothesis_is_refused():
    subject = a_ready_writer()
    subject.observe_refutation_verdict("h-1", "refuted")
    assert WAS_REFUTED in subject.write(a_hypothesis())[1]


def test_an_untagged_hypothesis_is_refused():
    subject = a_ready_writer()
    subject.observe_regime_tag("h-1", None)
    assert NO_REGIME_TAG in subject.write(a_hypothesis())[1]


def test_an_edge_that_decays_before_its_own_sample_size_is_refused():
    """Confirming it spends the trades to learn nothing."""
    subject = a_ready_writer()
    subject.observe_edge_half_life("h-1", 50.0)
    assert EDGE_DIES_BEFORE_IT_IS_CONFIRMED in subject.write(a_hypothesis())[1]


def test_an_unreachable_sample_size_is_refused():
    subject = a_ready_writer(maximum=100)
    assert SAMPLE_UNREACHABLE in subject.write(a_hypothesis())[1]


def test_a_writer_with_no_known_measurements_is_refused():
    with pytest.raises(ValueError):
        InstructionWriter(
            minimum_novelty=0.5, maximum_reachable_trades=10_000, known_measurements=()
        )
