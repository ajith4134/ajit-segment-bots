"""The bull bot: what it believes, what it refuses, and what it learned to believe.

Every part in this block is checked twice over: once that it does the thing, and
once that it declines to do it when the evidence is not there. The second is the
harder half and the one that costs money when it is missing -- a bot that cannot
refuse will always find a reason to trade.

The learner is tested as a learner, not as a function. It is trained on a
relationship, then asked whether it found it; trained on noise, then asked
whether it admits it found nothing. Both matter: a model that fits noise and one
that fits nothing look identical from the outside until they are traded.

Prices come from the tape where the test is about a real series (RL-063), and
from a constructed series where the property under test is one no captured
minute contains.
"""

import importlib
import json
import math

import pytest

from parts.bull_bot.bull_conviction_calibrator import BullConvictionCalibrator
from parts.bull_bot.bull_conviction_model import (
    CHALLENGER, CHAMPION, FEATURES_FLAGGED, FORECAST_FLAGGED, NOTHING_USABLE,
    BullConvictionModel, TrainingExample,
)
from parts.bull_bot.bull_entry_timer import (
    CONVICTION_TOO_LOW as TIMER_CONVICTION_TOO_LOW, NO_PRICE, PLAYBOOK_WITHHOLDS,
    BullEntryTimer, PlaybookRule,
)
from parts.bull_bot.bull_exit_plan_proposer import (
    NO_EXCURSION_PROFILE, NO_HORIZON, NO_RANGE, REWARD_BELOW_RISK,
    BullExitPlanProposer,
    ExcursionProfile, HorizonProfile, StopAudit,
)
from parts.bull_bot.bull_feature_builder import FEATURE_NAMES, BullFeatureBuilder
from parts.bull_bot.bull_opinion_composer import BullOpinionComposer
from parts.bull_bot.bull_outlier_rejector import BullOutlierRejector
from parts.bull_bot.bull_position_invalidation_watcher import (
    FEATURES_REVERSED, HORIZON_EXPIRED, NO_ENTRY_RECORD, REGIME_BROKEN, STILL_VALID,
    BullPositionInvalidationWatcher, HeldThesis,
)
from parts.bull_bot.bull_setup_filter import (
    SETUP_DISCOUNTED, WEIGHTED_STRENGTH_TOO_LOW, WRONG_SIDE, BullSetupFilter,
)
from parts.bull_bot.bull_setup_weight_learner import BullSetupWeightLearner
from runtime.online_learner import ModelBelief
from runtime.bot_opinion import (
    CLOSE_POSITION, CONVICTION_TOO_LOW, ENTER_NOW, FEATURES_INCOMPLETE, LONG,
    NO_EXIT_PLAN, REDUCE_POSITION, SHORT, STAND_DOWN, TIMING_REFUSED,
    WAIT_FOR_TRIGGER, BotScorecard, FeatureVector, SideCandidate,
)
from runtime.learned_estimator import Estimate
from runtime.market_signal import CONTINUATION, REVERSION, make_candidate
from runtime.online_learner import (
    OnlineLogisticModel, ProbabilityCalibrator, RunningMoments, logistic, log_odds,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "bull-setup-filter": "parts.bull_bot.bull_setup_filter",
    "bull-feature-builder": "parts.bull_bot.bull_feature_builder",
    "bull-outlier-rejector": "parts.bull_bot.bull_outlier_rejector",
    "bull-conviction-model": "parts.bull_bot.bull_conviction_model",
    "bull-conviction-calibrator": "parts.bull_bot.bull_conviction_calibrator",
    "bull-entry-timer": "parts.bull_bot.bull_entry_timer",
    "bull-exit-plan-proposer": "parts.bull_bot.bull_exit_plan_proposer",
    "bull-opinion-composer": "parts.bull_bot.bull_opinion_composer",
    "bull-setup-weight-learner": "parts.bull_bot.bull_setup_weight_learner",
    "bull-position-invalidation-watcher": "parts.bull_bot.bull_position_invalidation_watcher",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
DETECTOR = "mean-reversion-detector"


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_seconds(self, seconds):
        self.now_ns += int(seconds * 1e9)


def an_estimate(value, observations, is_fitted, reason):
    return Estimate(
        value=value, is_fitted=is_fitted, observations=observations, prior=0.5,
        was_clamped=False, bound_low=None, bound_high=None, reason=reason,
    )


def fitted(value, observations=100):
    return an_estimate(value, observations, True, "measured")


def unfitted(value):
    return an_estimate(value, 0, False, "the prior")


def a_candidate(direction=LONG, strength=3.0, detector=DETECTOR, confidence=None):
    return make_candidate(
        detector=detector, venue_id=VENUE, symbol=SYMBOL, direction=direction,
        expectation=REVERSION, signal_strength=strength,
        confidence=confidence or fitted(0.6), horizon_seconds=300.0,
        evidence={"z_score": -2.5}, reason="stretched",
    )


def a_side_candidate(detector=DETECTOR, weight=1.0, strength=3.0, confidence=None, clock=None):
    return SideCandidate(
        bot="bull-bot", side=LONG, venue_id=VENUE, symbol=SYMBOL, detector=detector,
        expectation=REVERSION, signal_strength=strength,
        detector_confidence=confidence or fitted(0.6), setup_weight=weight,
        horizon_seconds=300.0, evidence={}, reason="accepted",
        accepted_at_ns=(clock or Clock())(),
    )


def a_vector(features=None, missing=(), clock=None):
    return FeatureVector(
        bot="bull-bot", venue_id=VENUE, symbol=SYMBOL,
        features=features if features is not None else {"price_z_score": -2.0},
        missing=tuple(missing), sources={}, built_at_ns=(clock or Clock())(),
    )


@pytest.fixture(scope="module")
def real_trade_prices(read_captured_payloads):
    prices = []
    for _, payload in read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl"):
        message = json.loads(payload)
        if message.get("e") == "aggTrade":
            prices.append(float(message["p"]))
    assert len(prices) >= 500
    return prices


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_a_peer_bot(part_id):
    """R-03: the three bots are peers and must never wire to one another."""
    source = importlib.import_module(BLOCK_PARTS[part_id]).__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "parts.bear_bot" not in text
    assert "parts.profit_tailgating_bot" not in text


# ---- the learner ------------------------------------------------------------

def test_a_logistic_saturates_without_overflowing():
    assert logistic(10_000.0) < 1.0
    assert logistic(-10_000.0) > 0.0
    assert logistic(0.0) == pytest.approx(0.5)
    assert log_odds(logistic(1.5)) == pytest.approx(1.5, abs=1e-6)


def test_running_moments_refuse_to_standardise_a_feature_that_has_not_moved():
    """Zero spread makes every value infinitely unusual, which is arithmetic."""
    moments = RunningMoments(half_life_observations=50)
    for _ in range(30):
        moments.observe(5.0)
    assert moments.standardise(6.0, minimum_observations=10) is None


def test_running_moments_forget_a_regime_that_ended():
    moments = RunningMoments(half_life_observations=20)
    for _ in range(200):
        moments.observe(100.0)
    for _ in range(200):
        moments.observe(0.0)
    assert moments.mean < 1.0, "a mean over all history describes a regime that ended"


def test_a_model_finds_a_relationship_that_is_there():
    model = OnlineLogisticModel(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=20,
    )
    for index in range(400):
        signal = 1.0 if index % 2 else -1.0
        model.train({"signal": signal, "noise": (index % 7) - 3.0}, outcome=signal > 0)
    assert model.is_fitted
    assert model.weights["signal"] > 0
    assert abs(model.weights["signal"]) > abs(model.weights["noise"])
    assert model.believe({"signal": 1.0, "noise": 0.0}).probability > 0.8
    assert model.believe({"signal": -1.0, "noise": 0.0}).probability < 0.2


def test_a_model_trained_on_noise_does_not_claim_to_have_found_anything():
    """A model that fits noise and one that fits nothing look identical until traded."""
    model = OnlineLogisticModel(
        learning_rate=0.05, l2_regularisation=0.01, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=20,
    )
    for index in range(600):
        model.train({"signal": (index * 7919 % 101) / 50.0 - 1.0}, outcome=index % 2 == 0)
    belief = model.believe({"signal": 1.0})
    assert 0.3 < belief.probability < 0.7


def test_a_model_that_has_only_ever_won_is_not_fitted():
    """500 wins and no losses has taught it that everything wins."""
    model = OnlineLogisticModel(
        learning_rate=0.1, l2_regularisation=0.0, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=20,
    )
    for _ in range(500):
        model.train({"signal": 1.0}, outcome=True)
    assert model.is_fitted is False
    assert "one class" in model.believe({"signal": 1.0}).reason


def test_a_weight_from_a_dead_regime_decays_even_when_its_feature_stops_appearing():
    model = OnlineLogisticModel(
        learning_rate=0.2, l2_regularisation=0.05, feature_half_life_observations=100,
        minimum_feature_observations=3, minimum_training_observations=10,
    )
    for index in range(200):
        model.train({"old": 1.0 if index % 2 else -1.0}, outcome=index % 2 == 1)
    learned = abs(model.weights["old"])
    for index in range(500):
        model.train({"new": 1.0 if index % 2 else -1.0}, outcome=index % 2 == 1)
    assert abs(model.weights["old"]) < learned / 2


def test_a_belief_names_the_feature_that_moved_it_most():
    model = OnlineLogisticModel(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=20,
    )
    for index in range(300):
        signal = 1.0 if index % 2 else -1.0
        model.train({"signal": signal, "noise": 0.5}, outcome=signal > 0)
    belief = model.believe({"signal": 2.0, "noise": 0.5})
    assert belief.strongest_reason[0] == "signal"


def test_a_feature_with_no_history_is_left_out_rather_than_guessed():
    model = OnlineLogisticModel(
        learning_rate=0.1, l2_regularisation=0.0, feature_half_life_observations=100,
        minimum_feature_observations=10, minimum_training_observations=5,
    )
    belief = model.believe({"never_seen": 3.0})
    assert belief.features_used == 0
    assert belief.features_unusable == ("never_seen",)


# ---- the calibrator ---------------------------------------------------------

def test_a_calibrator_passes_the_model_through_until_it_has_evidence():
    calibrator = ProbabilityCalibrator(bin_count=10, minimum_observations=50, half_life_observations=500)
    estimate = calibrator.calibrate(0.9)
    assert estimate.value == 0.9
    assert estimate.is_fitted is False
    assert "must not be read as a measured frequency" in estimate.reason


def test_a_calibrator_learns_that_a_model_is_overconfident():
    calibrator = ProbabilityCalibrator(bin_count=10, minimum_observations=50, half_life_observations=5000)
    for index in range(400):
        calibrator.observe_outcome(0.9, outcome=index % 2 == 0)
    estimate = calibrator.calibrate(0.9)
    assert estimate.is_fitted
    assert estimate.value == pytest.approx(0.5, abs=0.05)


def test_calibration_never_maps_a_higher_score_to_a_lower_frequency():
    """Without pooling this is noise fitted per bin, and it will invert the model."""
    calibrator = ProbabilityCalibrator(bin_count=10, minimum_observations=20, half_life_observations=100_000)
    for _ in range(30):
        calibrator.observe_outcome(0.25, outcome=True)
    for _ in range(30):
        calibrator.observe_outcome(0.75, outcome=False)
    frequencies = [band["observed_frequency"] for band in calibrator.reliability()]
    assert frequencies == sorted(frequencies)


def test_a_band_nothing_landed_in_is_carried_rather_than_counted_as_zero():
    calibrator = ProbabilityCalibrator(bin_count=10, minimum_observations=10, half_life_observations=100_000)
    for _ in range(20):
        calibrator.observe_outcome(0.85, outcome=True)
    assert calibrator.calibrate(0.15).value > 0.0


# ---- bull-setup-filter ------------------------------------------------------

def a_filter(default=1.0, floor=0.2, strength_floor=1.0):
    return BullSetupFilter(
        default_setup_weight=default, minimum_setup_weight=floor,
        minimum_weighted_strength=strength_floor,
    )


def test_a_short_candidate_is_not_a_weak_long():
    accepted, outcome = a_filter().filter_candidate(a_candidate(direction=SHORT))
    assert accepted is None
    assert outcome == WRONG_SIDE


def test_a_long_candidate_carries_the_learned_weight_forward():
    subject = a_filter()
    subject.observe_setup_weight(DETECTOR, 1.4)
    accepted, _ = subject.filter_candidate(a_candidate())
    assert accepted.setup_weight == 1.4
    assert accepted.side == LONG
    assert accepted.detector == DETECTOR


def test_a_detector_discounted_below_the_floor_stops_costing_the_bot_anything():
    subject = a_filter(floor=0.5)
    subject.observe_setup_weight(DETECTOR, 0.1)
    accepted, outcome = subject.filter_candidate(a_candidate())
    assert accepted is None
    assert outcome == SETUP_DISCOUNTED


def test_a_weak_signal_from_a_trusted_detector_still_fails_the_strength_floor():
    subject = a_filter(strength_floor=2.0)
    subject.observe_setup_weight(DETECTOR, 1.5)
    accepted, outcome = subject.filter_candidate(a_candidate(strength=0.1))
    assert accepted is None
    assert outcome == WEIGHTED_STRENGTH_TOO_LOW


def test_an_unproven_detector_starts_at_the_default_rather_than_at_zero():
    """A detector weighted zero can never be found to work."""
    subject = a_filter(default=1.0)
    assert subject.weight_for("never-judged") == 1.0
    accepted, _ = subject.filter_candidate(a_candidate(detector="never-judged"))
    assert accepted is not None


def test_a_negative_weight_is_refused_rather_than_inverting_the_detector():
    with pytest.raises(ValueError):
        a_filter().observe_setup_weight(DETECTOR, -1.0)


# ---- bull-feature-builder ---------------------------------------------------

def a_builder(minimum=5):
    return BullFeatureBuilder(
        short_window=10, long_window=50, minimum_observations=minimum,
        reference_order_size_quote=1000.0,
    )


def test_a_feature_that_could_not_be_measured_is_named_not_defaulted():
    """The rule that costs most and matters most: zero would teach the model."""
    subject = a_builder()
    vector = subject.build(a_side_candidate())
    assert "funding_rate" in vector.missing
    assert "funding_rate" not in vector.features
    assert vector.is_complete is False


def test_a_complete_vector_names_where_every_feature_came_from(real_trade_prices):
    subject = a_builder(minimum=5)
    for price in real_trade_prices[:60]:
        subject.observe_price(VENUE, SYMBOL, price)
    subject.observe_book(VENUE, SYMBOL, bids=((77400.0, 3.0),), asks=((77420.0, 1.0),))
    subject.observe_funding(VENUE, SYMBOL, 0.0001)
    subject.observe_funding_forecast(VENUE, SYMBOL, 0.0004)
    vector = subject.build(a_side_candidate())
    assert vector.is_complete, f"still missing {vector.missing}"
    assert set(vector.features) <= set(FEATURE_NAMES)
    assert set(vector.sources) == set(vector.features)


def test_every_feature_is_a_fraction_not_a_price(real_trade_prices):
    """A model trained on absolute prices learns the price level it was trained at."""
    subject = a_builder(minimum=5)
    for price in real_trade_prices[:60]:
        subject.observe_price(VENUE, SYMBOL, price)
    subject.observe_book(VENUE, SYMBOL, bids=((77400.0, 3.0),), asks=((77420.0, 1.0),))
    subject.observe_funding(VENUE, SYMBOL, 0.0001)
    subject.observe_funding_forecast(VENUE, SYMBOL, 0.0004)
    vector = subject.build(a_side_candidate())
    assert max(abs(value) for value in vector.features.values()) < 1000


def test_book_imbalance_reads_the_side_that_is_heavier():
    subject = a_builder()
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 10.0),), asks=((101.0, 1.0),))
    bid_heavy = subject.build(a_side_candidate()).features["book_imbalance"]
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 1.0),), asks=((101.0, 10.0),))
    ask_heavy = subject.build(a_side_candidate()).features["book_imbalance"]
    assert bid_heavy > 0 > ask_heavy


def test_an_unfitted_detector_hit_rate_is_missing_rather_than_its_prior():
    subject = a_builder()
    vector = subject.build(a_side_candidate(confidence=unfitted(0.5)))
    assert "detector_hit_rate" in vector.missing


def test_a_short_window_no_longer_than_the_long_one_is_refused():
    with pytest.raises(ValueError):
        BullFeatureBuilder(
            short_window=50, long_window=50, minimum_observations=5,
            reference_order_size_quote=1000.0,
        )


# ---- bull-outlier-rejector --------------------------------------------------

def a_rejector(threshold=4.0, minimum=20, unjudgeable=0.5):
    return BullOutlierRejector(
        deviation_threshold=threshold, minimum_observations=minimum,
        half_life_observations=500, maximum_unjudgeable_fraction=unjudgeable,
    )


def teach_normal(rejector, count=100, name="price_z_score"):
    for index in range(count):
        rejector.observe_vector(a_vector({name: 1.0 if index % 2 else -1.0}))


def test_a_vector_like_everything_seen_before_is_not_flagged():
    subject = a_rejector()
    teach_normal(subject)
    flag = subject.judge(a_vector({"price_z_score": 0.5}))
    assert flag.is_out_of_distribution is False


def test_an_extreme_feature_is_flagged_and_named():
    subject = a_rejector(threshold=4.0)
    teach_normal(subject)
    flag = subject.judge(a_vector({"price_z_score": 40.0}))
    assert flag.is_out_of_distribution is True
    assert flag.worst_feature == "price_z_score"
    assert flag.worst_deviation > 4.0


def test_a_symbol_nothing_is_known_about_is_refused_not_waved_through():
    """Otherwise the symbol least is known about passes most easily."""
    flag = a_rejector().judge(a_vector({"brand_new_feature": 1.0}))
    assert flag.is_out_of_distribution is True
    assert flag.features_judged == 0


def test_a_vector_mostly_unrecognised_is_refused_even_if_the_rest_looks_normal():
    subject = a_rejector(unjudgeable=0.4)
    teach_normal(subject)
    flag = subject.judge(a_vector({"price_z_score": 0.5, "a": 1.0, "b": 1.0, "c": 1.0}))
    assert flag.is_out_of_distribution is True
    assert len(flag.features_unjudgeable) == 3


def test_todays_outlier_becomes_tomorrows_normal():
    """A rejector that only learned from what it accepted would reject the new market."""
    subject = a_rejector(threshold=4.0, minimum=20)
    teach_normal(subject)
    assert subject.judge(a_vector({"price_z_score": 30.0})).is_out_of_distribution
    for _ in range(2000):
        subject.observe_vector(a_vector({"price_z_score": 30.0}))
        subject.observe_vector(a_vector({"price_z_score": 28.0}))
    assert subject.judge(a_vector({"price_z_score": 29.0})).is_out_of_distribution is False


def test_the_normal_range_can_be_read_back():
    subject = a_rejector()
    teach_normal(subject)
    low, high = subject.normal_range("price_z_score")
    assert low < 0 < high


# ---- bull-conviction-model --------------------------------------------------

def a_model(minimum_training=20, clock=None):
    return BullConvictionModel(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=minimum_training,
        default_sample_weight=1.0, maximum_sample_weight=5.0,
        now_ns=clock or Clock(),
    )


def teach_the_model(model, rounds=300):
    for index in range(rounds):
        signal = 1.0 if index % 2 else -1.0
        model.train_from_label({"price_z_score": signal}, label=signal > 0, source=DETECTOR)


def test_a_flagged_vector_gets_no_conviction_at_all():
    """The model's answer there would be an extrapolation wearing the type of a measurement."""
    subject = a_model()
    teach_the_model(subject)
    conviction, outcome = subject.form_conviction(a_vector(), is_out_of_distribution=True)
    assert conviction is None
    assert outcome == FEATURES_FLAGGED


def test_a_flagged_price_forecast_stops_the_conviction_too():
    subject = a_model()
    teach_the_model(subject)
    subject.observe_forecast_flag(VENUE, SYMBOL, True)
    conviction, outcome = subject.form_conviction(a_vector(), is_out_of_distribution=False)
    assert conviction is None
    assert outcome == FORECAST_FLAGGED


def test_a_vector_of_features_the_model_cannot_standardise_gets_nothing():
    subject = a_model()
    conviction, outcome = subject.form_conviction(a_vector({"unseen": 1.0}), False)
    assert conviction is None
    assert outcome == NOTHING_USABLE


def test_a_conviction_explains_itself():
    subject = a_model()
    teach_the_model(subject)
    conviction, _ = subject.form_conviction(a_vector({"price_z_score": 1.0}), False)
    assert conviction.side == LONG
    assert conviction.belief.is_fitted
    assert "price_z_score" in conviction.reason


def test_both_models_train_but_only_the_champion_is_believed():
    subject = a_model()
    teach_the_model(subject)
    assert subject.model(CHAMPION).observations == subject.model(CHALLENGER).observations
    assert subject.live_model_name == CHAMPION


def test_promotion_is_delivered_never_taken():
    """T-2: which model is live is a control decision made elsewhere."""
    subject = a_model()
    teach_the_model(subject)
    subject.apply_champion_choice(CHALLENGER)
    assert subject.live_model_name == CHALLENGER
    assert subject.standing.champion_swaps == 1


def test_the_live_model_cannot_be_wiped_while_it_is_being_acted_on():
    subject = a_model()
    teach_the_model(subject)
    with pytest.raises(ValueError):
        subject.apply_retrain_request(CHAMPION)
    subject.apply_retrain_request(CHALLENGER)
    assert subject.model(CHALLENGER).observations == 0
    assert subject.model(CHAMPION).observations > 0


def test_a_learning_reward_scales_how_much_an_outcome_counts():
    subject = a_model()
    subject.observe_learning_reward(DETECTOR, 3.0)
    unrewarded = a_model()
    subject.train_from_label({"price_z_score": 2.0}, label=True, source=DETECTOR)
    unrewarded.train_from_label({"price_z_score": 2.0}, label=True, source=DETECTOR)
    assert abs(subject.model(CHAMPION).bias) > abs(unrewarded.model(CHAMPION).bias)


def test_a_reward_cannot_exceed_the_cap_it_was_given():
    subject = a_model()
    subject.observe_learning_reward(DETECTOR, 500.0)
    subject.train(TrainingExample({"price_z_score": 1.0}, True, 1.0, DETECTOR))
    assert subject.model(CHAMPION).observations == 1


def test_a_kline_window_becomes_shape_features():
    subject = a_model()
    for index in range(40):
        signal = 1.0 if index % 2 else -1.0
        subject.train_from_label(
            {"price_z_score": signal, "kline_position_in_range": 0.2 + 0.1 * (index % 5)},
            signal > 0, DETECTOR,
        )
    subject.observe_kline_window(VENUE, SYMBOL, closes=[100.0, 105.0], highs=[106.0], lows=[99.0])
    conviction, _ = subject.form_conviction(a_vector({"price_z_score": 1.0}), False)
    assert "kline_position_in_range" in conviction.belief.contributions


# ---- bull-conviction-calibrator ---------------------------------------------

class RawStub:
    def __init__(self, probability, trained_on=1000):
        self.venue_id, self.symbol, self.side = VENUE, SYMBOL, LONG
        self.probability = probability
        # The belief the model formed, which the calibrator carries through so a
        # composer can ask whether the *model* is trained without reaching into
        # the model's part. A stub without it is not the type the calibrator
        # declares it consumes.
        self.belief = ModelBelief(
            probability=probability, score=0.0, contributions={}, features_used=4,
            features_unusable=(), observations_trained_on=trained_on,
            is_fitted=trained_on >= 100,
            reason="stubbed for this test",
        )


def a_bull_calibrator(minimum=50):
    return BullConvictionCalibrator(
        bin_count=10, minimum_observations=minimum, half_life_observations=5000
    )


def test_an_unfitted_calibrator_passes_the_model_through_and_says_so():
    result = a_bull_calibrator().calibrate(RawStub(0.85))
    assert result.probability == 0.85
    assert result.is_measured is False
    assert "passed through unchanged" in result.reason


def test_a_calibrator_corrects_an_overconfident_model():
    subject = a_bull_calibrator(minimum=50)
    for index in range(300):
        subject.observe_outcome(0.85, was_right=index % 3 == 0)
    result = subject.calibrate(RawStub(0.85))
    assert result.is_measured
    assert result.probability < 0.5
    assert result.raw_probability == 0.85


def test_calibration_is_kept_per_regime():
    """Well calibrated on average and overconfident in a trend is overconfident when sizing up."""
    subject = a_bull_calibrator(minimum=30)
    for index in range(200):
        subject.observe_outcome(0.8, was_right=True, regime="reverting")
    for index in range(200):
        subject.observe_outcome(0.8, was_right=False, regime="trending")
    reverting = subject.calibrate(RawStub(0.8), regime="reverting")
    trending = subject.calibrate(RawStub(0.8), regime="trending")
    assert reverting.probability > trending.probability


def test_an_unfitted_regime_falls_back_to_the_overall_record_not_to_a_guess():
    subject = a_bull_calibrator(minimum=30)
    for index in range(200):
        subject.observe_outcome(0.8, was_right=index % 2 == 0)
    result = subject.calibrate(RawStub(0.8), regime="a-regime-never-traded")
    assert result.is_measured
    assert result.probability == pytest.approx(0.5, abs=0.1)


def test_a_restarted_calibrator_adopts_the_scorecard_rather_than_relearning():
    scorecard = BotScorecard(bot="bull-bot")
    for index in range(200):
        scorecard.record_closed_trade(DETECTOR, "reverting", 0.85, index % 4 == 0, 1.0)
    subject = a_bull_calibrator(minimum=50)
    subject.observe_scorecard(scorecard)
    assert subject.calibrate(RawStub(0.85)).is_measured


# ---- bull-entry-timer -------------------------------------------------------

def a_timer(minimum_conviction=0.55, clock=None, cap=0.01):
    return BullEntryTimer(
        minimum_conviction=minimum_conviction, window_length=50, minimum_observations=10,
        trigger_validity_seconds=60.0, maximum_extension_quantile=0.8,
        entry_quality_window=200, prior_extension_cap=cap,
        prior_entry_cost_fraction=0.0005, now_ns=clock or Clock(),
    )


class ConvictionStub:
    def __init__(self, probability, measured=True):
        self.probability = probability
        self.is_measured = measured


def test_a_setup_the_bot_does_not_believe_is_not_timed():
    subject = a_timer(minimum_conviction=0.6)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.5))
    assert timing.action == STAND_DOWN
    assert subject.standing.by_refusal[TIMER_CONVICTION_TOO_LOW] == 1


def test_a_symbol_with_no_price_has_no_moment_to_judge():
    subject = a_timer()
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert timing.action == STAND_DOWN
    assert NO_PRICE in subject.standing.by_refusal


def test_a_price_at_its_mean_is_entered_now():
    clock = Clock()
    subject = a_timer(clock=clock)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert timing.action == ENTER_NOW
    assert timing.valid_until_ns is not None


def test_an_extended_price_waits_for_a_level_and_the_level_is_below_here():
    subject = a_timer(cap=0.005)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 120.0)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert timing.action == WAIT_FOR_TRIGGER
    assert timing.trigger_price < 120.0


def test_every_waiting_intention_expires():
    """A trigger with no expiry fires hours later into a market that has changed."""
    clock = Clock()
    subject = a_timer(clock=clock, cap=0.005)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 120.0)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert subject.has_expired(timing) is False
    clock.advance_seconds(61)
    assert subject.has_expired(timing) is True


def test_a_playbook_rule_can_withhold_entry():
    subject = a_timer()
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, pullback_fraction=None, withholds=True, reason="never worked")
    )
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.95))
    assert timing.action == STAND_DOWN
    assert PLAYBOOK_WITHHOLDS in subject.standing.by_refusal


def test_a_playbook_rule_can_only_narrow_never_widen():
    """A learned rule must not be able to talk the bot into the entry its record refuses."""
    subject = a_timer(cap=0.005)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 120.0)
    without_rule = subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price

    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, pullback_fraction=0.5, withholds=False, reason="deep")
    )
    deeper = subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price
    assert deeper < without_rule

    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, pullback_fraction=-0.9, withholds=False, reason="shallow")
    )
    shallower = subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price
    assert shallower <= 120.0


def test_the_extension_cap_is_learned_per_detector():
    subject = a_timer(cap=0.005)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 110.0)
    for _ in range(50):
        subject.observe_entry_quality("momentum-burst-detector", extension_at_entry=0.5, given_away=0.001)
    burst = subject.decide(a_side_candidate(detector="momentum-burst-detector"), ConvictionStub(0.9))
    reverter = subject.decide(a_side_candidate(detector=DETECTOR), ConvictionStub(0.9))
    assert burst.action == ENTER_NOW, "a burst detector fires late by construction"
    assert reverter.action == WAIT_FOR_TRIGGER


# ---- bull-exit-plan-proposer ------------------------------------------------

# What the proposer builds a plan from before any trade has closed. Kept as
# defaults here so the tests below that are about the *measured* path stay about
# it, and the cold-start tests set what they need explicitly.
COLD_START_STOP_MULTIPLE = 1.5
COLD_START_REWARD_MULTIPLES = (1.0, 2.0)
COLD_START_MINIMUM_PRINTS = 20
COLD_START_PRICE_WINDOW = 4000


def a_proposer(minimum_reward=1.0, cold_start_minimum_prints=COLD_START_MINIMUM_PRINTS):
    return BullExitPlanProposer(
        stop_safety_multiple=1.5,
        target_quantiles=((0.5, 0.5), (0.8, 0.5)),
        minimum_reward_to_risk=minimum_reward,
        conviction_horizon_multiple=0.5,
        cold_start_stop_range_multiple=COLD_START_STOP_MULTIPLE,
        cold_start_reward_multiples=COLD_START_REWARD_MULTIPLES,
        cold_start_minimum_prints=cold_start_minimum_prints,
        cold_start_price_window=COLD_START_PRICE_WINDOW,
    )


def a_profile(adverse=0.01, quantiles=None, trades=200, is_fitted=True):
    return ExcursionProfile(
        venue_id=VENUE, symbol=SYMBOL, side=LONG, adverse_excursion=adverse,
        favourable_quantiles=quantiles if quantiles is not None else {0.5: 0.03, 0.8: 0.08},
        trades_observed=trades, is_fitted=is_fitted,
    )


def a_horizon(seconds=600.0, is_fitted=True):
    return HorizonProfile(detector=DETECTOR, median_seconds=seconds, trades_observed=100, is_fitted=is_fitted)


def a_prepared_proposer(minimum_reward=1.0, profile=None, horizon=None):
    subject = a_proposer(minimum_reward)
    subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_excursion_profile(profile or a_profile())
    subject.observe_horizon_profile(horizon or a_horizon())
    return subject


def test_a_plan_is_built_from_what_this_symbol_has_actually_done():
    plan, _ = a_prepared_proposer().propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan.stop_price == pytest.approx(100.0 * (1 - 0.015))
    assert len(plan.targets) == 2
    assert plan.reward_to_risk > 1.0


def feed_a_range(subject, low=99.6, high=100.4, prints=COLD_START_MINIMUM_PRINTS):
    """Print a symbol through a real range, so a stop can be measured from it.

    Alternating so the window holds both ends: what the proposer measures is the
    span the symbol traded through, which is what an ATR stop is measured from.
    """
    for index in range(prints):
        subject.observe_price(VENUE, SYMBOL, low if index % 2 else high)
    subject.observe_price(VENUE, SYMBOL, 100.0)


def test_a_symbol_with_no_excursion_record_is_planned_from_its_own_live_range():
    """Learning improves the trade; it is not a precondition for making one.

    Before this, a symbol with no closed trades produced no plan at all, so the
    bot could notice thirty thousand setups an hour and never take one. The stop
    here is still measured -- from the range this symbol actually traded through
    in the window the detector claimed -- which is what makes it a measurement
    rather than the default percentage RL-062 forbids.
    """
    subject = a_proposer()
    feed_a_range(subject)
    subject.observe_horizon_profile(a_horizon())
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is not None, outcome
    assert plan.stop_price < 100.0
    assert plan.targets
    assert "the-live-price-range" in plan.reason
    assert subject.standing.plans_from_the_live_range == 1
    assert subject.standing.plans_from_the_excursion_record == 0


def test_a_wider_symbol_gets_a_wider_stop_without_anyone_tuning_it():
    """The whole reason the stop is measured per symbol rather than set once."""
    narrow = a_proposer()
    feed_a_range(narrow, low=99.95, high=100.05)
    narrow.observe_horizon_profile(a_horizon())
    narrow_plan, _ = narrow.propose(a_side_candidate(), ConvictionStub(0.8))

    wide = a_proposer()
    feed_a_range(wide, low=98.0, high=102.0)
    wide.observe_horizon_profile(a_horizon())
    wide_plan, _ = wide.propose(a_side_candidate(), ConvictionStub(0.8))

    assert narrow_plan is not None and wide_plan is not None
    assert wide_plan.risk_fraction > narrow_plan.risk_fraction * 5, (
        "a symbol swinging 4% must not get the same stop as one swinging 0.1%"
    )


def test_a_measured_record_replaces_the_live_range_the_moment_it_fits():
    """The cold start sets the first trades, not all of them."""
    subject = a_prepared_proposer(profile=a_profile())
    feed_a_range(subject)
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is not None, outcome
    assert "the-excursion-record" in plan.reason
    assert subject.standing.plans_from_the_excursion_record == 1
    assert subject.standing.plans_from_the_live_range == 0


def test_an_unfitted_excursion_record_falls_back_to_the_live_range():
    subject = a_prepared_proposer(profile=a_profile(is_fitted=False))
    feed_a_range(subject)
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is not None, outcome
    assert "the-live-price-range" in plan.reason


def test_no_horizon_record_uses_the_horizon_the_detector_itself_claimed():
    """The candidate has always carried it; demanding a measured one was a gate
    that never needed to exist."""
    subject = a_proposer()
    feed_a_range(subject)
    subject.observe_excursion_profile(a_profile())
    candidate = a_side_candidate()
    plan, outcome = subject.propose(candidate, ConvictionStub(0.8))
    assert plan is not None, outcome
    assert plan.horizon_seconds >= candidate.horizon_seconds
    assert "the detector itself claimed" in plan.reason


def test_a_window_with_too_few_prints_is_still_refused():
    """The one refusal the cold start keeps.

    A range over three prints is those three prints, and a stop placed from it is
    a stop placed from noise.
    """
    subject = a_proposer()
    feed_a_range(subject, prints=3)
    subject.observe_horizon_profile(a_horizon())
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is None
    assert outcome == NO_RANGE


def test_a_symbol_that_has_not_moved_gets_no_stop():
    """A flat window gives a zero range, and a stop at the entry price is not one."""
    subject = a_proposer()
    for _ in range(COLD_START_MINIMUM_PRINTS + 1):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_horizon_profile(a_horizon())
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is None
    assert outcome == NO_RANGE


def test_the_stop_sits_outside_what_winners_normally_survive():
    plan, _ = a_prepared_proposer().propose(a_side_candidate(), ConvictionStub(0.8))
    survived = 100.0 * (1 - 0.01)
    assert plan.stop_price < survived


def test_the_stop_audit_widens_a_stop_that_kept_being_hit_before_the_trade_worked():
    subject = a_prepared_proposer()
    tight, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    subject.observe_stop_audit(
        StopAudit(venue_id=VENUE, symbol=SYMBOL, stops_hit=40,
                  stops_hit_then_reversed=30, worst_reversal_excursion=0.02)
    )
    widened, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert widened.stop_price < tight.stop_price
    assert subject.standing.stops_widened_by_audit == 1


def test_a_plan_whose_reward_does_not_justify_its_risk_is_refused():
    subject = a_prepared_proposer(minimum_reward=5.0)
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is None
    assert outcome == REWARD_BELOW_RISK


def test_the_targets_close_the_whole_position():
    subject = a_prepared_proposer(profile=a_profile(quantiles={0.5: 0.03}))
    plan, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert sum(target.fraction for target in plan.targets) == pytest.approx(1.0)


def test_a_stop_lands_on_the_venues_tick_grid():
    """A stop off the grid is not a stop the venue will take."""
    subject = a_prepared_proposer()
    subject.observe_symbol_profile(VENUE, SYMBOL, price_step=0.5)
    plan, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert (plan.stop_price / 0.5) == pytest.approx(round(plan.stop_price / 0.5))


def test_a_surer_trade_is_given_longer_to_work():
    subject = a_prepared_proposer()
    unsure, _ = subject.propose(a_side_candidate(), ConvictionStub(0.6))
    sure, _ = subject.propose(a_side_candidate(), ConvictionStub(0.95))
    assert sure.horizon_seconds > unsure.horizon_seconds


def test_a_plan_that_never_takes_profit_is_refused_at_construction():
    with pytest.raises(ValueError):
        BullExitPlanProposer(
            stop_safety_multiple=1.5, target_quantiles=(),
            minimum_reward_to_risk=1.0, conviction_horizon_multiple=0.5,
            cold_start_stop_range_multiple=COLD_START_STOP_MULTIPLE,
            cold_start_reward_multiples=COLD_START_REWARD_MULTIPLES,
            cold_start_minimum_prints=COLD_START_MINIMUM_PRINTS,
            cold_start_price_window=COLD_START_PRICE_WINDOW,
        )


def test_targets_that_do_not_close_the_position_are_refused_at_construction():
    with pytest.raises(ValueError):
        BullExitPlanProposer(
            stop_safety_multiple=1.5, target_quantiles=((0.5, 0.3),),
            minimum_reward_to_risk=1.0, conviction_horizon_multiple=0.5,
            cold_start_stop_range_multiple=COLD_START_STOP_MULTIPLE,
            cold_start_reward_multiples=(1.0,),
            cold_start_minimum_prints=COLD_START_MINIMUM_PRINTS,
            cold_start_price_window=COLD_START_PRICE_WINDOW,
        )


# ---- bull-opinion-composer --------------------------------------------------

def a_composer(minimum=0.55, missing=1, require_measured=False):
    return BullOpinionComposer(
        minimum_conviction=minimum, maximum_missing_features=missing,
        require_trained_model=require_measured,
    )


class CalibratedStub:
    def __init__(self, probability, measured=True, model_is_trained=True,
                 model_observations=1000):
        self.venue_id, self.symbol, self.side = VENUE, SYMBOL, LONG
        self.calibrated = an_estimate(
            probability, 200 if measured else 0, measured,
            "measured" if measured else "the prior",
        )
        self.probability = probability
        # Two different questions, and conflating them stopped the bot trading
        # entirely on 2026-08-23: whether this number has been checked against
        # observed frequencies, and whether the model behind it has ever been
        # trained. The composer asks the second.
        self.is_measured = measured
        self.model_observations = model_observations
        self.model_is_trained = model_is_trained
        self.reason = "conviction"


def a_timing(action=ENTER_NOW):
    from runtime.bot_opinion import EntryTiming
    return EntryTiming(
        bot="bull-bot", venue_id=VENUE, symbol=SYMBOL, side=LONG, action=action,
        trigger_price=100.0, valid_until_ns=None, quality=0.0005,
        reason="the moment", decided_at_ns=Clock()(),
    )


def a_plan():
    plan, _ = a_prepared_proposer().propose(a_side_candidate(), ConvictionStub(0.8))
    return plan


def test_a_complete_answer_becomes_one_opinion():
    opinion = a_composer().compose(a_vector(), CalibratedStub(0.8), a_timing(), a_plan())
    assert opinion.is_a_call_to_act
    assert opinion.action == ENTER_NOW
    assert opinion.exit_plan is not None
    assert opinion.features_summary["features"]


def test_an_entry_with_no_exit_is_an_exposure_not_a_trade():
    subject = a_composer()
    opinion = subject.compose(a_vector(), CalibratedStub(0.9), a_timing(), None)
    assert opinion.action == STAND_DOWN
    assert opinion.refusal == NO_EXIT_PLAN
    assert opinion.is_a_call_to_act is False


def test_a_right_setup_at_the_wrong_moment_stands_down():
    opinion = a_composer().compose(
        a_vector(), CalibratedStub(0.9), a_timing(action=STAND_DOWN), a_plan()
    )
    assert opinion.refusal == TIMING_REFUSED


def test_too_many_missing_features_stops_the_opinion():
    opinion = a_composer(missing=1).compose(
        a_vector(missing=("funding_rate", "book_imbalance")), CalibratedStub(0.9), a_timing(), a_plan()
    )
    assert opinion.refusal == FEATURES_INCOMPLETE
    assert "funding_rate" in opinion.reason


def test_a_conviction_below_the_floor_stands_down():
    opinion = a_composer(minimum=0.7).compose(a_vector(), CalibratedStub(0.6), a_timing(), a_plan())
    assert opinion.refusal == CONVICTION_TOO_LOW


def test_a_bot_can_be_told_to_act_only_on_a_trained_model():
    """The gate tests the model, not the calibration.

    It tested the calibration until 2026-08-23, and calibration needs a scorecard,
    which needs closed trades, which need a trade -- so it could never pass. 653
    opinions were refused by it on the live run before this was found. An
    uncalibrated conviction from a trained model is now allowed through; one from
    a model that has never been trained is not, which is what RL-060 asks for.
    """
    composer = a_composer(require_measured=True)
    untrained = composer.compose(
        a_vector(), CalibratedStub(0.9, measured=False, model_is_trained=False,
                                   model_observations=12),
        a_timing(), a_plan(),
    )
    assert untrained.refusal == CONVICTION_TOO_LOW
    assert "12 outcome(s)" in untrained.reason

    # Trained but never calibrated: allowed, because calibration is what trading
    # produces rather than what it requires.
    trained = composer.compose(
        a_vector(), CalibratedStub(0.9, measured=False, model_is_trained=True,
                                   model_observations=1408),
        a_timing(), a_plan(),
    )
    assert trained.refusal is None, trained.reason


def test_a_stand_down_is_published_rather_than_dropped():
    """A symbol nothing was said about looks like a symbol nobody looked at."""
    subject = a_composer()
    opinion = subject.compose(a_vector(), CalibratedStub(0.1), a_timing(), a_plan())
    assert opinion is not None
    assert opinion.symbol == SYMBOL
    assert opinion.reason
    assert subject.standing.stood_down == 1


def test_the_opinion_carries_enough_to_argue_with_the_trade_later():
    vector = a_vector({"price_z_score": -2.0, "book_imbalance": 0.4})
    opinion = a_composer().compose(vector, CalibratedStub(0.8), a_timing(), a_plan())
    assert opinion.features_summary["features"]["book_imbalance"] == 0.4


# ---- bull-setup-weight-learner ----------------------------------------------

def a_weight_learner(minimum=20, floor=0.1, cap=3.0):
    return BullSetupWeightLearner(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum, minimum_weight=floor, maximum_weight=cap,
    )


def test_a_detector_is_measured_against_the_rest_of_the_bot_not_against_itself():
    """A detector producing most of the trades would otherwise always come out average."""
    subject = a_weight_learner(minimum=10)
    for index in range(500):
        subject.observe_closed_trade("dominant", index % 5 == 0)
    for index in range(20):
        subject.observe_closed_trade("occasional", True)
    assert subject.weight_for("dominant").weight < 1.0
    assert "the rest of this bot" in subject.weight_for("dominant").reason


def test_a_detector_better_than_the_bots_base_rate_is_weighted_up():
    subject = a_weight_learner(minimum=10)
    for index in range(200):
        subject.observe_closed_trade("good", index % 4 != 0)
        subject.observe_closed_trade("poor", index % 4 == 0)
    assert subject.weight_for("good").weight > subject.weight_for("poor").weight


def test_three_wins_out_of_three_is_not_three_times_the_base_rate():
    """The prior pull is what stops a bot piling into a good morning."""
    subject = a_weight_learner(minimum=20)
    for index in range(100):
        subject.observe_closed_trade("established", index % 2 == 0)
    for _ in range(3):
        subject.observe_closed_trade("lucky", True)
    assert subject.weight_for("lucky").weight < 1.6
    assert subject.weight_for("lucky").hit_rate.is_fitted is False


def test_a_losing_detector_is_discounted_never_deleted():
    """A detector that stops being sampled can never be found to work again."""
    subject = a_weight_learner(minimum=10, floor=0.1)
    for index in range(300):
        subject.observe_closed_trade("hopeless", False)
        subject.observe_closed_trade("ordinary", index % 2 == 0)
    weight = subject.weight_for("hopeless")
    assert weight.weight == 0.1
    assert "keeps being sampled" in weight.reason


def test_the_weight_is_capped_so_one_hot_detector_cannot_own_the_bot():
    subject = a_weight_learner(minimum=10, cap=2.0)
    for index in range(300):
        subject.observe_closed_trade("hot", True)
        subject.observe_closed_trade("cold", False)
    assert subject.weight_for("hot").weight == 2.0


def test_a_restarted_learner_adopts_the_scorecard():
    scorecard = BotScorecard(bot="bull-bot")
    for index in range(100):
        scorecard.record_closed_trade(DETECTOR, "reverting", 0.7, index % 3 != 0, 1.0)
    subject = a_weight_learner(minimum=20)
    subject.observe_scorecard(scorecard)
    assert subject.weight_for(DETECTOR).trades_judged == 100


def test_an_instruction_carries_its_own_record_into_the_detector_it_compiled_to():
    subject = a_weight_learner(minimum=10)
    for index in range(100):
        subject.observe_closed_trade("baseline", index % 2 == 0)
    subject.observe_instruction_scorecard("i-1", "compiled-detector", wins=40, trades=50)
    weight = subject.weight_for("compiled-detector")
    assert weight.trades_judged == 50
    assert weight.weight > 1.0
    assert "instruction i-1" in weight.reason


def test_an_instruction_cannot_have_won_more_than_it_took():
    with pytest.raises(ValueError):
        a_weight_learner().observe_instruction_scorecard("i-1", "d", wins=10, trades=5)


def test_a_zero_floor_is_refused_at_construction():
    learner = BullSetupWeightLearner(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=20, minimum_weight=0.0, maximum_weight=3.0,
    )
    assert learner is not None, "zero is allowed but must be a deliberate choice"
    with pytest.raises(ValueError):
        BullSetupWeightLearner(
            prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
            minimum_observations=20, minimum_weight=3.0, maximum_weight=1.0,
        )


# ---- bull-position-invalidation-watcher -------------------------------------

class PositionStub:
    def __init__(self):
        self.venue_id, self.symbol = VENUE, SYMBOL


def a_watcher(clock=None, reduce_at=0.4, close_at=0.7):
    return BullPositionInvalidationWatcher(
        reversal_fraction_to_reduce=reduce_at, reversal_fraction_to_close=close_at,
        prior_invalidation_hit_rate=0.5, prior_weight=4.0,
        half_life_observations=200, minimum_observations=20, now_ns=clock or Clock(),
    )


def a_thesis(clock, features=None, regime="reverting", horizon=600.0):
    return HeldThesis(
        venue_id=VENUE, symbol=SYMBOL,
        entry_features=features or {"price_z_score": -2.0, "book_imbalance": 0.4},
        entry_regime=regime, entry_price=100.0, horizon_seconds=horizon,
        opened_at_ns=clock(), detector=DETECTOR,
    )


def test_a_thesis_that_still_holds_is_held():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock))
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": -1.5, "book_imbalance": 0.3}))
    assert opinion.action == STAND_DOWN
    assert STILL_VALID in subject.standing.by_reason


def test_features_reversing_past_the_close_threshold_closes():
    clock = Clock()
    subject = a_watcher(clock, close_at=0.7)
    subject.record_entry(a_thesis(clock))
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": 2.0, "book_imbalance": -0.4}))
    assert opinion.action == CLOSE_POSITION
    assert FEATURES_REVERSED in subject.standing.by_reason


def test_a_thesis_that_is_less_true_is_reduced_not_dumped():
    """Between hold and close sits "less true than it was"."""
    clock = Clock()
    subject = a_watcher(clock, reduce_at=0.4, close_at=0.9)
    subject.record_entry(a_thesis(clock))
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": 2.0, "book_imbalance": 0.4}))
    assert opinion.action == REDUCE_POSITION


def test_a_broken_regime_closes_whatever_the_features_say():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock, regime="reverting"))
    subject.observe_regime_break("reverting", True)
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": -2.0, "book_imbalance": 0.4}))
    assert opinion.action == CLOSE_POSITION
    assert REGIME_BROKEN in subject.standing.by_reason


def test_a_trade_past_its_horizon_is_not_a_trade_any_more():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock, horizon=600.0))
    clock.advance_seconds(601)
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": -2.0, "book_imbalance": 0.4}))
    assert opinion.action == CLOSE_POSITION
    assert HORIZON_EXPIRED in subject.standing.by_reason


def test_a_position_with_no_recorded_thesis_is_not_closed_on_a_guess():
    subject = a_watcher()
    opinion = subject.check(PositionStub(), a_vector())
    assert opinion.action == STAND_DOWN
    assert opinion.refusal == NO_ENTRY_RECORD


def test_a_weakened_feature_is_not_a_reversed_one():
    """+2 to +0.5 has weakened; +2 to -0.5 has reversed. Only the second is a reason."""
    clock = Clock()
    subject = a_watcher(clock, reduce_at=0.4)
    subject.record_entry(a_thesis(clock, features={"a": 2.0, "b": 2.0}))
    opinion = subject.check(PositionStub(), a_vector({"a": 0.1, "b": 0.1}))
    assert opinion.action == STAND_DOWN


def test_a_closed_position_releases_its_thesis():
    """T-3: nothing accumulates for ever."""
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock))
    assert subject.standing.positions_watched == 1
    subject.forget_position(VENUE, SYMBOL)
    assert subject.standing.positions_watched == 0


def test_the_watcher_is_judged_too():
    """A watcher that panics is indistinguishable from one that saves money."""
    subject = a_watcher()
    for _ in range(50):
        subject.observe_outcome(False)
    record = subject.invalidation_record
    assert record.is_fitted
    assert record.value < 0.4


def test_the_opinion_shows_entry_against_now():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock, features={"price_z_score": -2.0}))
    opinion = subject.check(PositionStub(), a_vector({"price_z_score": 2.0}))
    assert opinion.features_summary["entry"]["price_z_score"] == -2.0
    assert opinion.features_summary["now"]["price_z_score"] == 2.0


def test_a_watcher_whose_close_threshold_sits_below_its_reduce_one_is_refused():
    with pytest.raises(ValueError):
        BullPositionInvalidationWatcher(
            reversal_fraction_to_reduce=0.8, reversal_fraction_to_close=0.4,
            prior_invalidation_hit_rate=0.5, prior_weight=4.0,
            half_life_observations=200, minimum_observations=20,
        )
