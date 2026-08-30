"""The brain: three opinions in, one intent out, and everything that argues with it.

The tests here are mostly about restraint. This block is where the system commits
capital, and almost every way it goes wrong is a small reasonable-looking
allowance: a forecast that shades a little too much, an objection that discounts
instead of stopping, a lucky win recorded as a success, a model's sentence kept
because it sounded right.

Each of those has a test that fails if the protection is removed.
"""

import importlib

import pytest

from parts.ai_brain.bot_weight_sampler import BotWeightSampler
from parts.ai_brain.brain_self_reflector import (
    FAILED_AS_ARGUED, FAILED_AS_PREDICTED, FAILED_UNANTICIPATED, WON_AS_REASONED,
    WON_DESPITE_THE_REASONING, BrainSelfReflector, TradeEpisode,
)
from parts.ai_brain.devils_advocate import (
    CONVICTION_IS_UNMEASURED, DISSENT_WAS_OVERRULED, NO_STOP, SOLE_UNPROVEN_BOT,
    DevilsAdvocate,
)
from parts.ai_brain.exploration_pair_opener import (
    ALREADY_RUNNING_HERE, BUDGET_SPENT, NOT_A_DISAGREEMENT, NOT_WORTH_THE_COST,
    OPENED, QUESTION_ALREADY_ANSWERED, ExplorationPairOpener,
)
from parts.ai_brain.forecast_bias_weigher import (
    NOT_TRUSTED, NOT_YET_MEASURED, ForecastBiasWeigher,
)
from parts.ai_brain.intent_explainer import IntentExplainer
from parts.ai_brain.intent_timing_gate import (
    ACT_NOW, A_BOT_STOOD_DOWN_ON_TIMING, NO_PRICE, PRICE_HAS_MOVED_PAST_THE_DECISION,
    WAITING_ON_A_BOT, IntentTimingGate,
)
from parts.ai_brain.opinion_arbiter import (
    CONVICTION_TOO_LOW, COUNTER_ARGUMENT_STANDS, COVERAGE_TOO_THIN, OUTSIDE_COMPETENCE,
    REGIME_BROKEN, OpinionArbiter,
)
from parts.ai_brain.opinion_conflict_resolver import (
    BotMaturity, FAVOUR_MATURITY, FAVOUR_THE_REGIME, FOLLOW_REGIME_MEMORY, NO_CONFLICT,
    STAND_ASIDE as RULING_STAND_ASIDE, OpinionConflictResolver,
)
from parts.ai_brain.premortem_writer import (
    CONVICTION_WAS_NOT_MEASURED, HORIZON_PASSES, REGIME_CHANGES, STOP_IS_HIT, PremortemWriter,
)
from parts.ai_brain.size_hint_writer import SizeHintWriter
from parts.ai_brain.strategy_review_reasoner import StrategyReviewReasoner
from runtime.bot_opinion import (
    CLOSE_POSITION, ENTER_NOW, LONG, SHORT, STAND_DOWN, WAIT_FOR_TRIGGER, BotScorecard,
    DirectionalOpinion, EntryTiming, ExitPlan, ExitTarget,
)
from runtime.claim_verification import (
    make_request, numbers_in, split_sentences, verify_against_facts, written_without_a_model,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trade_intent import (
    ADD_TO, CLOSE, MAJORITY, NO_OPINION, OPEN, RULED, SOLE_OPINION, STAND_ASIDE, UNANIMOUS,
    UNDERPERFORMING, UNMEASURED, WORKING, ConflictRuling, CounterArgument, StrategyReview,
    TradeIntent, no_intent,
)

BLOCK_PARTS = {
    "opinion-arbiter": "parts.ai_brain.opinion_arbiter",
    "exploration-pair-opener": "parts.ai_brain.exploration_pair_opener",
    "forecast-bias-weigher": "parts.ai_brain.forecast_bias_weigher",
    "intent-explainer": "parts.ai_brain.intent_explainer",
    "brain-self-reflector": "parts.ai_brain.brain_self_reflector",
    "opinion-conflict-resolver": "parts.ai_brain.opinion_conflict_resolver",
    "bot-weight-sampler": "parts.ai_brain.bot_weight_sampler",
    "size-hint-writer": "parts.ai_brain.size_hint_writer",
    "premortem-writer": "parts.ai_brain.premortem_writer",
    "devils-advocate": "parts.ai_brain.devils_advocate",
    "intent-timing-gate": "parts.ai_brain.intent_timing_gate",
    "strategy-review-reasoner": "parts.ai_brain.strategy_review_reasoner",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
BULL, BEAR, TAIL = "bull-bot", "bear-bot", "profit-tailgating-bot"


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_seconds(self, seconds):
        self.now_ns += int(seconds * 1e9)


def an_estimate(value, observations=100, is_fitted=True, reason="measured"):
    return Estimate(
        value=value, is_fitted=is_fitted, observations=observations, prior=0.5,
        was_clamped=False, bound_low=None, bound_high=None, reason=reason,
    )


class Regime:
    def __init__(self, regime="reverting", classified=True):
        self.regime = regime
        self.is_classified = classified


def a_plan(stop=95.0, horizon=600.0):
    return ExitPlan(
        bot=BULL, venue_id=VENUE, symbol=SYMBOL, side=LONG, stop_price=stop,
        targets=(ExitTarget(price=110.0, fraction=1.0, reason="target"),),
        invalidation_reason="below the stop", horizon_seconds=horizon,
        risk_fraction=0.05, reward_to_risk=2.0, reason="planned", planned_at_ns=Clock()(),
    )


def an_opinion(bot=BULL, side=LONG, conviction=0.8, measured=True, action=ENTER_NOW, plan=None):
    return DirectionalOpinion(
        bot=bot, side=side, venue_id=VENUE, symbol=SYMBOL, action=action,
        conviction=an_estimate(conviction, 200 if measured else 0, measured),
        timing=None, exit_plan=plan if plan is not None else a_plan(),
        features_summary={}, refusal=None,
        reason=f"{bot} wants {side}", formed_at_ns=Clock()(),
    )


def a_stand_down(bot=BEAR, refusal="conviction-below-threshold"):
    return DirectionalOpinion(
        bot=bot, side=SHORT, venue_id=VENUE, symbol=SYMBOL, action=STAND_DOWN,
        conviction=an_estimate(0.2), timing=None, exit_plan=None, features_summary={},
        refusal=refusal, reason=f"{bot} stood down", formed_at_ns=Clock()(),
    )


def an_intent(
    conviction=0.8, measured=True, agreement=UNANIMOUS, bots=(BULL, TAIL),
    dissenting=(), stop=95.0, side=LONG, action=OPEN, weights=None,
):
    return TradeIntent(
        venue_id=VENUE, symbol=SYMBOL, side=side, action=action,
        conviction=an_estimate(conviction, 200 if measured else 0, measured),
        horizon_seconds=600.0, stop_price=stop, agreement=agreement,
        contributing_bots=tuple(bots), dissenting_bots=tuple(dissenting),
        opinion_weights=weights if weights is not None else {bot: 1.0 for bot in bots},
        evidence={}, reason="an intent", formed_at_ns=Clock()(),
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_brain_part_imports_a_bot(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        text = handle.read()
    for peer in ("parts.bull_bot", "parts.bear_bot", "parts.profit_tailgating_bot"):
        assert peer not in text


# ---- claim verification -----------------------------------------------------

def test_a_sentence_citing_a_number_nothing_measured_is_removed():
    """A rationale with one invented number reads exactly like the true ones."""
    verified = verify_against_facts(
        "Conviction was 72%. The book was 4.4 times deeper than normal.",
        {"conviction": 0.72}, relative_tolerance=0.02,
    )
    assert verified.kept_sentences == ("Conviction was 72%.",)
    assert len(verified.removed_sentences) == 1
    assert "4.4" in verified.unsupported_claims[0]
    assert verified.is_fully_supported is False


def test_a_percentage_matches_the_fraction_it_was_measured_as():
    """Deleting true sentences trains whoever reads these to ignore the removals."""
    verified = verify_against_facts(
        "The stop is 5.0% away.", {"risk": 0.05}, relative_tolerance=0.02
    )
    assert verified.is_fully_supported
    assert verified.citations["5.0%"] == "risk"


def test_a_sentence_asserting_nothing_checkable_is_removed_from_a_rationale():
    verified = verify_against_facts(
        "This setup looks strong.", {"conviction": 0.7}, 0.02, require_a_citation=True
    )
    assert verified.is_empty


def test_a_failure_mode_with_no_number_is_kept_in_a_premortem():
    """The risks that end accounts are not the quantified ones."""
    verified = verify_against_facts(
        "The venue could halt withdrawals.", {"conviction": 0.7}, 0.02, require_a_citation=False
    )
    assert verified.kept_sentences


def test_a_request_with_no_facts_is_refused():
    """A request without facts asks the model to recall rather than to phrase."""
    with pytest.raises(ValueError):
        make_request(
            purpose="p", venue_id=VENUE, symbol=SYMBOL, instruction="i",
            facts={}, maximum_sentences=2,
        )


def test_numbers_are_read_with_separators_and_signs():
    found = dict((raw, value) for raw, value, _ in numbers_in("It moved -1,234.5 to 12%"))
    assert found["-1,234.5"] == -1234.5
    assert found["12%"] == 12.0


def test_the_fallback_text_needs_no_model():
    """A part whose explanation needs a model goes silent when the model is down."""
    verified = written_without_a_model("ignored", {"conviction": 0.7})
    assert verified.was_written_by_a_model is False
    assert "conviction" in verified.text


# ---- bot-weight-sampler -----------------------------------------------------

def a_sampler(floor=0.2, cap=3.0, exploration_floor=20, exploration_weight=1.0, regret_weight=1.0):
    return BotWeightSampler(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=10, minimum_weight=floor, maximum_weight=cap,
        regret_weight=regret_weight, exploration_floor_observations=exploration_floor,
        exploration_weight=exploration_weight,
    )


def test_a_bot_with_too_thin_a_record_is_sampled_not_ranked():
    subject = a_sampler(exploration_floor=50, exploration_weight=1.2)
    for index in range(10):
        subject.observe_closed_trade(BULL, "reverting", index % 2 == 0)
    weight = subject.weight_for(BULL, "reverting")
    assert weight.is_exploring is True
    assert weight.weight == 1.2
    assert "sampled deliberately" in weight.reason


def test_a_bot_weighted_to_zero_could_never_be_cleared():
    """The trap this part exists to avoid, refused at construction."""
    with pytest.raises(ValueError):
        BotWeightSampler(
            prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
            minimum_observations=10, minimum_weight=0.0, maximum_weight=3.0,
            regret_weight=1.0, exploration_floor_observations=20, exploration_weight=1.0,
        )


def test_a_bot_that_is_right_where_nobody_listened_carries_regret():
    """Without regret a bot right about untaken trades looks like one right about nothing."""
    subject = a_sampler(exploration_floor=5, regret_weight=2.0)
    for index in range(100):
        subject.observe_closed_trade(BULL, "reverting", index % 2 == 0)
        subject.observe_closed_trade(BEAR, "reverting", index % 2 == 0)
    plain = subject.weight_for(BEAR, "reverting").weight
    for _ in range(30):
        subject.observe_regret(BEAR, "reverting", 0.02)
    assert subject.weight_for(BEAR, "reverting").weight > plain


def test_weights_are_kept_per_regime():
    subject = a_sampler(exploration_floor=5)
    for index in range(100):
        subject.observe_closed_trade(BULL, "trending", True)
        subject.observe_closed_trade(BULL, "reverting", False)
        subject.observe_closed_trade(BEAR, "trending", index % 2 == 0)
        subject.observe_closed_trade(BEAR, "reverting", index % 2 == 0)
    assert subject.weight_for(BULL, "trending").weight > subject.weight_for(BULL, "reverting").weight


def test_a_bot_is_measured_against_the_other_bots_not_against_itself():
    subject = a_sampler(exploration_floor=5)
    for index in range(300):
        subject.observe_closed_trade(BULL, "reverting", index % 5 == 0)
    for _ in range(20):
        subject.observe_closed_trade(BEAR, "reverting", True)
    assert subject.weight_for(BULL, "reverting").weight < 1.0


def test_a_restarted_sampler_adopts_a_bots_scorecard():
    scorecard = BotScorecard(bot=BULL)
    for index in range(100):
        scorecard.record_closed_trade("d", "reverting", 0.7, index % 3 != 0, 1.0)
    subject = a_sampler(exploration_floor=5)
    subject.observe_scorecard(BULL, scorecard)
    assert subject.weight_for(BULL, "reverting").trades_judged == 100


# ---- opinion-conflict-resolver ----------------------------------------------

def a_resolver(gap=10, minimum_hit_rate=0.5, remember=True):
    return OpinionConflictResolver(
        maturity_gap_trades=gap, minimum_regime_hit_rate=minimum_hit_rate,
        remember_rulings=remember,
    )


def test_bots_pointing_the_same_way_are_not_a_conflict():
    ruling = a_resolver().resolve([an_opinion(BULL), an_opinion(TAIL)], Regime())
    assert ruling.ruling == NO_CONFLICT


def test_an_unresolvable_disagreement_stands_aside():
    """Taking the louder one is a coin flip with costs."""
    subject = a_resolver()
    ruling = subject.resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", classified=False)
    )
    assert ruling.ruling == RULING_STAND_ASIDE
    assert ruling.resolved is False
    assert subject.standing.stood_aside == 1


def test_the_regime_favours_the_bot_it_belongs_to_on_its_record_there():
    subject = a_resolver(minimum_hit_rate=0.5)
    subject.observe_regime_hit_rate(BULL, "trending", 0.65)
    ruling = subject.resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("trending")
    )
    assert ruling.ruling == FAVOUR_THE_REGIME
    assert ruling.favoured_bot == BULL
    assert "in this regime" in ruling.grounds


def test_a_regime_ruling_needs_the_record_in_that_regime_not_overall():
    """Where this kind of ruling usually goes wrong."""
    subject = a_resolver(minimum_hit_rate=0.6)
    subject.observe_regime_hit_rate(BULL, "trending", 0.2)
    ruling = subject.resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("trending")
    )
    assert ruling.ruling != FAVOUR_THE_REGIME


def test_maturity_decides_only_when_the_gap_is_large():
    subject = a_resolver(gap=20)
    subject.observe_bot_maturity(BotMaturity(BULL, "random-walk", 100, True))
    subject.observe_bot_maturity(BotMaturity(BEAR, "random-walk", 95, True))
    close = subject.resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )
    assert close.ruling == RULING_STAND_ASIDE

    subject.observe_bot_maturity(BotMaturity(BEAR, "random-walk", 5, False))
    clear = subject.resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )
    assert clear.ruling == FAVOUR_MATURITY
    assert clear.favoured_bot == BULL


def test_a_ruling_is_remembered_rather_than_re_derived_every_tick():
    subject = a_resolver(remember=True)
    subject.observe_regime_hit_rate(BULL, "trending", 0.7)
    subject.resolve([an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("trending"))
    again = subject.resolve([an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("trending"))
    assert again.ruling == FOLLOW_REGIME_MEMORY
    assert subject.standing.memory_hits == 1


def test_a_broken_regime_drops_what_it_taught():
    """The ruling that was right for months and stops working."""
    subject = a_resolver()
    subject.observe_regime_hit_rate(BULL, "trending", 0.7)
    subject.resolve([an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("trending"))
    assert subject.forget_regime("trending") == 1
    assert subject.remembered_ruling(SYMBOL, "trending") is None


def test_a_ruling_is_recorded_even_when_it_is_to_do_nothing():
    """A system that records only what it did cannot learn from what it declined."""
    ruling = a_resolver().resolve(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )
    assert ruling.grounds
    assert ruling.opposed_bots


# ---- opinion-arbiter --------------------------------------------------------

def a_floor(margin=0.0):
    # The plan every test opinion carries pays 2.0 to 1 with a 5% stop: fee-free
    # break-even one third, 0.022 of the risk in fees, so the floor is 34.1%.
    return ConvictionFloor(fee_rate=0.00055, margin=margin, fallback_reward_to_risk=1.5)


def an_arbiter(
    margin=0.0, agreement_bonus=0.05, sole_penalty=0.2, shade=0.05,
    minimum_competence=0.3, minimum_coverage=0.3, strategy_review_discount=0.3,
):
    return OpinionArbiter(
        conviction_floor=a_floor(margin), agreement_bonus=agreement_bonus,
        sole_opinion_penalty=sole_penalty, maximum_forecast_shade=shade,
        minimum_competence=minimum_competence, minimum_coverage=minimum_coverage,
        strategy_review_distrust_discount=strategy_review_discount,
    )


class Bias:
    def __init__(self, bias, trusted=True):
        self.venue_id, self.symbol = VENUE, SYMBOL
        self.bias = bias
        self.is_trusted = trusted


class Weight:
    def __init__(self, bot, regime, weight):
        self.bot, self.regime, self.weight = bot, regime, weight


def test_two_bots_agreeing_produce_one_intent():
    intent = an_arbiter().arbitrate([an_opinion(BULL), an_opinion(TAIL)], Regime())
    assert intent.is_actionable
    assert intent.agreement == UNANIMOUS
    assert set(intent.contributing_bots) == {BULL, TAIL}


def test_agreement_counts_for_more_than_one_bot_being_sure():
    """A property of the ensemble, not of any opinion in it."""
    subject = an_arbiter(agreement_bonus=0.1, sole_penalty=0.3)
    together = subject.arbitrate([an_opinion(BULL, conviction=0.7), an_opinion(TAIL, conviction=0.7)], Regime())
    alone = subject.arbitrate([an_opinion(BULL, conviction=0.7)], Regime())
    assert together.conviction.value > alone.conviction.value


def test_every_bot_standing_down_is_published_not_dropped():
    """A symbol the brain declined must differ from one it never saw."""
    intent = an_arbiter().arbitrate([a_stand_down(BULL), a_stand_down(BEAR)], Regime())
    assert intent.action == STAND_ASIDE
    assert intent.symbol == SYMBOL
    assert "stood down" in intent.reason


def test_a_symbol_outside_measured_competence_is_refused_not_discounted():
    """A small position taken outside competence is the same mistake in smaller size."""
    subject = an_arbiter(minimum_competence=0.5)
    subject.observe_competence(VENUE, SYMBOL, 0.1)
    intent = subject.arbitrate([an_opinion(BULL), an_opinion(TAIL)], Regime())
    assert intent.action == STAND_ASIDE
    assert subject.standing.by_refusal[OUTSIDE_COMPETENCE] == 1


def test_too_little_coverage_is_its_own_refusal():
    subject = an_arbiter(minimum_coverage=0.5)
    subject.observe_coverage(VENUE, SYMBOL, 0.1)
    subject.arbitrate([an_opinion(BULL)], Regime())
    assert subject.standing.by_refusal[COVERAGE_TOO_THIN] == 1


def test_a_broken_regime_stops_every_opinion_formed_in_it():
    subject = an_arbiter()
    subject.observe_regime_break("reverting", True)
    subject.arbitrate([an_opinion(BULL), an_opinion(TAIL)], Regime("reverting"))
    assert subject.standing.by_refusal[REGIME_BROKEN] == 1


def test_the_forecast_can_shade_a_decision_and_never_make_one():
    subject = an_arbiter(margin=0.2, shade=0.05, agreement_bonus=0.0)
    subject.observe_forecast_bias(Bias(0.5))
    intent = subject.arbitrate([an_opinion(BULL, conviction=0.4)], Regime())
    assert intent.action == STAND_ASIDE, "a 40% conviction cannot be carried by a forecast"

    supported = subject.arbitrate([an_opinion(BULL, conviction=0.7)], Regime())
    assert supported.evidence["forecast_shade"] == pytest.approx(0.05)


def test_a_shade_of_half_the_range_is_refused_at_construction():
    with pytest.raises(ValueError):
        OpinionArbiter(
            conviction_floor=a_floor(), agreement_bonus=0.05, sole_opinion_penalty=0.2,
            maximum_forecast_shade=0.5, minimum_competence=0.3, minimum_coverage=0.3,
            strategy_review_distrust_discount=0.3,
        )


def test_the_counter_argument_vetoes_rather_than_discounts():
    """An objection that could only lower a score would be absorbed."""
    subject = an_arbiter()
    argument = CounterArgument(
        venue_id=VENUE, symbol=SYMBOL, objections=("it is crowded",),
        strongest_objection="it is crowded", would_reverse_the_decision=True,
        evidence_cited={}, was_written_by_a_model=False, reason="strong", argued_at_ns=Clock()(),
    )
    intent = subject.arbitrate(
        [an_opinion(BULL, conviction=0.95), an_opinion(TAIL, conviction=0.95)],
        Regime(), counter_argument=argument,
    )
    assert intent.action == STAND_ASIDE
    assert subject.standing.by_refusal[COUNTER_ARGUMENT_STANDS] == 1


def test_opposite_opinions_are_never_averaged_into_a_third():
    """An average of two opposite opinions is a view nobody holds."""
    subject = an_arbiter()
    intent = subject.arbitrate([an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime())
    assert intent.action == STAND_ASIDE

    ruling = ConflictRuling(
        venue_id=VENUE, symbol=SYMBOL, ruling="favour", favoured_bot=BULL,
        opposed_bots=(BEAR,), grounds="the regime is the bull's", regime="reverting",
        ruled_at_ns=Clock()(),
    )
    ruled = subject.arbitrate(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime(), ruling=ruling
    )
    assert ruled.is_actionable
    assert ruled.side == LONG
    assert ruled.contributing_bots == (BULL,)
    assert ruled.agreement == RULED


def test_bot_weights_scale_the_blended_conviction():
    subject = an_arbiter(agreement_bonus=0.0)
    subject.observe_bot_weight(Weight(BULL, "reverting", 3.0))
    subject.observe_bot_weight(Weight(TAIL, "reverting", 0.2))
    intent = subject.arbitrate(
        [an_opinion(BULL, conviction=0.9), an_opinion(TAIL, conviction=0.6)], Regime()
    )
    assert intent.conviction.value > 0.8


def test_an_unweighted_bot_starts_at_one_not_at_zero():
    """A bot weighted zero could never produce the record that would weight it."""
    assert an_arbiter().weight_of("never-weighed", "reverting") == 1.0


def a_strategy_review(bot, assessment=UNDERPERFORMING, is_fitted=True, value=0.3):
    return StrategyReview(
        bot=bot, assessment=assessment,
        confidence=Estimate(
            value=value, is_fitted=is_fitted, observations=50, prior=0.5,
            was_clamped=False, bound_low=0.0, bound_high=1.0, reason="test",
        ),
        reason="test review", formed_at_ns=Clock()(),
    )


def test_an_underperforming_strategy_review_discounts_a_bots_weight():
    """A read on the bot's own record, never evidence about this setup."""
    subject = an_arbiter(agreement_bonus=0.0, strategy_review_discount=0.5)
    subject.observe_strategy_review(a_strategy_review(BULL, UNDERPERFORMING))
    trusted = subject.arbitrate([an_opinion(BULL, conviction=0.9)], Regime())
    distrusted_weight = trusted.opinion_weights[BULL]
    assert distrusted_weight == pytest.approx(0.5)


def test_an_unfitted_or_working_strategy_review_changes_nothing():
    subject = an_arbiter()
    subject.observe_strategy_review(a_strategy_review(BULL, UNDERPERFORMING, is_fitted=False))
    assert subject.trust_multiplier(BULL) == 1.0
    subject.observe_strategy_review(a_strategy_review(BULL, WORKING))
    assert subject.trust_multiplier(BULL) == 1.0
    subject.observe_strategy_review(a_strategy_review(BULL, UNMEASURED))
    assert subject.trust_multiplier(BULL) == 1.0


def test_a_close_opinion_becomes_a_close_intent():
    subject = an_arbiter()
    closing = an_opinion(BULL, action=CLOSE_POSITION)
    intent = subject.arbitrate([closing], Regime())
    assert intent.action == CLOSE


def test_a_weak_blended_conviction_stands_aside():
    subject = an_arbiter(margin=0.45, agreement_bonus=0.0)
    subject.arbitrate([an_opinion(BULL, conviction=0.6), an_opinion(TAIL, conviction=0.6)], Regime())
    assert subject.standing.by_refusal[CONVICTION_TOO_LOW] == 1


def test_the_brain_clears_the_most_demanding_plan_behind_the_intent():
    """Two bots, two plans; the intent acts on both, so it must be worth taking
    against the one with the tighter stop and the thinner reward."""
    from runtime.edge_arithmetic import break_even_probability, round_trip_cost_in_risk_units

    generous = a_plan()  # 2.0 to 1, 5% stop
    thin = ExitPlan(
        bot=TAIL, venue_id=VENUE, symbol=SYMBOL, side=LONG, stop_price=99.0,
        targets=(ExitTarget(price=101.2, fraction=1.0, reason="target"),),
        invalidation_reason="below the stop", horizon_seconds=600.0,
        risk_fraction=0.01, reward_to_risk=1.2, reason="planned", planned_at_ns=Clock()(),
    )
    demanding = break_even_probability(1.2, round_trip_cost_in_risk_units(0.00055, 0.01))
    subject = an_arbiter(agreement_bonus=0.0, sole_penalty=0.0)
    subject.arbitrate(
        [an_opinion(BULL, conviction=demanding - 0.01, plan=generous),
         an_opinion(TAIL, conviction=demanding - 0.01, plan=thin)],
        Regime(),
    )
    assert subject.standing.by_refusal[CONVICTION_TOO_LOW] == 1
    acted = subject.arbitrate(
        [an_opinion(BULL, conviction=demanding + 0.02, plan=generous),
         an_opinion(TAIL, conviction=demanding + 0.02, plan=thin)],
        Regime(),
    )
    assert acted.is_actionable


# ---- exploration-pair-opener ------------------------------------------------

def an_opener(maximum_pairs=3, gap=20, maximum_cost=0.02, minimum_information=0.05):
    return ExplorationPairOpener(
        maximum_open_pairs=maximum_pairs, maturity_gap_trades=gap,
        maximum_cost_fraction=maximum_cost, minimum_information_value=minimum_information,
    )


def test_a_genuine_unresolvable_disagreement_opens_a_pair():
    subject = an_opener()
    intents, outcome = subject.consider(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )
    assert outcome == OPENED
    assert len(intents) == 2
    assert {intent.side for intent in intents} == {LONG, SHORT}
    assert all(intent.evidence["is_an_exploration_leg"] for intent in intents)


def test_a_pair_is_an_experiment_not_a_hedge():
    """A hedge is meant to cancel; a pair is meant to resolve."""
    subject = an_opener()
    intents, _ = subject.consider(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )
    assert all("not a hedge" in intent.reason for intent in intents)
    assert len(subject.open_pairs) == 1


def test_bots_agreeing_is_not_an_experiment():
    subject = an_opener()
    assert subject.consider([an_opinion(BULL), an_opinion(TAIL)], Regime())[1] == NOT_A_DISAGREEMENT


def test_a_question_already_answered_is_not_run_again():
    """Running it anyway spends money to learn what was known."""
    subject = an_opener(gap=10)
    subject.observe_bot_maturity(BULL, "random-walk", 100)
    subject.observe_bot_maturity(BEAR, "random-walk", 3)
    outcome = subject.consider(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )[1]
    assert outcome == QUESTION_ALREADY_ANSWERED


def test_a_second_pair_in_the_same_symbol_is_refused():
    subject = an_opener()
    opinions = [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)]
    subject.consider(opinions, Regime("random-walk", False))
    assert subject.consider(opinions, Regime("random-walk", False))[1] == ALREADY_RUNNING_HERE


def test_the_experiment_budget_is_bounded():
    """Twenty simultaneous pairs teach twenty things badly."""
    subject = an_opener(maximum_pairs=1)
    subject.consider([an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False))
    subject.close_pair(VENUE, SYMBOL)
    subject._open[("other", "ETHUSDT")] = object()
    assert subject.consider(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )[1] == BUDGET_SPENT


def test_an_experiment_costing_more_than_it_teaches_is_refused():
    subject = an_opener(maximum_cost=0.001)
    subject.observe_round_trip_cost(VENUE, SYMBOL, 0.01)
    assert subject.consider(
        [an_opinion(BULL, LONG), an_opinion(BEAR, SHORT)], Regime("random-walk", False)
    )[1] == NOT_WORTH_THE_COST


def test_information_value_falls_as_both_bots_become_well_measured():
    subject = an_opener()
    subject.observe_bot_maturity(BULL, "reverting", 0)
    subject.observe_bot_maturity(BEAR, "reverting", 0)
    unknown = subject.information_value(BULL, BEAR, "reverting")
    subject.observe_bot_maturity(BULL, "reverting", 500)
    subject.observe_bot_maturity(BEAR, "reverting", 500)
    assert subject.information_value(BULL, BEAR, "reverting") < unknown


def test_a_brain_that_may_run_no_experiments_is_refused_at_construction():
    with pytest.raises(ValueError):
        ExplorationPairOpener(
            maximum_open_pairs=0, maturity_gap_trades=20,
            maximum_cost_fraction=0.02, minimum_information_value=0.05,
        )


# ---- forecast-bias-weigher --------------------------------------------------

def a_weigher(maximum_bias=0.05, minimum_trust=0.55, minimum=20):
    return ForecastBiasWeigher(
        maximum_bias=maximum_bias, minimum_trust=minimum_trust, prior_trust=0.5,
        prior_weight=4.0, half_life_observations=500, minimum_observations=minimum,
    )


def test_an_unmeasured_forecaster_contributes_exactly_nothing():
    """Letting it contribute a little is how an unvalidated model gets everywhere."""
    subject = a_weigher()
    subject.observe_forecast(VENUE, SYMBOL, 0.05)
    bias = subject.weigh(VENUE, SYMBOL, "ensemble", "trending")
    assert bias.bias == 0.0
    assert bias.is_trusted is False
    assert NOT_YET_MEASURED in bias.reason


def test_a_forecaster_that_has_been_wrong_is_not_listened_to():
    subject = a_weigher(minimum_trust=0.6, minimum=20)
    for index in range(100):
        subject.observe_forecast_outcome("ensemble", "trending", index % 5 == 0)
    subject.observe_forecast(VENUE, SYMBOL, 0.05)
    bias = subject.weigh(VENUE, SYMBOL, "ensemble", "trending")
    assert bias.bias == 0.0
    assert NOT_TRUSTED in bias.reason


def test_a_trusted_forecast_shades_within_its_ceiling():
    subject = a_weigher(maximum_bias=0.04, minimum_trust=0.5, minimum=20)
    for _ in range(100):
        subject.observe_forecast_outcome("ensemble", "trending", True)
    subject.observe_forecast(VENUE, SYMBOL, 0.05)
    bias = subject.weigh(VENUE, SYMBOL, "ensemble", "trending")
    assert 0 < bias.bias <= 0.04


def test_the_forecasts_magnitude_does_not_raise_the_bias():
    """A model predicting 40% is not more trustworthy, it is more likely broken."""
    subject = a_weigher(maximum_bias=0.04, minimum=20)
    for _ in range(100):
        subject.observe_forecast_outcome("ensemble", "trending", True)
    subject.observe_forecast(VENUE, SYMBOL, 0.02)
    modest = subject.weigh(VENUE, SYMBOL, "ensemble", "trending").bias
    subject.observe_forecast(VENUE, SYMBOL, 0.40)
    wild = subject.weigh(VENUE, SYMBOL, "ensemble", "trending").bias
    assert modest == wild


def test_trust_is_kept_per_condition():
    subject = a_weigher(minimum=20, minimum_trust=0.6)
    for _ in range(100):
        subject.observe_forecast_outcome("ensemble", "trending", True)
        subject.observe_forecast_outcome("ensemble", "chop", False)
    subject.observe_forecast(VENUE, SYMBOL, 0.05)
    assert subject.weigh(VENUE, SYMBOL, "ensemble", "trending").bias > 0
    assert subject.weigh(VENUE, SYMBOL, "ensemble", "chop").bias == 0.0


def test_a_short_intent_reads_the_bias_the_other_way():
    subject = a_weigher(minimum=20)
    for _ in range(100):
        subject.observe_forecast_outcome("ensemble", "trending", True)
    subject.observe_forecast(VENUE, SYMBOL, -0.05)
    assert subject.weigh(VENUE, SYMBOL, "ensemble", "trending").bias < 0


# ---- size-hint-writer -------------------------------------------------------

class CalibratedStub:
    def __init__(self, bot, probability, measured=True):
        self.bot, self.venue_id, self.symbol = bot, VENUE, SYMBOL
        self.probability = probability
        self.is_measured = measured
        self.calibrated = an_estimate(probability, 200 if measured else 0, measured)


def a_hint_writer(floor=0.25, ceiling=2.0, reference=0.6, agreement=1.2, sole=0.7, unmeasured=0.5):
    return SizeHintWriter(
        floor_multiple=floor, ceiling_multiple=ceiling, conviction_reference=reference,
        agreement_multiple=agreement, sole_opinion_multiple=sole,
        unmeasured_multiple=unmeasured,
    )


def test_a_hint_is_a_multiple_never_a_notional():
    """Sizing without knowing the rest of the book is how a book concentrates."""
    hint = a_hint_writer().write(an_intent())
    assert hasattr(hint, "multiple_of_normal")
    assert not hasattr(hint, "notional")


def test_the_hint_uses_the_bots_calibrated_numbers_not_the_blend():
    subject = a_hint_writer(reference=0.6, agreement=1.0)
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.3))
    subject.observe_calibrated_conviction(CalibratedStub(TAIL, 0.3))
    hint = subject.write(an_intent(conviction=0.9))
    assert hint.multiple_of_normal < 1.0
    assert "own calibrated numbers" in hint.reason


def test_an_unmeasured_conviction_sizes_smaller():
    subject = a_hint_writer(unmeasured=0.5, agreement=1.0)
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.6, measured=False))
    small = subject.write(an_intent(bots=(BULL,), agreement=SOLE_OPINION)).multiple_of_normal
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.6, measured=True))
    larger = subject.write(an_intent(bots=(BULL,), agreement=SOLE_OPINION)).multiple_of_normal
    assert small < larger


def test_agreement_sizes_up_and_a_lone_opinion_sizes_down():
    subject = a_hint_writer(agreement=1.5, sole=0.5)
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.6))
    subject.observe_calibrated_conviction(CalibratedStub(TAIL, 0.6))
    together = subject.write(an_intent(agreement=UNANIMOUS)).multiple_of_normal
    alone = subject.write(an_intent(bots=(BULL,), agreement=SOLE_OPINION)).multiple_of_normal
    assert together > alone


def test_the_hint_is_floored_and_capped_and_says_which():
    subject = a_hint_writer(floor=0.5, ceiling=1.5, reference=0.5, agreement=1.0)
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.99))
    subject.observe_calibrated_conviction(CalibratedStub(TAIL, 0.99))
    big = subject.write(an_intent())
    assert big.multiple_of_normal == 1.5
    assert "ceiling" in big.reason
    assert subject.standing.clipped_at_ceiling == 1


def test_a_floor_of_zero_is_refused_at_construction():
    """A position too small teaches the system nothing."""
    with pytest.raises(ValueError):
        SizeHintWriter(
            floor_multiple=0.0, ceiling_multiple=2.0, conviction_reference=0.6,
            agreement_multiple=1.2, sole_opinion_multiple=0.7, unmeasured_multiple=0.5,
        )


def test_the_scorecard_band_record_is_reported():
    scorecard = BotScorecard(bot=BULL)
    for index in range(50):
        scorecard.record_closed_trade("d", "reverting", 0.75, index % 2 == 0, 1.0)
    subject = a_hint_writer()
    subject.observe_scorecard(BULL, scorecard)
    subject.observe_calibrated_conviction(CalibratedStub(BULL, 0.75))
    hint = subject.write(an_intent(bots=(BULL,)))
    assert "have resolved" in hint.reason


# ---- intent-timing-gate -----------------------------------------------------

def a_gate(validity=30.0, drift=0.01, clock=None):
    return IntentTimingGate(
        validity_seconds=validity, maximum_price_drift_fraction=drift,
        now_ns=clock or Clock(),
    )


def a_timing(bot=BULL, action=ENTER_NOW, trigger=100.0):
    return EntryTiming(
        bot=bot, venue_id=VENUE, symbol=SYMBOL, side=LONG, action=action,
        trigger_price=trigger, valid_until_ns=None, quality=None,
        reason="timed", decided_at_ns=Clock()(),
    )


def test_every_timed_intent_carries_an_expiry():
    """An intent with no expiry is a trade taken later by aged-out reasoning."""
    subject = a_gate(validity=30.0)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_bot_timing(BULL, a_timing())
    timed = subject.gate(an_intent(bots=(BULL,)))
    assert timed.act_now
    assert timed.valid_until_ns is not None
    assert timed.waited_for == ACT_NOW


def test_an_expired_intent_is_detected():
    clock = Clock()
    subject = a_gate(validity=10.0, clock=clock)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_bot_timing(BULL, a_timing())
    timed = subject.gate(an_intent(bots=(BULL,)))
    assert subject.has_expired(timed) is False
    clock.advance_seconds(11)
    assert subject.has_expired(timed) is True


def test_the_strictest_bot_timing_wins():
    """Acting now on a view half of whose evidence says it is early is the worst of both."""
    subject = a_gate()
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_bot_timing(BULL, a_timing(BULL, ENTER_NOW))
    subject.observe_bot_timing(TAIL, a_timing(TAIL, WAIT_FOR_TRIGGER, trigger=98.0))
    timed = subject.gate(an_intent(bots=(BULL, TAIL)))
    assert timed.act_now is False
    assert timed.trigger_price == 98.0
    assert timed.waited_for == WAITING_ON_A_BOT


def test_price_moving_past_the_decision_refuses_the_intent():
    """A conviction formed at one price is not the same conviction two percent away."""
    subject = a_gate(drift=0.01)
    subject.record_decision_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 105.0, subject._now_ns())
    subject.observe_bot_timing(BULL, a_timing())
    timed = subject.gate(an_intent(bots=(BULL,)))
    assert timed.act_now is False
    assert timed.waited_for == PRICE_HAS_MOVED_PAST_THE_DECISION
    assert timed.valid_until_ns is None


def test_a_bot_standing_down_since_the_intent_stops_it():
    subject = a_gate()
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_bot_timing(BULL, a_timing(BULL, STAND_DOWN))
    assert subject.gate(an_intent(bots=(BULL,))).waited_for == A_BOT_STOOD_DOWN_ON_TIMING


def test_no_price_means_no_moment_to_judge():
    assert a_gate().gate(an_intent()).waited_for == NO_PRICE


def test_a_gate_with_no_expiry_is_refused_at_construction():
    with pytest.raises(ValueError):
        IntentTimingGate(validity_seconds=0.0, maximum_price_drift_fraction=0.01)


# ---- intent-explainer -------------------------------------------------------

def an_explainer(tolerance=0.02, sentences=3):
    return IntentExplainer(relative_tolerance=tolerance, maximum_sentences=sentences)


def test_a_rationale_keeps_only_what_traces_to_a_measurement():
    subject = an_explainer()
    intent = an_intent(conviction=0.72)
    rationale = subject.explain(
        intent, [an_opinion(BULL, conviction=0.8)],
        "Weighted conviction was 72%. The order book was 4.4 times deeper than usual.",
    )
    assert "72%" in rationale.body
    assert "4.4" not in rationale.body
    assert rationale.is_fully_supported is False
    assert rationale.citations["72%"] == "weighted_conviction"


def test_a_rationale_is_written_even_with_no_model():
    """A decision with no recorded reason is worse than one recorded bluntly."""
    rationale = an_explainer().explain(an_intent(), [an_opinion(BULL)], None)
    assert rationale.body
    assert rationale.was_written_by_a_model is False


def test_missing_feature_attribution_is_stated_rather_than_omitted():
    rationale = an_explainer().explain(an_intent(), [an_opinion(BULL)], None)
    assert "not known" in rationale.body


def test_attribution_when_present_becomes_citable_facts():
    subject = an_explainer()
    subject.observe_feature_attribution(VENUE, SYMBOL, {"book_imbalance": 0.42})
    facts = subject.facts_for(an_intent(), [an_opinion(BULL)])
    assert facts["attribution_book_imbalance"] == 0.42


def test_the_request_carries_the_facts_it_will_be_checked_against():
    request = an_explainer().request(an_intent(), [an_opinion(BULL)])
    assert request.is_answerable_from_facts
    assert "weighted_conviction" in request.facts


# ---- devils-advocate --------------------------------------------------------

def an_advocate(veto_above=0.7, minimum=20):
    return DevilsAdvocate(
        veto_when_objection_hit_rate_above=veto_above, minimum_observations=minimum,
        prior_objection_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        relative_tolerance=0.02, maximum_sentences=3,
    )


def test_an_unmeasured_conviction_is_objected_to_without_any_model():
    """The trades taken while the model is down are the ones nobody reviewed."""
    subject = an_advocate()
    argument = subject.argue(
        an_intent(measured=False), [an_opinion(BULL)], "reverting"
    )
    assert argument.objections
    assert subject.standing.by_objection[CONVICTION_IS_UNMEASURED] == 1


def test_a_lone_unproven_bot_is_objected_to():
    subject = an_advocate(minimum=50)
    subject.observe_bot_maturity(BULL, "reverting", 3)
    subject.argue(an_intent(bots=(BULL,)), [an_opinion(BULL)], "reverting")
    assert subject.standing.by_objection[SOLE_UNPROVEN_BOT] == 1


def test_an_overruled_dissent_is_an_objection():
    subject = an_advocate()
    subject.argue(an_intent(dissenting=(BEAR,)), [an_opinion(BULL)], "reverting")
    assert subject.standing.by_objection[DISSENT_WAS_OVERRULED] == 1


def test_an_intent_with_no_stop_is_objected_to():
    subject = an_advocate()
    subject.argue(an_intent(stop=None), [an_opinion(BULL)], "reverting")
    assert subject.standing.by_objection[NO_STOP] == 1


def test_an_objection_with_a_record_can_veto():
    """An objection that could only lower a score would be absorbed."""
    subject = an_advocate(veto_above=0.6, minimum=10)
    for _ in range(50):
        subject.observe_objection_outcome(CONVICTION_IS_UNMEASURED, "reverting", True)
    argument = subject.argue(an_intent(measured=False), [an_opinion(BULL)], "reverting")
    assert argument.would_reverse_the_decision is True
    assert subject.standing.vetoes == 1


def test_an_objection_with_no_record_does_not_veto():
    subject = an_advocate(veto_above=0.6, minimum=100)
    argument = subject.argue(an_intent(measured=False), [an_opinion(BULL)], "reverting")
    assert argument.would_reverse_the_decision is False


def test_a_model_objection_citing_nothing_is_removed():
    subject = an_advocate()
    subject.argue(
        an_intent(), [an_opinion(BULL)], "reverting",
        model_output="The book is 9.9 times thinner than normal.",
    )
    assert subject.standing.objections_removed_as_unsupported == 1


def test_no_objection_surviving_is_recorded_rather_than_silence():
    """A silent advocate is indistinguishable from one that was not run."""
    subject = an_advocate()
    argument = subject.argue(an_intent(), [an_opinion(BULL)], "reverting")
    assert argument.objections == ()
    assert argument.reason
    assert subject.standing.trades_with_no_objection == 1


# ---- premortem-writer -------------------------------------------------------

def a_premortem_writer():
    return PremortemWriter(relative_tolerance=0.02, maximum_sentences=4)


def test_a_premortem_lists_failure_modes_with_no_model_at_all():
    note = a_premortem_writer().write(an_intent(), [an_opinion(BULL)])
    assert note.failure_modes
    assert note.most_likely_failure
    assert note.was_written_by_a_model is False


def test_every_measured_failure_mode_names_what_would_show_it_early():
    """"funding could invert" is a note; a threshold is something a part can watch."""
    note = a_premortem_writer().write(an_intent(), [an_opinion(BULL)])
    assert len(note.what_would_show_it_early) >= 4


def test_the_expected_failure_modes_are_present():
    subject = a_premortem_writer()
    subject.write(an_intent(measured=False), [an_opinion(BULL)])
    for mode in (STOP_IS_HIT, HORIZON_PASSES, REGIME_CHANGES, CONVICTION_WAS_NOT_MEASURED):
        assert mode in subject.standing.by_failure_mode


def test_a_failure_mode_without_a_number_survives():
    """The risks that end accounts are not the quantified ones."""
    subject = a_premortem_writer()
    note = subject.write(
        an_intent(), [an_opinion(BULL)],
        model_output="The venue could halt withdrawals during the move.",
    )
    assert any("withdrawals" in mode for mode in note.failure_modes)


def test_a_model_failure_mode_citing_an_invented_number_is_still_removed():
    subject = a_premortem_writer()
    subject.write(
        an_intent(), [an_opinion(BULL)],
        model_output="Funding will reach 7.7% and the position bleeds out.",
    )
    assert subject.standing.model_sentences_removed == 1


# ---- brain-self-reflector ---------------------------------------------------

def a_reflector():
    return BrainSelfReflector(relative_tolerance=0.02, maximum_sentences=3)


def an_episode(profitable=True, how="stop-is-hit", measured=True, conviction=0.8):
    return TradeEpisode(
        venue_id=VENUE, symbol=SYMBOL, was_profitable=profitable,
        realised_fraction=0.02 if profitable else -0.02, how_it_ended=how,
        seconds_held=400.0, entry_conviction=conviction, conviction_was_measured=measured,
    )


def test_a_win_on_an_unmeasured_conviction_is_a_lucky_win():
    """The most expensive event a learning system can record as a success."""
    subject = a_reflector()
    note = subject.reflect(an_episode(profitable=True, measured=False))
    assert note.is_a_lucky_win is True
    assert subject.standing.lucky_wins == 1


def test_a_loss_that_happened_the_way_the_premortem_said_is_sound_reasoning():
    subject = a_reflector()
    subject.observe_premortem(
        a_premortem_writer().write(an_intent(), [an_opinion(BULL)])
    )
    note = subject.reflect(an_episode(profitable=False, how="stop"))
    assert note.reasoning_was_sound is True
    assert note.is_an_unlucky_loss is True
    assert FAILED_AS_PREDICTED in note.reason


def test_a_loss_for_the_reason_the_advocate_raised_is_unsound_reasoning():
    subject = a_reflector()
    subject.observe_counter_argument(
        CounterArgument(
            venue_id=VENUE, symbol=SYMBOL, objections=("the regime is about to break",),
            strongest_objection="the regime is about to break",
            would_reverse_the_decision=False, evidence_cited={},
            was_written_by_a_model=False, reason="raised", argued_at_ns=Clock()(),
        )
    )
    note = subject.reflect(an_episode(profitable=False, how="regime-break"))
    assert note.reasoning_was_sound is False
    assert FAILED_AS_ARGUED in note.reason


def test_a_win_that_was_vetoed_and_taken_anyway_is_not_a_success():
    subject = a_reflector()
    subject.observe_counter_argument(
        CounterArgument(
            venue_id=VENUE, symbol=SYMBOL, objections=("crowded",),
            strongest_objection="crowded", would_reverse_the_decision=True,
            evidence_cited={}, was_written_by_a_model=False, reason="veto",
            argued_at_ns=Clock()(),
        )
    )
    note = subject.reflect(an_episode(profitable=True))
    assert note.reasoning_was_sound is False
    assert WON_DESPITE_THE_REASONING in note.reason


def test_an_unanticipated_loss_produces_a_lesson_that_generalises():
    subject = a_reflector()
    note = subject.reflect(an_episode(profitable=False, how="exchange-outage"))
    assert note.reasoning_was_sound is None
    assert note.applies_beyond_this_trade is True
    assert FAILED_UNANTICIPATED in note.reason


def test_a_sound_win_is_recorded_as_one():
    subject = a_reflector()
    note = subject.reflect(an_episode(profitable=True, measured=True))
    assert note.reasoning_was_sound is True
    assert WON_AS_REASONED in note.reason
    assert subject.standing.sound_and_won == 1


def test_the_reflection_works_without_a_model():
    note = a_reflector().reflect(an_episode())
    assert note.lesson
    assert note.was_written_by_a_model is False


def test_a_model_lesson_citing_an_invented_number_is_removed():
    subject = a_reflector()
    subject.reflect(an_episode(), model_output="The move was 9.9% against us.")
    assert subject.standing.model_sentences_removed == 1


# ---- strategy-review-reasoner ------------------------------------------------

def a_reasoner(working_threshold=0.55, minimum_new_trades=5, minimum_observations=20):
    return StrategyReviewReasoner(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum_observations, working_threshold=working_threshold,
        minimum_new_trades=minimum_new_trades, relative_tolerance=0.02, maximum_sentences=3,
    )


def test_a_bot_is_not_due_before_enough_new_trades_close():
    subject = a_reasoner(minimum_new_trades=5)
    assert subject.due_for_review(BULL, 3) is False
    assert subject.due_for_review(BULL, 5) is True


def test_fewer_than_minimum_observations_is_unmeasured_not_a_verdict():
    """No evidence renders as its own state, never as a guess (Rule 8)."""
    subject = a_reasoner(minimum_observations=50)
    _facts, assessment, confidence = subject.prepare_review(BULL, trades=10, wins=8, realised=0.1)
    assert assessment == UNMEASURED
    assert confidence.is_fitted is False


def test_a_high_hit_rate_is_judged_working_once_fitted():
    subject = a_reasoner(minimum_observations=20, working_threshold=0.55)
    _facts, assessment, confidence = subject.prepare_review(BULL, trades=30, wins=27, realised=0.5)
    assert confidence.is_fitted is True
    assert assessment == WORKING


def test_a_low_hit_rate_is_judged_underperforming_once_fitted():
    subject = a_reasoner(minimum_observations=20, working_threshold=0.55)
    _facts, assessment, confidence = subject.prepare_review(BULL, trades=30, wins=3, realised=-0.5)
    assert confidence.is_fitted is True
    assert assessment == UNDERPERFORMING


def test_a_second_review_learns_only_from_the_new_trades():
    """The scorecard is cumulative; re-observing the same trades would double count."""
    subject = a_reasoner(minimum_observations=1)
    subject.prepare_review(BULL, trades=10, wins=10, realised=0.2)
    _facts, _assessment, confidence = subject.prepare_review(BULL, trades=15, wins=10, realised=0.2)
    assert confidence.value < 1.0, "five new losses should pull a perfect record down"


def test_the_request_carries_the_frozen_facts_the_answer_is_checked_against():
    subject = a_reasoner()
    facts, _assessment, _confidence = subject.prepare_review(BULL, trades=30, wins=27, realised=0.5)
    request = subject.request(BULL, facts)
    assert request.is_answerable_from_facts
    assert request.symbol == BULL
    assert request.facts == facts


def test_a_model_sentence_citing_nothing_is_removed():
    subject = a_reasoner()
    facts, assessment, confidence = subject.prepare_review(BULL, trades=30, wins=27, realised=0.5)
    review = subject.review(
        BULL, facts, assessment, confidence,
        model_output="This bot has a great vibe and should be trusted completely.",
    )
    assert subject.standing.model_sentences_removed == 1
    assert review.assessment == assessment


def test_no_model_falls_back_to_the_measured_facts_not_silence():
    """A review whose explanation depended on a model would go silent exactly
    when the model is down."""
    subject = a_reasoner()
    facts, assessment, confidence = subject.prepare_review(BULL, trades=30, wins=27, realised=0.5)
    review = subject.review(BULL, facts, assessment, confidence)
    assert review.reason
    assert "trades" in review.reason
