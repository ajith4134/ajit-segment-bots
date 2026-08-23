"""The bear bot: the asymmetries, and whether each is actually implemented.

Half of these tests exist to check that this bot is not the bull bot with signs
flipped. Every place the short side is genuinely different -- carry, the
unbounded loss, the squeeze, down being faster than up -- has a test that fails
if that difference is quietly dropped, because a mirrored short book is the
common way a two-sided system loses money on one side only.

Prices come from the tape where the test is about a real series (RL-063).
"""

import importlib
import json

import pytest

from parts.bear_bot.bear_conviction_calibrator import ALL_REGIMES, BearConvictionCalibrator
from parts.bear_bot.bear_conviction_model import (
    CHALLENGER, CHAMPION, FEATURES_FLAGGED, FORECAST_FLAGGED, NOTHING_USABLE,
    BearConvictionModel, ShortOutcome,
)
from parts.bear_bot.bear_entry_timer import (
    CONVICTION_TOO_LOW as TIMER_CONVICTION_TOO_LOW, NO_PRICE, PLAYBOOK_WITHHOLDS,
    BearEntryTimer, PlaybookRule,
)
from parts.bear_bot.bear_exit_plan_proposer import (
    MAXIMUM_SHORT_FAVOURABLE_EXCURSION, NO_EXCURSION_PROFILE, NO_HORIZON, NO_RANGE,
    REWARD_BELOW_RISK, STOP_WOULD_BE_UNBOUNDED, BearExitPlanProposer, ExcursionProfile,
    HorizonProfile, StopAudit,
)
from parts.bear_bot.bear_feature_builder import FEATURE_NAMES, BearFeatureBuilder
from parts.bear_bot.bear_opinion_composer import RISK_UNBOUNDED, BearOpinionComposer
from parts.bear_bot.bear_outlier_rejector import DANGEROUS_WHEN_LOW, BearOutlierRejector
from parts.bear_bot.bear_position_invalidation_watcher import (
    CARRY_ATE_THE_THESIS, FEATURES_REVERSED, HORIZON_EXPIRED, NO_ENTRY_RECORD,
    REGIME_BROKEN, SQUEEZE_FORMING, STILL_VALID, BearPositionInvalidationWatcher, HeldThesis,
)
from parts.bear_bot.bear_setup_filter import (
    CARRY_EATS_THE_EDGE, SETUP_DISCOUNTED, WEIGHTED_STRENGTH_TOO_LOW, WRONG_SIDE,
    BearSetupFilter,
)
from parts.bear_bot.bear_setup_weight_learner import BearSetupWeightLearner
from runtime.online_learner import ModelBelief
from runtime.bot_opinion import (
    CLOSE_POSITION, CONVICTION_TOO_LOW, ENTER_NOW, FEATURES_INCOMPLETE, LONG,
    NO_EXIT_PLAN, REDUCE_POSITION, SHORT, STAND_DOWN, TIMING_REFUSED, WAIT_FOR_TRIGGER,
    BotScorecard, FeatureVector, SideCandidate,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.market_signal import CONTINUATION, make_candidate
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "bear-setup-filter": "parts.bear_bot.bear_setup_filter",
    "bear-feature-builder": "parts.bear_bot.bear_feature_builder",
    "bear-outlier-rejector": "parts.bear_bot.bear_outlier_rejector",
    "bear-conviction-model": "parts.bear_bot.bear_conviction_model",
    "bear-conviction-calibrator": "parts.bear_bot.bear_conviction_calibrator",
    "bear-entry-timer": "parts.bear_bot.bear_entry_timer",
    "bear-exit-plan-proposer": "parts.bear_bot.bear_exit_plan_proposer",
    "bear-opinion-composer": "parts.bear_bot.bear_opinion_composer",
    "bear-setup-weight-learner": "parts.bear_bot.bear_setup_weight_learner",
    "bear-position-invalidation-watcher": "parts.bear_bot.bear_position_invalidation_watcher",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
DETECTOR = "momentum-burst-detector"
SETTLEMENTS_PER_DAY = 3.0


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


def fitted(value, observations=100):
    return an_estimate(value, observations, True)


def unfitted(value):
    return an_estimate(value, 0, False, "the prior")


def a_candidate(direction=SHORT, strength=3.0, detector=DETECTOR, horizon=3600.0, confidence=None):
    return make_candidate(
        detector=detector, venue_id=VENUE, symbol=SYMBOL, direction=direction,
        expectation=CONTINUATION, signal_strength=strength,
        confidence=confidence or fitted(0.6), horizon_seconds=horizon,
        evidence={"burst": True}, reason="breaking down",
    )


def a_side_candidate(detector=DETECTOR, weight=1.0, strength=3.0, horizon=3600.0, confidence=None):
    return SideCandidate(
        bot="bear-bot", side=SHORT, venue_id=VENUE, symbol=SYMBOL, detector=detector,
        expectation=CONTINUATION, signal_strength=strength,
        detector_confidence=confidence or fitted(0.6), setup_weight=weight,
        horizon_seconds=horizon, evidence={}, reason="accepted", accepted_at_ns=Clock()(),
    )


def a_vector(features=None, missing=()):
    return FeatureVector(
        bot="bear-bot", venue_id=VENUE, symbol=SYMBOL,
        features=features if features is not None else {"price_z_score": 2.0},
        missing=tuple(missing), sources={}, built_at_ns=Clock()(),
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
    """R-03: bull, bear and tailgater are peers with no wire between them."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "parts.bull_bot" not in text
    assert "parts.profit_tailgating_bot" not in text


# ---- bear-setup-filter ------------------------------------------------------

def a_filter(default=1.0, floor=0.2, strength_floor=1.0, maximum_carry=0.01):
    return BearSetupFilter(
        default_setup_weight=default, minimum_setup_weight=floor,
        minimum_weighted_strength=strength_floor, settlements_per_day=SETTLEMENTS_PER_DAY,
        maximum_carry_fraction_of_horizon=maximum_carry,
    )


def test_a_long_candidate_is_not_a_weak_short():
    accepted, outcome = a_filter().filter_candidate(a_candidate(direction=LONG))
    assert accepted is None
    assert outcome == WRONG_SIDE


def test_a_short_into_negative_funding_bleeds_and_is_refused():
    """The carry is paid every settlement whatever the price does."""
    subject = a_filter(maximum_carry=0.005)
    subject.observe_funding_rate(VENUE, SYMBOL, -0.01)
    accepted, outcome = subject.filter_candidate(a_candidate(horizon=86400.0))
    assert accepted is None
    assert outcome == CARRY_EATS_THE_EDGE


def test_a_short_into_positive_funding_is_paid_to_wait():
    subject = a_filter()
    subject.observe_funding_rate(VENUE, SYMBOL, 0.01)
    accepted, _ = subject.filter_candidate(a_candidate(horizon=86400.0))
    assert accepted is not None
    assert accepted.evidence["projected_carry_fraction"] < 0
    assert "pays the short" in accepted.reason


def test_the_carry_is_projected_over_the_setups_own_horizon():
    subject = a_filter(maximum_carry=0.02)
    subject.observe_funding_rate(VENUE, SYMBOL, -0.005)
    brief, _ = subject.filter_candidate(a_candidate(horizon=600.0))
    long_held, outcome = subject.filter_candidate(a_candidate(horizon=7 * 86400.0))
    assert brief is not None, "ten minutes of carry is negligible"
    assert long_held is None and outcome == CARRY_EATS_THE_EDGE


def test_a_symbol_with_no_funding_rate_says_the_carry_is_unknown():
    accepted, _ = a_filter().filter_candidate(a_candidate())
    assert accepted.evidence["projected_carry_fraction"] is None
    assert "carry is unknown" in accepted.reason


def test_a_discounted_detector_and_a_weak_signal_are_both_refused():
    subject = a_filter(floor=0.5, strength_floor=2.0)
    subject.observe_setup_weight(DETECTOR, 0.1)
    assert subject.filter_candidate(a_candidate())[1] == SETUP_DISCOUNTED
    subject.observe_setup_weight(DETECTOR, 1.0)
    assert subject.filter_candidate(a_candidate(strength=0.5))[1] == WEIGHTED_STRENGTH_TOO_LOW


def test_a_filter_with_no_settlement_schedule_cannot_project_carry():
    with pytest.raises(ValueError):
        BearSetupFilter(
            default_setup_weight=1.0, minimum_setup_weight=0.2, minimum_weighted_strength=1.0,
            settlements_per_day=0.0, maximum_carry_fraction_of_horizon=0.01,
        )


# ---- bear-feature-builder ---------------------------------------------------

def a_builder(minimum=5):
    return BearFeatureBuilder(
        short_window=10, long_window=50, minimum_observations=minimum,
        settlements_per_day=SETTLEMENTS_PER_DAY,
    )


def a_prepared_builder(prices, minimum=5):
    subject = a_builder(minimum)
    for price in prices:
        subject.observe_price(VENUE, SYMBOL, price)
    subject.observe_book(VENUE, SYMBOL, bids=((77400.0, 3.0),), asks=((77420.0, 5.0),))
    subject.observe_funding(VENUE, SYMBOL, 0.0001)
    subject.observe_funding_forecast(VENUE, SYMBOL, 0.0004)
    return subject


def test_a_complete_short_vector_names_where_everything_came_from(real_trade_prices):
    vector = a_prepared_builder(real_trade_prices[:60]).build(a_side_candidate())
    assert vector.is_complete, f"still missing {vector.missing}"
    assert set(vector.features) == set(FEATURE_NAMES)
    assert set(vector.sources) == set(vector.features)


def test_a_feature_that_could_not_be_measured_is_named_not_defaulted():
    vector = a_builder().build(a_side_candidate())
    assert "funding_rate" in vector.missing
    assert "funding_rate" not in vector.features


def test_offer_side_imbalance_is_signed_for_the_short_not_reused_from_the_bull():
    """A feature whose sign means the opposite to another bot teaches both wrong."""
    subject = a_builder()
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 1.0),), asks=((101.0, 10.0),))
    offer_heavy = subject.build(a_side_candidate()).features["offer_side_imbalance"]
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 10.0),), asks=((101.0, 1.0),))
    bid_heavy = subject.build(a_side_candidate()).features["offer_side_imbalance"]
    assert offer_heavy > 0 > bid_heavy


def test_downside_volatility_separates_a_fall_from_a_rally():
    """One volatility number treats a 3% drop and a 3% rally as the same event."""
    falling = a_builder(minimum=5)
    rising = a_builder(minimum=5)
    price_down, price_up = 100.0, 100.0
    for index in range(60):
        price_down *= 0.99 if index % 3 else 1.001
        price_up *= 1.01 if index % 3 else 0.999
        falling.observe_price(VENUE, SYMBOL, price_down)
        rising.observe_price(VENUE, SYMBOL, price_up)
    down = falling.build(a_side_candidate()).features["downside_volatility_ratio"]
    up = rising.build(a_side_candidate()).features["downside_volatility_ratio"]
    assert down > up


def test_squeeze_room_is_thin_when_a_moving_symbol_has_a_light_offer_side():
    subject = a_builder(minimum=5)
    price = 100.0
    for index in range(60):
        price *= 1.02 if index % 2 else 0.98
        subject.observe_price(VENUE, SYMBOL, price)
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 100.0),), asks=((101.0, 0.01),))
    thin = subject.build(a_side_candidate()).features["squeeze_room"]
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 100.0),), asks=((101.0, 10_000.0),))
    deep = subject.build(a_side_candidate()).features["squeeze_room"]
    assert thin < deep


def test_the_carry_feature_is_signed_as_a_cost_to_the_short():
    subject = a_builder(minimum=5)
    for _ in range(60):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_funding(VENUE, SYMBOL, -0.01)
    charged = subject.build(a_side_candidate(horizon=86400.0)).features["funding_carry_over_horizon"]
    subject.observe_funding(VENUE, SYMBOL, 0.01)
    paid = subject.build(a_side_candidate(horizon=86400.0)).features["funding_carry_over_horizon"]
    assert charged > 0 > paid


# ---- bear-outlier-rejector --------------------------------------------------

def a_rejector(threshold=4.0, dangerous=2.5, minimum=20, unjudgeable=0.5,
               room_deviation=1.5, volatility_deviation=1.5):
    return BearOutlierRejector(
        deviation_threshold=threshold, dangerous_side_threshold=dangerous,
        minimum_observations=minimum, half_life_observations=500,
        maximum_unjudgeable_fraction=unjudgeable,
        squeeze_room_deviation=room_deviation,
        rising_volatility_deviation=volatility_deviation,
    )


def teach_normal(rejector, count=200):
    for index in range(count):
        rejector.observe_vector(a_vector({
            "price_z_score": 1.0 if index % 2 else -1.0,
            "squeeze_room": 100.0 if index % 2 else 80.0,
            "realised_volatility_fraction": 0.01 if index % 2 else 0.012,
        }))


def normal_vector(**overrides):
    features = {"price_z_score": 0.5, "squeeze_room": 90.0, "realised_volatility_fraction": 0.011}
    features.update(overrides)
    return a_vector(features)


def test_an_ordinary_short_vector_is_not_flagged():
    subject = a_rejector()
    teach_normal(subject)
    assert subject.judge(normal_vector()).is_out_of_distribution is False


def test_the_dangerous_side_of_a_feature_trips_at_a_nearer_threshold():
    """Distance is symmetric; a short's exposure to it is not.

    The same distance from normal is refused below and accepted above, because
    a thin offer side is what ends a short and a deep one is not.
    """
    subject = a_rejector(threshold=4.0, dangerous=2.0, room_deviation=99.0)
    teach_normal(subject)
    low, high = subject.normal_range("squeeze_room")
    mean = 90.0
    three_deviations = (mean - low) / 2 * 3

    assert "squeeze_room" in DANGEROUS_WHEN_LOW
    thin = subject.judge(normal_vector(squeeze_room=mean - three_deviations))
    deep = subject.judge(normal_vector(squeeze_room=mean + three_deviations))
    assert thin.is_out_of_distribution is True
    assert deep.is_out_of_distribution is False
    assert "hurts a short" in thin.reason


def test_a_squeeze_shaped_vector_is_refused_even_when_each_feature_is_ordinary():
    subject = a_rejector(threshold=6.0, dangerous=6.0, room_deviation=1.0, volatility_deviation=1.0)
    teach_normal(subject)
    flag = subject.judge(normal_vector(squeeze_room=60.0, realised_volatility_fraction=0.014))
    assert flag.is_out_of_distribution is True
    assert "squeeze" in flag.reason
    assert subject.standing.squeeze_shaped == 1


def test_a_symbol_nothing_is_known_about_is_refused():
    flag = a_rejector().judge(a_vector({"brand_new": 1.0}))
    assert flag.is_out_of_distribution is True
    assert flag.features_judged == 0


def test_a_rejector_whose_dangerous_side_is_looser_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearOutlierRejector(
            deviation_threshold=2.0, dangerous_side_threshold=4.0, minimum_observations=20,
            half_life_observations=500, maximum_unjudgeable_fraction=0.5,
            squeeze_room_deviation=1.5, rising_volatility_deviation=1.5,
        )


# ---- bear-conviction-model --------------------------------------------------

def a_model(minimum_training=20):
    return BearConvictionModel(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=5, minimum_training_observations=minimum_training,
        default_sample_weight=1.0, maximum_sample_weight=5.0,
    )


def teach_the_model(model, rounds=300, horizon=600.0):
    for index in range(rounds):
        signal = 1.0 if index % 2 else -1.0
        model.train_from_label(
            {"price_z_score": signal}, moved_the_expected_way=signal > 0,
            seconds_to_resolve=60.0, horizon_seconds=horizon, source=DETECTOR,
        )


def test_a_short_that_worked_only_after_its_horizon_is_not_a_win():
    """Counting it as one teaches the model to hold shorts through squeezes."""
    late = ShortOutcome(
        features={"price_z_score": 1.0}, moved_the_expected_way=True,
        seconds_to_resolve=5000.0, horizon_seconds=600.0, sample_weight=1.0, source=DETECTOR,
    )
    assert late.label is False
    assert late.was_late is True

    prompt = ShortOutcome(
        features={"price_z_score": 1.0}, moved_the_expected_way=True,
        seconds_to_resolve=100.0, horizon_seconds=600.0, sample_weight=1.0, source=DETECTOR,
    )
    assert prompt.label is True


def test_late_shorts_are_counted_so_the_bot_can_see_it_is_holding_too_long():
    subject = a_model()
    for _ in range(20):
        subject.train_from_label(
            {"price_z_score": 1.0}, moved_the_expected_way=True,
            seconds_to_resolve=9000.0, horizon_seconds=600.0, source=DETECTOR,
        )
    assert subject.standing.resolved_after_horizon == 20


def test_a_flagged_vector_or_forecast_gets_no_conviction():
    subject = a_model()
    teach_the_model(subject)
    assert subject.form_conviction(a_vector(), True)[1] == FEATURES_FLAGGED
    subject.observe_forecast_flag(VENUE, SYMBOL, True)
    assert subject.form_conviction(a_vector(), False)[1] == FORECAST_FLAGGED


def test_a_vector_the_model_cannot_standardise_gets_nothing():
    assert a_model().form_conviction(a_vector({"unseen": 1.0}), False)[1] == NOTHING_USABLE


def test_a_short_conviction_explains_itself():
    subject = a_model()
    teach_the_model(subject)
    conviction, _ = subject.form_conviction(a_vector({"price_z_score": 1.0}), False)
    assert conviction.side == SHORT
    assert "inside the horizon" in conviction.reason


def test_the_kline_shape_feature_is_read_from_the_shorts_end_of_the_range():
    subject = a_model()
    subject.observe_kline_window(VENUE, SYMBOL, closes=[100.0, 105.0], highs=[110.0], lows=[100.0])
    features = subject._kline_features[(VENUE, SYMBOL)]
    assert "kline_distance_below_high" in features
    assert features["kline_distance_below_high"] == pytest.approx(0.5)


def test_promotion_is_delivered_and_the_live_model_cannot_be_wiped():
    subject = a_model()
    teach_the_model(subject)
    subject.apply_champion_choice(CHALLENGER)
    assert subject.live_model_name == CHALLENGER
    with pytest.raises(ValueError):
        subject.apply_retrain_request(CHALLENGER)
    subject.apply_retrain_request(CHAMPION)
    assert subject.model(CHAMPION).observations == 0


# ---- bear-conviction-calibrator ---------------------------------------------

class RawStub:
    def __init__(self, probability, trained_on=1000):
        self.venue_id, self.symbol, self.side = VENUE, SYMBOL, SHORT
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


def a_bear_calibrator(minimum=50):
    return BearConvictionCalibrator(
        bin_count=10, minimum_observations=minimum, half_life_observations=5000
    )


def test_an_unfitted_short_calibrator_passes_the_model_through():
    result = a_bear_calibrator().calibrate(RawStub(0.85))
    assert result.probability == 0.85
    assert result.is_measured is False


def test_overconfidence_is_measured_and_reported():
    subject = a_bear_calibrator(minimum=50)
    for index in range(300):
        subject.observe_outcome(0.85, was_right=index % 4 == 0)
    result = subject.calibrate(RawStub(0.85))
    assert result.probability < 0.5
    assert subject.standing.largest_overconfidence > 0.3


def test_a_regime_with_a_thin_short_record_falls_back_and_says_so():
    """The regimes where a short model looks best are the ones its record is thinnest in."""
    subject = a_bear_calibrator(minimum=30)
    for index in range(200):
        subject.observe_outcome(0.8, was_right=index % 2 == 0, regime="falling")
    result = subject.calibrate(RawStub(0.8), regime="chop-before-a-squeeze")
    assert subject.standing.fell_back_to_overall == 1
    assert "too thin a record of its own" in result.reason


def test_the_record_decays_so_a_past_regime_stops_governing():
    subject = BearConvictionCalibrator(bin_count=10, minimum_observations=30, half_life_observations=50)
    for _ in range(300):
        subject.observe_outcome(0.8, was_right=True)
    for _ in range(300):
        subject.observe_outcome(0.8, was_right=False)
    assert subject.calibrate(RawStub(0.8)).probability < 0.2


# ---- bear-entry-timer -------------------------------------------------------

def a_floor(margin=0.0):
    # Fee-free break-even at 1.5 reward to risk is 40%; the plan's own floor
    # applies in the composer. Same arithmetic as the bull bot's tests.
    return ConvictionFloor(fee_rate=0.00055, margin=margin, fallback_reward_to_risk=1.5)


def a_timer(margin=0.0, clock=None, floor=0.01):
    return BearEntryTimer(
        conviction_floor=a_floor(margin), window_length=50, minimum_observations=10,
        trigger_validity_seconds=30.0, minimum_extension_quantile=0.2,
        entry_quality_window=200, prior_extension_floor=floor,
        prior_entry_cost_fraction=0.0005, now_ns=clock or Clock(),
    )


class ConvictionStub:
    def __init__(self, probability, measured=True):
        self.probability = probability
        self.is_measured = measured


def test_a_short_the_bot_does_not_believe_is_not_timed():
    subject = a_timer(margin=0.2)
    assert subject.decide(a_side_candidate(), ConvictionStub(0.5)).action == STAND_DOWN
    assert TIMER_CONVICTION_TOO_LOW in subject.standing.by_refusal


def test_a_symbol_with_no_price_has_no_moment_to_judge():
    subject = a_timer()
    assert subject.decide(a_side_candidate(), ConvictionStub(0.9)).action == STAND_DOWN
    assert NO_PRICE in subject.standing.by_refusal


def test_a_short_waits_for_a_bounce_above_here_not_a_pullback_below():
    """The bull waits below; a short that waits below watches the move finish."""
    subject = a_timer(floor=0.02)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 90.0)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert timing.action == WAIT_FOR_TRIGGER
    assert timing.trigger_price > 90.0


def test_a_price_still_above_its_mean_is_shorted_now():
    subject = a_timer(floor=0.0)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 105.0)
    assert subject.decide(a_side_candidate(), ConvictionStub(0.9)).action == ENTER_NOW


def test_a_waiting_short_expires_and_the_reason_names_the_carry():
    clock = Clock()
    subject = a_timer(clock=clock, floor=0.02)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 90.0)
    timing = subject.decide(a_side_candidate(), ConvictionStub(0.9))
    assert "carry" in timing.reason
    assert subject.has_expired(timing) is False
    clock.advance_seconds(31)
    assert subject.has_expired(timing) is True


def test_a_playbook_rule_can_withhold_a_short():
    subject = a_timer()
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, bounce_fraction=None, withholds=True, reason="never worked")
    )
    assert subject.decide(a_side_candidate(), ConvictionStub(0.95)).action == STAND_DOWN
    assert PLAYBOOK_WITHHOLDS in subject.standing.by_refusal


def test_a_playbook_rule_can_demand_a_higher_bounce_never_a_lower_one():
    subject = a_timer(floor=0.02)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_price(VENUE, SYMBOL, 90.0)
    plain = subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price

    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, bounce_fraction=0.5, withholds=False, reason="wait higher")
    )
    higher = subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price
    assert higher > plain

    subject.observe_playbook_rule(
        PlaybookRule(detector=DETECTOR, bounce_fraction=-0.9, withholds=False, reason="lower")
    )
    assert subject.decide(a_side_candidate(), ConvictionStub(0.9)).trigger_price >= 90.0


# ---- bear-exit-plan-proposer ------------------------------------------------

# The cold-start path, as in the bull bot's tests: a stop from the symbol's own
# range over the claimed horizon, targets at multiples of it.
COLD_START_STOP_MULTIPLE = 1.5
COLD_START_REWARD_MULTIPLES = (1.0, 2.0)
COLD_START_MINIMUM_PRINTS = 20
COLD_START_PRICE_WINDOW = 4000


def a_proposer(minimum_reward=1.0, maximum_stop=0.15, clock=None):
    return BearExitPlanProposer(
        stop_safety_multiple=1.5, maximum_stop_fraction=maximum_stop,
        target_quantiles=((0.5, 0.5), (0.8, 0.5)), minimum_reward_to_risk=minimum_reward,
        cold_start_stop_range_multiple=COLD_START_STOP_MULTIPLE,
        cold_start_reward_multiples=COLD_START_REWARD_MULTIPLES,
        cold_start_minimum_prints=COLD_START_MINIMUM_PRINTS,
        cold_start_price_window=COLD_START_PRICE_WINDOW,
        **({"now_ns": clock} if clock is not None else {}),
    )


def a_profile(adverse=0.02, quantiles=None, trades=200, is_fitted=True):
    return ExcursionProfile(
        venue_id=VENUE, symbol=SYMBOL, side=SHORT, adverse_excursion=adverse,
        favourable_quantiles=quantiles if quantiles is not None else {0.5: 0.04, 0.8: 0.10},
        trades_observed=trades, is_fitted=is_fitted,
    )


def a_horizon(seconds=600.0, is_fitted=True):
    return HorizonProfile(detector=DETECTOR, median_seconds=seconds, trades_observed=100, is_fitted=is_fitted)


def a_prepared_proposer(minimum_reward=1.0, maximum_stop=0.15, profile=None, horizon=None):
    subject = a_proposer(minimum_reward, maximum_stop)
    subject.observe_price(VENUE, SYMBOL, 100.0)
    subject.observe_excursion_profile(profile or a_profile())
    subject.observe_horizon_profile(horizon or a_horizon())
    return subject


def test_a_short_plan_puts_its_stop_above_and_its_targets_below():
    plan, _ = a_prepared_proposer().propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan.stop_price > 100.0
    assert all(target.price < 100.0 for target in plan.targets)
    assert plan.side == SHORT


def test_a_symbol_needing_a_stop_wider_than_a_short_can_carry_gets_no_plan():
    """Clamping would produce a bounded plan that is wrong, which is worse than none."""
    subject = a_prepared_proposer(maximum_stop=0.05, profile=a_profile(adverse=0.20))
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is None
    assert outcome == STOP_WOULD_BE_UNBOUNDED
    assert subject.standing.refused_unbounded == 1


def test_a_target_cannot_be_priced_below_zero():
    """Price cannot fall below zero, and an order there is rejected, not ambitious."""
    subject = a_prepared_proposer(profile=a_profile(quantiles={0.5: 0.5, 0.8: 3.0}))
    plan, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert all(target.price > 0 for target in plan.targets)
    assert subject.standing.targets_clamped_at_zero == 1
    assert MAXIMUM_SHORT_FAVOURABLE_EXCURSION == 1.0


def test_no_excursion_record_falls_back_to_the_range_and_no_range_is_refused_by_name():
    """Rewritten on 2026-08-23 with the cold-start path: no record is no longer
    no plan, it is a plan from the symbol's own range -- and with one print
    there is no range, which is its own refusal."""
    without_excursion = a_proposer()
    without_excursion.observe_price(VENUE, SYMBOL, 100.0)
    without_excursion.observe_horizon_profile(a_horizon())
    assert without_excursion.propose(a_side_candidate(), ConvictionStub(0.8))[1] == NO_RANGE


def test_no_horizon_record_uses_the_horizon_the_detector_claimed():
    without_horizon = a_proposer()
    without_horizon.observe_price(VENUE, SYMBOL, 100.0)
    without_horizon.observe_excursion_profile(a_profile())
    plan, outcome = without_horizon.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is not None, outcome
    assert "the horizon the detector itself claimed" in plan.reason


def test_the_horizon_is_not_extended_for_conviction_because_carry_accrues():
    subject = a_prepared_proposer()
    unsure, _ = subject.propose(a_side_candidate(), ConvictionStub(0.6))
    sure, _ = subject.propose(a_side_candidate(), ConvictionStub(0.95))
    assert sure.horizon_seconds == unsure.horizon_seconds == 600.0


def test_a_plan_whose_reward_does_not_justify_its_risk_is_refused():
    subject = a_prepared_proposer(minimum_reward=10.0)
    assert subject.propose(a_side_candidate(), ConvictionStub(0.8))[1] == REWARD_BELOW_RISK


def test_the_stop_audit_widens_a_short_stop_that_kept_being_hit():
    subject = a_prepared_proposer()
    tight, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    subject.observe_stop_audit(
        StopAudit(venue_id=VENUE, symbol=SYMBOL, stops_hit=40,
                  stops_hit_then_reversed=30, worst_reversal_excursion=0.04)
    )
    widened, _ = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert widened.stop_price > tight.stop_price


def test_a_proposer_with_no_risk_ceiling_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearExitPlanProposer(
            stop_safety_multiple=1.5, maximum_stop_fraction=1.5,
            target_quantiles=((0.5, 1.0),), minimum_reward_to_risk=1.0,
            cold_start_stop_range_multiple=COLD_START_STOP_MULTIPLE,
            cold_start_reward_multiples=(1.0,),
            cold_start_minimum_prints=COLD_START_MINIMUM_PRINTS,
            cold_start_price_window=COLD_START_PRICE_WINDOW,
        )


# ---- bear-opinion-composer --------------------------------------------------

def a_composer(margin=0.0, missing=1, maximum_risk=0.1, require_measured=False):
    return BearOpinionComposer(
        conviction_floor=a_floor(margin), maximum_missing_features=missing,
        maximum_risk_fraction=maximum_risk, require_trained_model=require_measured,
    )


class CalibratedStub:
    def __init__(self, probability, measured=True):
        self.venue_id, self.symbol, self.side = VENUE, SYMBOL, SHORT
        self.calibrated = an_estimate(probability, 200 if measured else 0, measured)
        self.probability = probability
        self.is_measured = measured
        self.reason = "conviction"


def a_timing(action=ENTER_NOW):
    from runtime.bot_opinion import EntryTiming
    return EntryTiming(
        bot="bear-bot", venue_id=VENUE, symbol=SYMBOL, side=SHORT, action=action,
        trigger_price=100.0, valid_until_ns=None, quality=0.0005,
        reason="the moment", decided_at_ns=Clock()(),
    )


def a_plan():
    plan, _ = a_prepared_proposer().propose(a_side_candidate(), ConvictionStub(0.8))
    return plan


def test_a_complete_short_answer_becomes_one_opinion():
    opinion = a_composer().compose(a_vector(), CalibratedStub(0.8), a_timing(), a_plan())
    assert opinion.is_a_call_to_act
    assert opinion.side == SHORT


def test_a_plan_whose_stop_is_too_far_is_refused_here_as_well_as_upstream():
    """The requirement lives where the opinion is formed, so it cannot be dropped."""
    subject = a_composer(maximum_risk=0.01)
    opinion = subject.compose(a_vector(), CalibratedStub(0.9), a_timing(), a_plan())
    assert opinion.action == STAND_DOWN
    assert opinion.refusal == RISK_UNBOUNDED
    assert "no ceiling" in opinion.reason


def test_the_usual_refusals_still_apply():
    subject = a_composer(margin=0.5, missing=1)
    assert subject.compose(a_vector(), CalibratedStub(0.5), a_timing(), a_plan()).refusal == CONVICTION_TOO_LOW
    assert subject.compose(
        a_vector(missing=("a", "b")), CalibratedStub(0.9), a_timing(), a_plan()
    ).refusal == FEATURES_INCOMPLETE
    assert subject.compose(a_vector(), CalibratedStub(0.9), a_timing(STAND_DOWN), a_plan()).refusal == TIMING_REFUSED
    assert subject.compose(a_vector(), CalibratedStub(0.9), a_timing(), None).refusal == NO_EXIT_PLAN


def test_a_composer_with_no_risk_ceiling_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearOpinionComposer(
            conviction_floor=a_floor(), maximum_missing_features=1,
            maximum_risk_fraction=0.0, require_trained_model=False,
        )


def test_a_stand_down_is_published_rather_than_dropped():
    subject = a_composer()
    opinion = subject.compose(a_vector(), CalibratedStub(0.1), a_timing(), a_plan())
    assert opinion.symbol == SYMBOL and opinion.reason
    assert subject.standing.stood_down == 1


# ---- bear-setup-weight-learner ----------------------------------------------

def a_weight_learner(minimum=10, floor=0.1, cap=3.0, tolerated_tail=3.0):
    return BearSetupWeightLearner(
        prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum, minimum_weight=floor, maximum_weight=cap,
        loss_window=200, prior_loss_fraction=0.02, prior_win_fraction=0.02,
        tail_quantile=0.95, tolerated_tail_ratio=tolerated_tail,
    )


def test_a_detector_is_measured_against_the_rest_of_the_short_book():
    subject = a_weight_learner()
    for index in range(300):
        subject.observe_closed_trade("dominant", index % 5 == 0, 0.02)
    for _ in range(20):
        subject.observe_closed_trade("occasional", True, 0.02)
    assert subject.weight_for("dominant").weight < 1.0


def test_a_detector_that_wins_often_and_loses_catastrophically_is_discounted():
    """Hit rate alone cannot tell a steady detector from one that is short volatility."""
    subject = a_weight_learner(tolerated_tail=3.0)
    for index in range(100):
        if index % 10 == 0:
            subject.observe_closed_trade("short-volatility", False, 0.60)
        else:
            subject.observe_closed_trade("short-volatility", True, 0.01)
        subject.observe_closed_trade("steady", index % 4 != 0, 0.02)

    tail, _ = subject.tail_ratio("short-volatility")
    assert tail > 3.0
    weight = subject.weight_for("short-volatility")
    assert "short volatility" in weight.reason
    assert weight.weight < subject.weight_for("steady").weight * 1.5
    assert subject.standing.detectors_discounted_for_tail >= 1


def test_a_detector_with_a_contained_tail_is_not_discounted():
    subject = a_weight_learner(tolerated_tail=3.0)
    for index in range(200):
        subject.observe_closed_trade("contained", index % 3 != 0, 0.02)
        subject.observe_closed_trade("other", index % 2 == 0, 0.02)
    assert "discounted" not in subject.weight_for("contained").reason


def test_a_losing_detector_is_discounted_never_deleted():
    subject = a_weight_learner(floor=0.1)
    for index in range(300):
        subject.observe_closed_trade("hopeless", False, 0.02)
        subject.observe_closed_trade("ordinary", index % 2 == 0, 0.02)
    weight = subject.weight_for("hopeless")
    assert weight.weight == 0.1
    assert "keeps being sampled" in weight.reason


def test_a_restarted_learner_adopts_the_short_scorecard():
    scorecard = BotScorecard(bot="bear-bot")
    for index in range(100):
        scorecard.record_closed_trade(DETECTOR, "falling", 0.7, index % 3 != 0, 0.02)
    subject = a_weight_learner()
    subject.observe_scorecard(scorecard)
    assert subject.weight_for(DETECTOR).trades_judged == 100


def test_a_zero_tolerated_tail_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearSetupWeightLearner(
            prior_hit_rate=0.5, prior_weight=4.0, half_life_observations=500,
            minimum_observations=10, minimum_weight=0.1, maximum_weight=3.0,
            loss_window=200, prior_loss_fraction=0.02, prior_win_fraction=0.02,
            tail_quantile=0.95, tolerated_tail_ratio=0.0,
        )


# ---- bear-position-invalidation-watcher -------------------------------------

class PositionStub:
    def __init__(self):
        self.venue_id, self.symbol = VENUE, SYMBOL


def a_watcher(clock=None, reduce_at=0.4, close_at=0.7, carry_limit=0.5,
              room_collapse=0.5, volatility_rise=1.5):
    return BearPositionInvalidationWatcher(
        reversal_fraction_to_reduce=reduce_at, reversal_fraction_to_close=close_at,
        squeeze_room_collapse_fraction=room_collapse, volatility_rise_fraction=volatility_rise,
        carry_fraction_of_expected_move=carry_limit, prior_invalidation_hit_rate=0.5,
        prior_weight=4.0, half_life_observations=200, minimum_observations=20,
        now_ns=clock or Clock(),
    )


def a_thesis(clock, features=None, regime="falling", horizon=600.0, expected=0.04):
    return HeldThesis(
        venue_id=VENUE, symbol=SYMBOL,
        entry_features=features or {
            "price_z_score": 2.0, "offer_side_imbalance": 0.4,
            "squeeze_room": 100.0, "realised_volatility_fraction": 0.01,
        },
        entry_regime=regime, entry_price=100.0, expected_move_fraction=expected,
        horizon_seconds=horizon, opened_at_ns=clock(), detector=DETECTOR,
    )


def a_now_vector(**overrides):
    features = {
        "price_z_score": 1.5, "offer_side_imbalance": 0.3,
        "squeeze_room": 90.0, "realised_volatility_fraction": 0.011,
    }
    features.update(overrides)
    return a_vector(features)


def test_a_short_thesis_that_still_holds_is_held():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock))
    opinion = subject.check(PositionStub(), a_now_vector())
    assert opinion.action == STAND_DOWN
    assert STILL_VALID in subject.standing.by_reason


def test_a_forming_squeeze_closes_before_a_majority_of_features_flip():
    """Waiting for the majority means exiting after the squeeze, not during it."""
    clock = Clock()
    subject = a_watcher(clock, room_collapse=0.5, volatility_rise=1.5)
    subject.record_entry(a_thesis(clock))
    opinion = subject.check(
        PositionStub(), a_now_vector(squeeze_room=20.0, realised_volatility_fraction=0.03)
    )
    assert opinion.action == CLOSE_POSITION
    assert SQUEEZE_FORMING in subject.standing.by_reason
    assert subject.standing.squeezes_caught == 1


def test_carry_that_has_eaten_the_expected_move_closes_the_short():
    """Invisible to any feature comparison: the thesis was paid away, not disproved."""
    clock = Clock()
    subject = a_watcher(clock, carry_limit=0.5)
    subject.record_entry(a_thesis(clock, expected=0.04))
    for _ in range(5):
        subject.observe_funding_settlement(VENUE, SYMBOL, 0.005)
    opinion = subject.check(PositionStub(), a_now_vector())
    assert opinion.action == CLOSE_POSITION
    assert CARRY_ATE_THE_THESIS in subject.standing.by_reason
    assert subject.standing.carry_closes == 1


def test_carry_that_is_being_received_never_closes_the_short():
    clock = Clock()
    subject = a_watcher(clock, carry_limit=0.5)
    subject.record_entry(a_thesis(clock))
    for _ in range(20):
        subject.observe_funding_settlement(VENUE, SYMBOL, -0.005)
    assert subject.check(PositionStub(), a_now_vector()).action == STAND_DOWN


def test_features_reversing_close_and_partial_reversal_reduces():
    clock = Clock()
    subject = a_watcher(clock, reduce_at=0.4, close_at=0.7)
    subject.record_entry(a_thesis(clock, features={"a": 1.0, "b": 1.0, "c": 1.0}))
    assert subject.check(PositionStub(), a_vector({"a": -1.0, "b": -1.0, "c": -1.0})).action == CLOSE_POSITION
    assert subject.check(PositionStub(), a_vector({"a": -1.0, "b": 1.0, "c": 1.0})).action == STAND_DOWN
    assert subject.check(PositionStub(), a_vector({"a": -1.0, "b": -1.0, "c": 1.0})).action == REDUCE_POSITION


def test_a_broken_regime_and_an_expired_horizon_both_close():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock, regime="falling"))
    subject.observe_regime_break("falling", True)
    assert subject.check(PositionStub(), a_now_vector()).action == CLOSE_POSITION
    assert REGIME_BROKEN in subject.standing.by_reason

    subject.observe_regime_break("falling", False)
    clock.advance_seconds(601)
    assert subject.check(PositionStub(), a_now_vector()).action == CLOSE_POSITION
    assert HORIZON_EXPIRED in subject.standing.by_reason


def test_a_position_with_no_recorded_thesis_is_not_closed_on_a_guess():
    opinion = a_watcher().check(PositionStub(), a_now_vector())
    assert opinion.action == STAND_DOWN
    assert opinion.refusal == NO_ENTRY_RECORD


def test_a_closed_short_releases_its_thesis_and_its_carry():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock))
    subject.observe_funding_settlement(VENUE, SYMBOL, 0.001)
    subject.forget_position(VENUE, SYMBOL)
    assert subject.standing.positions_watched == 0
    subject.observe_funding_settlement(VENUE, SYMBOL, 0.001)
    assert subject._carry_paid == {}


def test_the_opinion_shows_entry_now_and_what_carry_has_cost():
    clock = Clock()
    subject = a_watcher(clock)
    subject.record_entry(a_thesis(clock))
    subject.observe_funding_settlement(VENUE, SYMBOL, 0.002)
    summary = subject.check(PositionStub(), a_now_vector()).features_summary
    assert summary["entry"]["squeeze_room"] == 100.0
    assert summary["carry_paid_fraction"] == pytest.approx(0.002)


def test_the_watcher_is_judged_too():
    subject = a_watcher()
    for _ in range(50):
        subject.observe_outcome(True)
    assert subject.invalidation_record.is_fitted
    assert subject.invalidation_record.value > 0.6


def test_a_short_is_planned_before_any_short_has_closed_from_the_symbols_own_range():
    """The bull bot needed this to produce its first trade; so does the bear."""
    clock = Clock()
    subject = a_proposer(minimum_reward=1.0, clock=clock)
    # Forty prints swinging 2% inside the claimed horizon, then a short candidate.
    for index in range(40):
        subject.observe_price(VENUE, SYMBOL, 100.0 + (2.0 if index % 2 else 0.0))
        clock.advance_seconds(1.0)
    candidate = a_side_candidate()
    plan, outcome = subject.propose(candidate, ConvictionStub(0.8))
    assert plan is not None, outcome
    assert plan.stop_price > 100.0 and all(target.price < 100.0 for target in plan.targets)
    assert "no short has closed in it yet" in plan.reason


def test_too_few_prints_refuse_a_cold_start_plan_by_name():
    subject = a_proposer()
    subject.observe_price(VENUE, SYMBOL, 100.0)
    plan, outcome = subject.propose(a_side_candidate(), ConvictionStub(0.8))
    assert plan is None and outcome == NO_RANGE
