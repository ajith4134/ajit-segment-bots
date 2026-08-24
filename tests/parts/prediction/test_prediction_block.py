"""Prediction: what the market does next, and everything it refuses to claim.

The forecasts here are the numbers most easily believed without evidence, so most
of these tests check refusals: a model that is not loaded, a window with a hole in
it, an unmeasured forecaster, a model asked about something it never saw. Each of
those has a state of its own, and none of them is a zero.

The entropy parts are checked against `docs/research/order-flow-entropy.md`
directly, including Theorem 2 -- the label-swap symmetry that makes direction
mathematically unrecoverable. Real BTCUSDT candles come from the tape where a
real series is what makes the test meaningful (RL-063).
"""

import importlib
import json
import math

import pytest

from parts.prediction.entropy_magnitude_forecaster import (
    NOT_YET_MEASURED, NO_ENTROPY, QUINTILE_COUNT, EntropyMagnitudeForecaster,
)
from parts.prediction.flow_entropy_meter import (
    MEASURED, NO_TRANSITIONS, WINDOW_TOO_SHORT as ENTROPY_WINDOW_TOO_SHORT, FlowEntropyMeter,
)
from parts.prediction.forecast_distribution_gate import (
    FORECAST_LARGER_THAN_ANY_SEEN, IN_DISTRIBUTION, NO_TRAINING_STATISTICS,
    VOLATILITY_UNLIKE_TRAINING, WINDOW_UNLIKE_TRAINING, ForecastDistributionGate,
    TrainingStatistics,
)
from parts.prediction.forecast_ensembler import (
    EXCLUDED_FLAGGED, EXCLUDED_UNMEASURED, NOTHING_USABLE, NO_TRUSTED_MEMBER, ForecastEnsembler,
)
from parts.prediction.forecast_scorer import ForecastScorer
from parts.prediction.funding_rate_forecaster import (
    FORECAST, NO_PREMIUM_OBSERVATIONS, NO_VENUE_PARAMETERS, FundingParameters,
    FundingRateForecaster,
)
from parts.prediction.implied_vol_reader import (
    ALL_STALE, NO_FEED, READABLE, TOO_THIN, ImpliedVolReader, OptionQuote,
)
from parts.prediction.kline_window_builder import INTERVAL_SECONDS, KlineWindowBuilder
from parts.prediction.kronos_finetuner import (
    DID_NOT_IMPROVE, NO_ACCELERATOR as FINETUNE_NO_ACCELERATOR, NO_TRAINER, TOO_LITTLE_DATA,
    TRAINED, KronosFinetuner,
)
from parts.prediction.kronos_forecaster import CHALLENGER, CHAMPION, KronosForecaster, LoadedModel
from parts.prediction.kronos_size_selector import (
    CHOSEN_ON_ACCURACY, HELD_BY_HYSTERESIS, NOTHING_MEASURED, SAMPLING_AN_UNMEASURED_SIZE,
    KronosSizeSelector,
)
from parts.prediction.liquidation_cluster_mapper import (
    MAPPED, NO_MARGIN_SCHEDULE, NO_OPEN_INTEREST, NO_VOLUME_PROFILE, LiquidationClusterMapper,
    MarginTier,
)
from parts.prediction.model_drift_monitor import (
    ACCURACY_FELL, MODEL_IS_FORGETTING, NOT_ENOUGH_HISTORY, NO_DRIFT, RETRAIN, SWAP_CHAMPION,
    ModelDriftMonitor,
)
from parts.prediction.order_flow_state_encoder import (
    STATE_COUNT, OrderFlowState, OrderFlowStateEncoder,
)
from parts.prediction.realised_vol_regressor import (
    FEATURES_MISSING, NOT_ENOUGH_TRAINING, RealisedVolRegressor, REQUIRED_FEATURES,
)
from parts.prediction.volatility_feature_builder import FEATURE_NAMES, VolatilityFeatureBuilder
from runtime.forecast_types import (
    AVAILABLE, MODEL_NOT_LOADED, NO_ACCELERATOR, WINDOW_TOO_SHORT, Candle, ForecastAccuracy,
    KlineWindow, PriceForecast, VolatilityForecast, annualise, unavailable_forecast,
)
from runtime.learned_estimator import Estimate
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "kline-window-builder": "parts.prediction.kline_window_builder",
    "kronos-forecaster": "parts.prediction.kronos_forecaster",
    "kronos-finetuner": "parts.prediction.kronos_finetuner",
    "forecast-scorer": "parts.prediction.forecast_scorer",
    "kronos-size-selector": "parts.prediction.kronos_size_selector",
    "implied-vol-reader": "parts.prediction.implied_vol_reader",
    "volatility-feature-builder": "parts.prediction.volatility_feature_builder",
    "realised-vol-regressor": "parts.prediction.realised_vol_regressor",
    "order-flow-state-encoder": "parts.prediction.order_flow_state_encoder",
    "flow-entropy-meter": "parts.prediction.flow_entropy_meter",
    "entropy-magnitude-forecaster": "parts.prediction.entropy_magnitude_forecaster",
    "funding-rate-forecaster": "parts.prediction.funding_rate_forecaster",
    "liquidation-cluster-mapper": "parts.prediction.liquidation_cluster_mapper",
    "forecast-ensembler": "parts.prediction.forecast_ensembler",
    "model-drift-monitor": "parts.prediction.model_drift_monitor",
    "forecast-distribution-gate": "parts.prediction.forecast_distribution_gate",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
SECOND_NS = 1_000_000_000
MINUTE_NS = 60 * SECOND_NS


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_seconds(self, seconds):
        self.now_ns += int(seconds * 1e9)


def an_estimate(value, observations=100, is_fitted=True):
    return Estimate(
        value=value, is_fitted=is_fitted, observations=observations, prior=0.5,
        was_clamped=False, bound_low=None, bound_high=None, reason="measured",
    )


def a_candle(open_time_ns, close, high=None, low=None, open_=None, volume=1.0, closed=True):
    open_ = close if open_ is None else open_
    return Candle(
        open_time_ns=open_time_ns, open=open_, high=high if high is not None else max(open_, close),
        low=low if low is not None else min(open_, close), close=close, volume=volume,
        quote_volume=volume * close, trades=10, is_closed=closed,
    )


def a_window(closes, venue=VENUE, symbol=SYMBOL, requested=None, gaps=(), start_ns=0):
    candles = tuple(
        a_candle(start_ns + index * MINUTE_NS, close) for index, close in enumerate(closes)
    )
    return KlineWindow(
        venue_id=venue, symbol=symbol, interval="1m", candles=candles,
        length_requested=requested if requested is not None else len(closes),
        gaps=tuple(gaps), built_at_ns=Clock()(),
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
def test_no_prediction_part_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        text = handle.read()
    for line in text.splitlines():
        if line.startswith("from parts.") or line.startswith("import parts."):
            raise AssertionError(f"{part_id} imports another part: {line}")


# ---- forecast substrate -----------------------------------------------------

def test_a_forecast_that_could_not_be_made_is_not_a_zero():
    """Zero reads as 'no move expected', which is a confident claim."""
    forecast = unavailable_forecast(
        VENUE, SYMBOL, "f", "m", MODEL_NOT_LOADED, 600.0, 0, "no weights"
    )
    assert forecast.expected_return is None
    assert forecast.is_usable is False


def test_volatility_scales_by_the_square_root_of_time():
    """Multiplying a daily number by 365 overstates a year nineteenfold."""
    daily = 0.02
    yearly = annualise(daily, horizon_seconds=86400, seconds_per_year=365 * 86400)
    assert yearly == pytest.approx(daily * math.sqrt(365))
    assert yearly < daily * 365


def test_a_volatility_over_no_time_is_refused():
    with pytest.raises(ValueError):
        annualise(0.02, horizon_seconds=0, seconds_per_year=1)


# ---- kline-window-builder ---------------------------------------------------

def a_builder(interval="1m", maximum=100, include_open=False):
    return KlineWindowBuilder(
        interval=interval, maximum_window=maximum, include_open_candle=include_open
    )


def feed_candles(builder, count, start=0, step=1, closed=True, price=100.0):
    for index in range(count):
        builder.observe_candle(
            VENUE, SYMBOL,
            a_candle((start + index * step) * MINUTE_NS, price + index, closed=closed),
        )


def test_a_window_of_consecutive_candles_is_complete():
    subject = a_builder()
    feed_candles(subject, 20)
    window = subject.build(VENUE, SYMBOL, 20)
    assert window.is_complete
    assert window.gaps == ()


def test_a_missing_interval_is_named_never_closed():
    """A model handed a spliced series learns that time moves at the feed's rate."""
    subject = a_builder()
    feed_candles(subject, 5)
    feed_candles(subject, 5, start=10)
    window = subject.build(VENUE, SYMBOL, 10)
    assert window.gaps
    assert window.is_complete is False
    assert "5 interval(s) missing" in window.gaps[0]


def test_an_open_candle_is_excluded_by_default():
    """A partial bar will change after it has been forecast from."""
    subject = a_builder(include_open=False)
    feed_candles(subject, 10)
    subject.observe_candle(VENUE, SYMBOL, a_candle(10 * MINUTE_NS, 999.0, closed=False))
    window = subject.build(VENUE, SYMBOL, 11)
    assert all(candle.is_closed for candle in window.candles)
    assert subject.standing.open_candles_excluded == 1


def test_a_revised_candle_replaces_rather_than_appends():
    subject = a_builder()
    subject.observe_candle(VENUE, SYMBOL, a_candle(0, 100.0, closed=False))
    subject.observe_candle(VENUE, SYMBOL, a_candle(0, 105.0, closed=True))
    window = subject.build(VENUE, SYMBOL, 5)
    assert len(window.candles) == 1
    assert window.candles[0].close == 105.0


def test_asking_for_more_than_is_kept_is_refused_rather_than_shortened():
    """Returning a shorter window silently hands the model a series it misreads."""
    subject = a_builder(maximum=50)
    with pytest.raises(ValueError):
        subject.build(VENUE, SYMBOL, 100)


def test_aggregation_refuses_a_group_missing_a_candle():
    """A 5-minute bar from four minutes is not a shorter bar, it is a wrong one."""
    subject = a_builder(maximum=100)
    for index in (0, 1, 2, 3, 5, 6, 7, 8, 9):
        subject.observe_candle(VENUE, SYMBOL, a_candle(index * MINUTE_NS, 100.0 + index))
    window = subject.aggregate(VENUE, SYMBOL, group_size=5, length=2)
    assert subject.standing.aggregations_refused >= 1


def test_aggregation_takes_the_first_open_and_the_extremes():
    subject = a_builder(maximum=100)
    for index in range(10):
        subject.observe_candle(
            VENUE, SYMBOL,
            a_candle(index * MINUTE_NS, 100.0 + index, high=200.0 + index, low=50.0 - index, open_=100.0 + index),
        )
    window = subject.aggregate(VENUE, SYMBOL, group_size=5, length=2)
    first = window.candles[0]
    assert first.open == 100.0
    assert first.close == 104.0
    assert first.high == 204.0
    assert first.low == 46.0


def test_an_unknown_interval_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        KlineWindowBuilder(interval="7m", maximum_window=100)


def test_releasing_a_symbol_drops_its_candles():
    """T-3: an off part releases its memory."""
    subject = a_builder()
    feed_candles(subject, 10)
    subject.release(VENUE, SYMBOL)
    assert subject.build(VENUE, SYMBOL, 10).candles == ()


def test_a_window_from_the_real_tape_computes_its_own_returns(real_trade_prices):
    window = a_window(real_trade_prices[:50])
    assert len(window.returns) == 49
    assert max(abs(value) for value in window.returns) < 0.1


# ---- kronos-forecaster ------------------------------------------------------

def a_kronos(steps=5, paths=8, quantile=0.8, interval=60.0):
    return KronosForecaster(
        forecast_steps=steps, sample_paths=paths, interval_quantile=quantile,
        interval_seconds=interval,
    )


def a_model(name="kronos-base", context=10, paths_return=None, device="cpu", finetuned=None):
    def predict(candles, steps, paths):
        last = candles[-1].close
        if paths_return is not None:
            return [[last * (1 + move)] for move in paths_return]
        return [[last * (1 + 0.001 * (index - paths / 2))] for index in range(paths)]

    return LoadedModel(
        name=name, context_length=context, predict=predict, device=device, finetuned_on=finetuned
    )


def test_no_weights_loaded_is_a_state_not_a_forecast():
    subject = a_kronos()
    forecast = subject.forecast(a_window([100.0] * 20))
    assert forecast.state == MODEL_NOT_LOADED
    assert forecast.expected_return is None
    assert "would read as 'no move expected'" in forecast.reason


def test_a_window_shorter_than_the_context_is_refused_not_padded():
    subject = a_kronos()
    subject.load_model(CHAMPION, a_model(context=50))
    forecast = subject.forecast(a_window([100.0] * 10))
    assert forecast.state == WINDOW_TOO_SHORT
    assert "padding would feed it a series it never saw" in forecast.reason


def test_no_accelerator_slot_stops_the_forecast():
    """A part that ran anyway would be reaching into the control plane."""
    subject = a_kronos()
    subject.load_model(CHAMPION, a_model())
    subject.set_accelerator_slot(False)
    assert subject.forecast(a_window([100.0] * 20)).state == NO_ACCELERATOR


def test_a_forecast_is_a_distribution_not_a_single_path():
    """One sampled path is a draw wearing the authority of an estimate."""
    subject = a_kronos(paths=5, quantile=0.8)
    subject.load_model(CHAMPION, a_model(paths_return=[-0.02, -0.01, 0.0, 0.01, 0.02]))
    forecast = subject.forecast(a_window([100.0] * 20))
    assert forecast.state == AVAILABLE
    assert forecast.expected_return == pytest.approx(0.0)
    assert forecast.lower_return < forecast.expected_return < forecast.upper_return
    assert forecast.interval_width > 0


def test_one_sampled_path_is_refused_at_construction():
    with pytest.raises(ValueError):
        KronosForecaster(
            forecast_steps=5, sample_paths=1, interval_quantile=0.8, interval_seconds=60.0
        )


def test_promotion_is_delivered_never_taken():
    subject = a_kronos()
    subject.load_model(CHAMPION, a_model(name="base"))
    subject.load_model(CHALLENGER, a_model(name="finetuned"))
    assert subject.forecast(a_window([100.0] * 20)).model_name == "base"
    subject.apply_champion_choice(CHALLENGER)
    assert subject.forecast(a_window([100.0] * 20)).model_name == "finetuned"
    assert subject.standing.champion_swaps == 1


def test_a_gap_in_the_window_is_reported_in_the_forecast():
    subject = a_kronos()
    subject.load_model(CHAMPION, a_model())
    forecast = subject.forecast(a_window([100.0] * 20, gaps=("3 missing",)))
    assert "not consecutive" in forecast.reason


def test_releasing_the_model_frees_it():
    """T-3, and these are the largest thing this part holds."""
    subject = a_kronos()
    subject.load_model(CHAMPION, a_model())
    subject.release()
    assert subject.forecast(a_window([100.0] * 20)).state == MODEL_NOT_LOADED


# ---- kronos-finetuner -------------------------------------------------------

def a_finetuner(minimum=10, validation=0.2, epochs=1, maximum=100):
    return KronosFinetuner(
        minimum_training_windows=minimum, validation_fraction=validation, epochs=epochs,
        refit_tokenizer=True, maximum_windows_held=maximum,
    )


def a_trainer(score=0.6, refitted=True):
    def train(training, validation, epochs, refit_tokenizer, weights):
        return (f"/tmp/model-{len(training)}", score, refitted)

    return train


def test_no_trainer_installed_produces_no_artefact():
    """An artefact that was never trained but carries a version number is worse than none."""
    subject = a_finetuner()
    for index in range(20):
        subject.observe_window(a_window([100.0] * 5))
    model, outcome = subject.finetune()
    assert model is None
    assert outcome == NO_TRAINER


def test_no_accelerator_slot_stops_the_run():
    subject = a_finetuner()
    subject.install_trainer(a_trainer())
    subject.set_accelerator_slot(False)
    assert subject.finetune()[1] == FINETUNE_NO_ACCELERATOR


def test_too_few_windows_is_refused():
    subject = a_finetuner(minimum=50)
    subject.install_trainer(a_trainer())
    for _ in range(10):
        subject.observe_window(a_window([100.0] * 5))
    assert subject.finetune()[1] == TOO_LITTLE_DATA


def test_the_validation_split_is_the_recent_tail_never_random():
    """A random split lets the model see the future of its own training rows."""
    subject = a_finetuner(minimum=10, validation=0.2)
    for index in range(10):
        subject.observe_window(a_window([100.0 + index] * 5))
    training, validation = subject.split()
    assert len(training) == 8 and len(validation) == 2
    assert validation[0].candles[0].close > training[-1].candles[0].close


def test_a_model_that_did_not_beat_the_champion_is_discarded():
    """Retraining on drift that was noise makes the model worse every time."""
    subject = a_finetuner(minimum=10)
    subject.install_trainer(a_trainer(score=0.5))
    subject.observe_champion_score(0.6)
    for _ in range(20):
        subject.observe_window(a_window([100.0] * 5))
    model, outcome = subject.finetune()
    assert outcome == DID_NOT_IMPROVE
    assert model.beat_the_champion is False
    assert subject.standing.discarded_no_improvement == 1


def test_a_model_that_beat_the_champion_is_offered():
    subject = a_finetuner(minimum=10)
    subject.install_trainer(a_trainer(score=0.7))
    subject.observe_champion_score(0.6)
    for _ in range(20):
        subject.observe_window(a_window([100.0] * 5))
    model, outcome = subject.finetune()
    assert outcome == TRAINED
    assert model.beat_the_champion
    assert model.tokenizer_was_refitted


def test_a_drift_alert_becomes_a_retrain_request_carrying_its_reason():
    subject = a_finetuner()
    subject.observe_drift_alert("accuracy fell 8%")
    assert subject.has_a_pending_request


def test_a_validation_fraction_of_most_of_the_data_is_refused():
    with pytest.raises(ValueError):
        KronosFinetuner(
            minimum_training_windows=10, validation_fraction=0.8, epochs=1,
            refit_tokenizer=True, maximum_windows_held=100,
        )


# ---- forecast-scorer --------------------------------------------------------

def a_scorer(minimum=10, clock=None):
    scorer = ForecastScorer(
        prior_accuracy=0.5, prior_weight=4.0, half_life_observations=500,
        minimum_observations=minimum,
    )
    if clock is not None:
        scorer._now_ns = clock
    return scorer


def a_forecast(expected=0.01, lower=-0.01, upper=0.03, horizon=60.0, at_ns=0, state=AVAILABLE):
    return PriceForecast(
        venue_id=VENUE, symbol=SYMBOL, forecaster="kronos-forecaster", model_name="base",
        state=state, expected_return=expected if state == AVAILABLE else None,
        lower_return=lower if state == AVAILABLE else None,
        upper_return=upper if state == AVAILABLE else None,
        horizon_seconds=horizon, window_length=100, reason="forecast", forecast_at_ns=at_ns,
    )


def test_a_forecast_is_not_scored_before_its_horizon_elapses():
    """Scoring early credits the model for a move that has not finished."""
    clock = Clock()
    subject = a_scorer(clock=clock)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.take_forecast(a_forecast(horizon=60.0, at_ns=clock()))
    assert subject.score_due() == ()
    clock.advance_seconds(61)
    subject.observe_price(VENUE, SYMBOL, 101.0, subject._now_ns())
    assert len(subject.score_due()) == 1


def test_an_unusable_forecast_is_counted_not_scored_as_a_miss():
    """Folding a refusal in as a miss would make honesty look like being wrong."""
    subject = a_scorer()
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.take_forecast(a_forecast(state=MODEL_NOT_LOADED))
    assert subject.standing.unusable_forecasts_counted == 1
    assert subject.standing.still_waiting == 0


def test_direction_and_magnitude_are_scored_separately():
    clock = Clock()
    subject = a_scorer(minimum=1, clock=clock)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.take_forecast(a_forecast(expected=0.10, horizon=60.0, at_ns=clock()))
    clock.advance_seconds(61)
    subject.observe_price(VENUE, SYMBOL, 100.5, subject._now_ns())
    accuracy = subject.score_due()[0]
    assert accuracy.directional_accuracy.value > 0.5, "it called the direction"
    assert accuracy.mean_absolute_error > 0.09, "and badly overshot the magnitude"


def test_interval_coverage_is_scored():
    """A model whose 80% interval contains the outcome 40% of the time means nothing."""
    clock = Clock()
    subject = a_scorer(minimum=1, clock=clock)
    for index in range(5):
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
        subject.take_forecast(a_forecast(expected=0.0, lower=-0.001, upper=0.001, at_ns=clock()))
        clock.advance_seconds(61)
        subject.observe_price(VENUE, SYMBOL, 110.0, subject._now_ns())
        subject.score_due()
    accuracy = subject._records[
        ("kronos-forecaster", "base", f"{VENUE}:{SYMBOL}", 60.0)
    ].coverage.estimate(1)
    assert accuracy.value < 0.5


def test_a_forecast_of_exactly_zero_is_not_scored_as_a_direction():
    """A model forecasting zero has not called a direction."""
    clock = Clock()
    subject = a_scorer(minimum=1, clock=clock)
    subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.take_forecast(a_forecast(expected=0.0, at_ns=clock()))
    clock.advance_seconds(61)
    subject.observe_price(VENUE, SYMBOL, 105.0, subject._now_ns())
    accuracy = subject.score_due()[0]
    assert accuracy.directional_accuracy.observations == 0


# ---- kronos-size-selector ---------------------------------------------------

def a_selector(sizes=("kronos-mini", "kronos-small", "kronos-base"), minimum=10,
               margin=0.03, sample_every=1000):
    return KronosSizeSelector(
        sizes=sizes, minimum_forecasts=minimum, switch_margin=margin, sample_every=sample_every
    )


def an_accuracy(model_name, accuracy, scored=100, horizon=60.0):
    return ForecastAccuracy(
        forecaster="kronos-forecaster", model_name=model_name, venue_id=VENUE, symbol=SYMBOL,
        horizon_seconds=horizon, directional_accuracy=an_estimate(accuracy, scored),
        mean_absolute_error=0.01, interval_coverage=an_estimate(0.8),
        forecasts_scored=scored, reason="scored", scored_at_ns=0,
    )


def test_the_most_accurate_size_is_chosen():
    subject = a_selector()
    subject.observe_accuracy(an_accuracy("kronos-mini", 0.52))
    subject.observe_accuracy(an_accuracy("kronos-base", 0.61))
    choice = subject.choose(VENUE, SYMBOL, 60.0)
    assert choice.model_name == "kronos-base"
    assert choice.state == CHOSEN_ON_ACCURACY
    assert "rather than on what the machine has free" in choice.reason


def test_a_size_never_tried_is_sampled():
    """Otherwise the first size tried keeps being chosen forever."""
    subject = a_selector(sample_every=1)
    subject.observe_accuracy(an_accuracy("kronos-mini", 0.60))
    choice = subject.choose(VENUE, SYMBOL, 60.0)
    assert choice.state == SAMPLING_AN_UNMEASURED_SIZE


def test_nothing_measured_holds_the_incumbent_and_says_it_is_not_a_measured_choice():
    subject = a_selector(minimum=1000, sample_every=10_000)
    choice = subject.choose(VENUE, SYMBOL, 60.0)
    assert choice.state == NOTHING_MEASURED
    assert choice.is_measured is False


def test_a_narrow_lead_does_not_cause_a_switch():
    """Every switch discards the record being accumulated for the size it left."""
    subject = a_selector(margin=0.05)
    subject.observe_accuracy(an_accuracy("kronos-mini", 0.60))
    subject.observe_accuracy(an_accuracy("kronos-base", 0.58))
    subject.choose(VENUE, SYMBOL, 60.0)
    subject.observe_accuracy(an_accuracy("kronos-base", 0.62))
    held = subject.choose(VENUE, SYMBOL, 60.0)
    assert held.state == HELD_BY_HYSTERESIS
    assert held.model_name == "kronos-mini"


def test_a_clear_lead_does_cause_a_switch():
    subject = a_selector(margin=0.02)
    subject.observe_accuracy(an_accuracy("kronos-mini", 0.60))
    subject.observe_accuracy(an_accuracy("kronos-base", 0.55))
    assert subject.choose(VENUE, SYMBOL, 60.0).model_name == "kronos-mini"
    subject.observe_accuracy(an_accuracy("kronos-base", 0.75))
    assert subject.choose(VENUE, SYMBOL, 60.0).model_name == "kronos-base"
    assert subject.standing.switches == 1


def test_choices_are_kept_per_symbol_and_horizon():
    subject = a_selector()
    subject.observe_accuracy(an_accuracy("kronos-mini", 0.70, horizon=60.0))
    subject.observe_accuracy(an_accuracy("kronos-base", 0.70, horizon=3600.0))
    assert subject.choose(VENUE, SYMBOL, 60.0).model_name == "kronos-mini"
    assert subject.choose(VENUE, SYMBOL, 3600.0).model_name == "kronos-base"


def test_a_selector_with_no_switch_margin_is_refused():
    with pytest.raises(ValueError):
        KronosSizeSelector(
            sizes=("a", "b"), minimum_forecasts=10, switch_margin=0.0, sample_every=10
        )


# ---- implied-vol-reader -----------------------------------------------------

def a_vol_reader(maximum_age=60.0, minimum_strikes=3, clock=None):
    reader = ImpliedVolReader(
        maximum_quote_age_seconds=maximum_age, minimum_strikes_per_expiry=minimum_strikes
    )
    if clock is not None:
        reader._now_ns = clock
    return reader


def a_quote(strike, implied, bid=1.0, ask=1.2, expiry=86400.0, at_ns=0, kind="call"):
    return OptionQuote(
        symbol=f"BTC-{kind}-{strike}", underlying=SYMBOL, kind=kind, strike=strike,
        seconds_to_expiry=expiry, bid=bid, ask=ask, implied_volatility=implied,
        quoted_at_ns=at_ns,
    )


def test_no_options_feed_produces_no_surface_rather_than_a_flat_one():
    """A flat surface is an invented forward view reaching every sizing decision."""
    subject = a_vol_reader()
    surface = subject.read(VENUE, SYMBOL, spot=100.0)
    assert surface.state == NO_FEED
    assert surface.is_usable is False


def test_a_one_sided_quote_is_not_a_data_point():
    """A surface from one-sided marks is built from where nobody will trade."""
    clock = Clock()
    subject = a_vol_reader(clock=clock)
    subject.set_feed_connected(True)
    for strike in (90, 100, 110):
        subject.observe_quote(VENUE, a_quote(strike, 0.5, bid=None, at_ns=clock()))
    surface = subject.read(VENUE, SYMBOL, spot=100.0)
    assert surface.state == ALL_STALE
    assert surface.quotes_dropped["one-sided"] == 3


def test_a_stale_quote_is_dropped_not_carried():
    clock = Clock()
    subject = a_vol_reader(maximum_age=60.0, clock=clock)
    subject.set_feed_connected(True)
    for strike in (90, 100, 110):
        subject.observe_quote(VENUE, a_quote(strike, 0.5, at_ns=clock()))
    clock.advance_seconds(120)
    surface = subject.read(VENUE, SYMBOL, spot=100.0)
    assert surface.quotes_dropped["stale"] == 3


def test_a_thin_surface_is_published_as_thin_not_smoothed():
    clock = Clock()
    subject = a_vol_reader(minimum_strikes=5, clock=clock)
    subject.set_feed_connected(True)
    for strike in (90, 100, 110):
        subject.observe_quote(VENUE, a_quote(strike, 0.5, at_ns=clock()))
    assert subject.read(VENUE, SYMBOL, spot=100.0).state == TOO_THIN


def test_a_readable_surface_carries_its_skew():
    clock = Clock()
    subject = a_vol_reader(clock=clock)
    subject.set_feed_connected(True)
    for strike, implied in ((90, 0.70), (100, 0.50), (110, 0.45)):
        subject.observe_quote(VENUE, a_quote(strike, implied, at_ns=clock()))
    surface = subject.read(VENUE, SYMBOL, spot=100.0)
    assert surface.state == READABLE
    assert surface.at_the_money[86400.0] == 0.50
    assert surface.skew[86400.0] == pytest.approx(0.25), "puts bid over calls: a crash priced"


def test_a_point_nobody_quoted_returns_none_not_an_interpolation():
    """The wing is where the information is, and inventing it is confident nonsense."""
    clock = Clock()
    subject = a_vol_reader(clock=clock)
    subject.set_feed_connected(True)
    for strike in (90, 100, 110):
        subject.observe_quote(VENUE, a_quote(strike, 0.5, at_ns=clock()))
    surface = subject.read(VENUE, SYMBOL, spot=100.0)
    assert surface.volatility_at(86400.0, 95.0) is None


def test_a_reader_with_no_staleness_bound_is_refused():
    with pytest.raises(ValueError):
        ImpliedVolReader(maximum_quote_age_seconds=0.0, minimum_strikes_per_expiry=3)


# ---- volatility-feature-builder ---------------------------------------------

def a_vol_builder(short=5, long=20, minimum=5):
    return VolatilityFeatureBuilder(
        short_window=short, long_window=long, minimum_observations=minimum
    )


def a_candle_window(count=30, base=100.0, step=1.0):
    candles = tuple(
        a_candle(
            index * MINUTE_NS, base + index * step,
            high=base + index * step + 2, low=base + index * step - 2,
            open_=base + index * step - 0.5,
        )
        for index in range(count)
    )
    return KlineWindow(
        venue_id=VENUE, symbol=SYMBOL, interval="1m", candles=candles,
        length_requested=count, gaps=(), built_at_ns=0,
    )


def test_a_feature_that_cannot_be_measured_is_named_not_zeroed():
    subject = a_vol_builder()
    features = subject.build(a_candle_window(), horizon_seconds=3600.0)
    assert "implied_at_the_money" in features.missing
    assert "implied_at_the_money" not in features.features


def test_parkinson_and_garman_klass_use_what_close_to_close_throws_away():
    """Close-to-close discards the high and the low, which is most of a candle."""
    subject = a_vol_builder()
    features = subject.build(a_candle_window(), horizon_seconds=3600.0).features
    assert features["parkinson"] > 0
    assert features["garman_klass"] >= 0
    assert features["parkinson"] != features["close_to_close_long"]


def test_a_symbol_that_moves_and_comes_back_shows_it_in_the_range_ratio():
    subject = a_vol_builder(minimum=3)
    wide = tuple(
        a_candle(index * MINUTE_NS, 100.0, high=120.0, low=80.0, open_=100.0)
        for index in range(30)
    )
    quiet = tuple(
        a_candle(index * MINUTE_NS, 100.0 + index * 0.5, high=100.5 + index * 0.5,
                 low=99.5 + index * 0.5, open_=100.0 + index * 0.5)
        for index in range(30)
    )
    wide_window = KlineWindow(VENUE, SYMBOL, "1m", wide, 30, (), 0)
    quiet_window = KlineWindow(VENUE, SYMBOL, "1m", quiet, 30, (), 0)
    assert subject.build(wide_window, 3600.0).features["parkinson"] > subject.build(
        quiet_window, 3600.0
    ).features["parkinson"]


def test_the_implied_premium_appears_when_a_surface_exists():
    class Surface:
        is_usable = True
        at_the_money = {3600.0: 0.8}
        skew = {3600.0: 0.05}

    subject = a_vol_builder(minimum=3)
    subject.observe_surface(VENUE, SYMBOL, Surface())
    features = subject.build(a_candle_window(), horizon_seconds=3600.0)
    assert features.features["implied_at_the_money"] == 0.8
    assert features.features["implied_over_realised"] > 0
    assert features.is_complete


def test_one_window_cannot_tell_rising_volatility_from_falling():
    with pytest.raises(ValueError):
        VolatilityFeatureBuilder(short_window=20, long_window=20, minimum_observations=5)


# ---- realised-vol-regressor -------------------------------------------------

class FeatureSetStub:
    def __init__(self, features):
        self.venue_id, self.symbol = VENUE, SYMBOL
        self.features = dict(features)
        self.missing = ()


def a_regressor(minimum_training=20, minimum_feature=5, horizon=3600.0):
    return RealisedVolRegressor(
        learning_rate=0.1, l2_regularisation=0.0001, feature_half_life_observations=500,
        minimum_feature_observations=minimum_feature,
        minimum_training_observations=minimum_training, horizon_seconds=horizon,
    )


def test_an_untrained_regressor_produces_no_number():
    subject = a_regressor(minimum_training=50)
    forecast = subject.forecast(FeatureSetStub({name: 0.01 for name in REQUIRED_FEATURES}))
    assert forecast.state == NOT_ENOUGH_TRAINING
    assert forecast.expected_volatility is None


def test_a_missing_required_feature_stops_the_forecast():
    """A forecast without it would be from a different model than the one trained."""
    subject = a_regressor(minimum_training=1)
    assert subject.forecast(FeatureSetStub({"parkinson": 0.01})).state == FEATURES_MISSING


def test_the_regressor_learns_that_volatility_clusters():
    subject = a_regressor(minimum_training=20, minimum_feature=5)
    for index in range(300):
        level = 0.01 if index % 2 else 0.05
        subject.train(
            FeatureSetStub({"close_to_close_short": level, "close_to_close_long": level}),
            realised_volatility=level,
        )
    quiet = subject.forecast(
        FeatureSetStub({"close_to_close_short": 0.01, "close_to_close_long": 0.01})
    )
    wild = subject.forecast(
        FeatureSetStub({"close_to_close_short": 0.05, "close_to_close_long": 0.05})
    )
    assert quiet.expected_volatility < wild.expected_volatility


def test_the_forecast_is_never_negative():
    """A linear model on the raw quantity predicts negative volatility on quiet days."""
    subject = a_regressor(minimum_training=10, minimum_feature=3)
    for index in range(200):
        level = 0.001 * (index % 10 + 1)
        subject.train(
            FeatureSetStub({"close_to_close_short": level, "close_to_close_long": level}), level
        )
    forecast = subject.forecast(
        FeatureSetStub({"close_to_close_short": 1e-9, "close_to_close_long": 1e-9})
    )
    assert forecast.expected_volatility > 0


def test_a_zero_realised_volatility_is_refused_as_training():
    """A flat window is an absence of an observation, not an observation of zero."""
    subject = a_regressor()
    with pytest.raises(ValueError):
        subject.train(FeatureSetStub({"close_to_close_short": 0.01}), realised_volatility=0.0)


def test_the_coefficients_can_be_read():
    subject = a_regressor(minimum_training=5, minimum_feature=3)
    for index in range(100):
        level = 0.01 if index % 2 else 0.03
        subject.train(
            FeatureSetStub({"close_to_close_short": level, "close_to_close_long": level}), level
        )
    assert subject.coefficients


# ---- order-flow-state-encoder -----------------------------------------------

def an_encoder(window=120, minimum=10):
    return OrderFlowStateEncoder(volume_window_seconds=window, minimum_volume_observations=minimum)


def test_the_state_space_is_the_fifteen_the_specification_names():
    assert STATE_COUNT == 15


def test_a_second_with_no_trades_is_a_state_not_a_gap():
    """Skipping it splices two non-adjacent seconds into a transition that never happened."""
    subject = an_encoder()
    subject.observe_trade(VENUE, SYMBOL, 100.0, 1.0, 0)
    subject.observe_trade(VENUE, SYMBOL, 101.0, 1.0, 5 * SECOND_NS)
    assert subject.standing.empty_seconds_encoded == 4


def test_the_volume_quintile_is_relative_to_recent_activity():
    """A fixed threshold would measure the symbol's size rather than its flow."""
    subject = an_encoder(minimum=5)
    for index in range(20):
        subject.observe_trade(VENUE, SYMBOL, 100.0, 1.0, index * SECOND_NS)
    subject.observe_trade(VENUE, SYMBOL, 100.0, 1000.0, 20 * SECOND_NS)
    subject.observe_trade(VENUE, SYMBOL, 100.0, 1.0, 21 * SECOND_NS)
    states = subject.states_for(VENUE, SYMBOL, 5)
    assert states[-1].volume_quintile == 5, "the huge second is the top quintile here"


def test_the_price_sign_is_the_change_from_the_previous_second():
    subject = an_encoder(minimum=1)
    subject.observe_trade(VENUE, SYMBOL, 100.0, 1.0, 0)
    subject.observe_trade(VENUE, SYMBOL, 105.0, 1.0, SECOND_NS)
    subject.observe_trade(VENUE, SYMBOL, 95.0, 1.0, 2 * SECOND_NS)
    subject.close_open_second(VENUE, SYMBOL)
    states = subject.states_for(VENUE, SYMBOL, 5)
    assert [state.price_sign for state in states[-2:]] == [1, -1]


def test_every_state_maps_to_a_distinct_index():
    seen = set()
    for sign in (-1, 0, 1):
        for quintile in (1, 2, 3, 4, 5):
            state = OrderFlowState(VENUE, SYMBOL, 0, sign, quintile, 100.0, 1.0, 1)
            seen.add(state.index)
    assert len(seen) == STATE_COUNT
    assert min(seen) == 0 and max(seen) == STATE_COUNT - 1


def test_a_one_second_volume_window_is_refused():
    with pytest.raises(ValueError):
        OrderFlowStateEncoder(volume_window_seconds=1, minimum_volume_observations=1)


# ---- flow-entropy-meter -----------------------------------------------------

def a_meter(window=120, minimum=20):
    return FlowEntropyMeter(
        window_seconds=window, percentile_window=500, prior_entropy=0.8,
        minimum_observations=minimum,
    )


def a_state(sign, quintile, index):
    return OrderFlowState(VENUE, SYMBOL, index, sign, quintile, 100.0, 1.0, 1)


def pseudo_random_states(count, seed=12345, structured_fraction=0.0):
    states = []
    value = seed
    for index in range(count):
        value = (1103515245 * value + 12345) % (2 ** 31)
        if structured_fraction and value % 100 < structured_fraction * 100:
            sign, quintile = (1 if index % 2 else -1), 3
        else:
            sign, quintile = [-1, 0, 1][value % 3], (value // 3) % 5 + 1
        states.append(a_state(sign, quintile, index))
    return states


def test_structured_flow_has_lower_entropy_than_scattered_flow():
    """The paper's central claim: low entropy means informed traders left a footprint."""
    subject = a_meter()
    scattered = subject.measure(VENUE, SYMBOL, pseudo_random_states(400))
    structured = subject.measure(
        VENUE, SYMBOL, pseudo_random_states(400, seed=999, structured_fraction=0.8)
    )
    assert scattered.state == MEASURED and structured.state == MEASURED
    assert structured.entropy < scattered.entropy


def test_entropy_is_bounded_in_zero_to_one():
    subject = a_meter()
    reading = subject.measure(VENUE, SYMBOL, pseudo_random_states(400))
    assert 0.0 <= reading.entropy <= 1.0


def test_swapping_the_buy_and_sell_labels_leaves_the_entropy_unchanged():
    """Theorem 2, checked rather than assumed: direction is mathematically unrecoverable."""
    subject = a_meter()
    for states in (
        pseudo_random_states(200),
        pseudo_random_states(200, seed=777, structured_fraction=0.8),
    ):
        assert subject.is_invariant_under_label_swap(states[-120:])


def test_the_reading_carries_no_direction_and_says_so():
    subject = a_meter()
    reading = subject.measure(VENUE, SYMBOL, pseudo_random_states(400))
    assert reading.carries_a_direction is False
    assert "invariant under swapping" in reading.reason
    assert not hasattr(reading, "direction")


def test_a_window_shorter_than_the_specification_is_refused():
    subject = a_meter(window=120)
    assert subject.measure(VENUE, SYMBOL, pseudo_random_states(50)).state == ENTROPY_WINDOW_TOO_SHORT


def test_a_row_with_no_transitions_falls_back_to_uniform():
    """Renormalising would make the entropy measure the sample rather than the flow."""
    subject = a_meter()
    states = [a_state(1, 3, index) for index in range(200)]
    reading = subject.measure(VENUE, SYMBOL, states)
    assert reading.rows_with_no_transitions == 14, "one state seen, fourteen rows empty"


def test_a_percentile_appears_once_there_is_history():
    subject = a_meter(minimum=5)
    for seed in range(10):
        subject.measure(VENUE, SYMBOL, pseudo_random_states(200, seed=seed * 31 + 1))
    reading = subject.measure(VENUE, SYMBOL, pseudo_random_states(200, seed=555))
    assert reading.percentile is not None


def test_a_far_too_short_window_is_refused_at_construction():
    with pytest.raises(ValueError):
        FlowEntropyMeter(
            window_seconds=5, percentile_window=100, prior_entropy=0.8, minimum_observations=10
        )


# ---- entropy-magnitude-forecaster -------------------------------------------

class EntropyStub:
    def __init__(self, entropy=0.3, percentile=0.02, usable=True):
        self.venue_id, self.symbol = VENUE, SYMBOL
        self.entropy = entropy
        self.percentile = percentile
        self.is_usable = usable


def a_magnitude_forecaster(minimum=10, low_percentile=0.05):
    return EntropyMagnitudeForecaster(
        horizon_seconds=300.0, minimum_observations_per_quintile=minimum, outcome_window=500,
        prior_absolute_return=0.001, low_entropy_percentile=low_percentile,
    )


def test_the_relation_is_measured_here_not_inherited_from_the_paper():
    """2.17 and 2.89 are SPY's numbers over 36 days in one asset class."""
    subject = a_magnitude_forecaster()
    forecast = subject.forecast(EntropyStub())
    assert forecast.state == NOT_YET_MEASURED
    assert "is not measured yet" in forecast.reason
    assert "not inherited" in forecast.reason


def test_low_entropy_forecasts_a_larger_move_once_measured():
    subject = a_magnitude_forecaster(minimum=5)
    for _ in range(50):
        subject.observe_outcome(entropy_percentile=0.02, absolute_return=0.008)
        subject.observe_outcome(entropy_percentile=0.95, absolute_return=0.002)
    structured = subject.forecast(EntropyStub(percentile=0.02))
    calm = subject.forecast(EntropyStub(percentile=0.95))
    assert structured.expected_volatility > calm.expected_volatility


def test_the_forecast_carries_no_direction():
    subject = a_magnitude_forecaster(minimum=5)
    for _ in range(50):
        subject.observe_outcome(0.02, 0.008)
        subject.observe_outcome(0.5, 0.003)
    forecast = subject.forecast(EntropyStub())
    assert isinstance(forecast, VolatilityForecast)
    assert not hasattr(forecast, "direction")
    assert "carries no direction" in forecast.reason


def test_no_usable_entropy_produces_nothing():
    assert a_magnitude_forecaster().forecast(EntropyStub(usable=False)).state == NO_ENTROPY


def test_the_structured_condition_is_the_low_tail():
    subject = a_magnitude_forecaster(minimum=5, low_percentile=0.05)
    for _ in range(50):
        subject.observe_outcome(0.02, 0.008)
        subject.observe_outcome(0.5, 0.003)
    assert "Structured condition" in subject.forecast(EntropyStub(percentile=0.02)).reason
    assert "Structured condition" not in subject.forecast(EntropyStub(percentile=0.5)).reason


def test_a_threshold_at_the_median_is_not_a_tail():
    with pytest.raises(ValueError):
        EntropyMagnitudeForecaster(
            horizon_seconds=300.0, minimum_observations_per_quintile=10, outcome_window=500,
            prior_absolute_return=0.001, low_entropy_percentile=0.5,
        )


def test_there_are_five_quintiles_as_the_paper_reports():
    assert QUINTILE_COUNT == 5


# ---- funding-rate-forecaster ------------------------------------------------

def a_funding_forecaster(window=100, minimum=10, clock=None):
    forecaster = FundingRateForecaster(
        premium_window_observations=window, minimum_observations=minimum
    )
    if clock is not None:
        forecaster._now_ns = clock
    return forecaster


def binance_parameters():
    return FundingParameters(
        venue_id=VENUE, interval_seconds=28800.0, cap=0.0075,
        interest_rate_per_interval=0.0001, premium_clamp=0.0005,
        averaging_window_seconds=28800.0,
    )


def test_an_unknown_venue_formula_produces_no_forecast():
    """Interval, cap and interest rate differ between venues and change."""
    assert a_funding_forecaster().forecast(VENUE, SYMBOL).state == NO_VENUE_PARAMETERS


def test_funding_is_computed_from_the_premium_index_not_fitted_to_past_rates():
    subject = a_funding_forecaster(minimum=5)
    subject.observe_venue_parameters(binance_parameters())
    for _ in range(20):
        subject.observe_premium(VENUE, SYMBOL, mark_price=100.05, index_price=100.0)
    forecast = subject.forecast(VENUE, SYMBOL)
    assert forecast.state == FORECAST
    assert forecast.premium_average == pytest.approx(0.0005)
    assert forecast.predicted_rate > 0


def test_the_rate_is_capped_at_the_venues_cap():
    subject = a_funding_forecaster(minimum=5)
    subject.observe_venue_parameters(binance_parameters())
    for _ in range(20):
        subject.observe_premium(VENUE, SYMBOL, mark_price=200.0, index_price=100.0)
    forecast = subject.forecast(VENUE, SYMBOL)
    assert forecast.was_capped
    assert forecast.predicted_rate == pytest.approx(0.0075)


def test_the_forecast_says_how_much_of_it_is_already_determined():
    """An hour before settlement is a different object from a minute before."""
    clock = Clock()
    subject = a_funding_forecaster(minimum=5, clock=clock)
    subject.observe_venue_parameters(binance_parameters())
    for _ in range(20):
        subject.observe_premium(VENUE, SYMBOL, 100.01, 100.0)
    subject.observe_next_settlement(VENUE, SYMBOL, clock() + int(1000 * 1e9))
    forecast = subject.forecast(VENUE, SYMBOL)
    assert forecast.fraction_of_window_elapsed > 0.9
    assert forecast.is_nearly_settled


def test_no_premium_observations_produces_no_forecast():
    subject = a_funding_forecaster(minimum=10)
    subject.observe_venue_parameters(binance_parameters())
    assert subject.forecast(VENUE, SYMBOL).state == NO_PREMIUM_OBSERVATIONS


def test_the_forecast_is_a_cost_not_a_direction():
    subject = a_funding_forecaster(minimum=5)
    subject.observe_venue_parameters(binance_parameters())
    for _ in range(20):
        subject.observe_premium(VENUE, SYMBOL, 100.01, 100.0)
    assert "not a claim about direction" in subject.forecast(VENUE, SYMBOL).reason


# ---- liquidation-cluster-mapper ---------------------------------------------

def a_liquidation_mapper(bands=(5.0, 10.0, 20.0), minimum=10, buckets=100):
    return LiquidationClusterMapper(
        leverage_bands=bands, prior_band_share=0.33, prior_weight=4.0,
        half_life_observations=500, minimum_observations=minimum,
        volume_profile_buckets=buckets,
    )


def binance_tiers():
    return (
        MarginTier(notional_floor=0.0, maintenance_margin_rate=0.004, maximum_leverage=125.0),
        MarginTier(notional_floor=50_000.0, maintenance_margin_rate=0.005, maximum_leverage=100.0),
    )


def test_a_liquidation_price_uses_the_venues_schedule_not_a_rule_of_thumb():
    """At 20x the liquidation is not 5% away, and the difference is what a cascade trades."""
    subject = a_liquidation_mapper()
    subject.observe_margin_schedule(VENUE, binance_tiers())
    price = subject.liquidation_price(VENUE, entry=100.0, leverage=20.0, side="long")
    assert price == pytest.approx(100.0 * (1 - (0.05 - 0.004)))
    assert price > 95.0


def test_no_margin_schedule_produces_no_map():
    subject = a_liquidation_mapper()
    subject.observe_open_interest(VENUE, SYMBOL, 1_000_000.0)
    assert subject.map(VENUE, SYMBOL, 100.0).state == NO_MARGIN_SCHEDULE


def test_no_open_interest_produces_no_map():
    subject = a_liquidation_mapper()
    subject.observe_margin_schedule(VENUE, binance_tiers())
    assert subject.map(VENUE, SYMBOL, 100.0).state == NO_OPEN_INTEREST


def test_no_volume_profile_produces_no_map():
    """A single assumed entry price would put every cluster in one place."""
    subject = a_liquidation_mapper()
    subject.observe_margin_schedule(VENUE, binance_tiers())
    subject.observe_open_interest(VENUE, SYMBOL, 1_000_000.0)
    assert subject.map(VENUE, SYMBOL, 100.0).state == NO_VOLUME_PROFILE


def test_clusters_are_placed_where_volume_actually_traded():
    subject = a_liquidation_mapper()
    subject.observe_margin_schedule(VENUE, binance_tiers())
    subject.observe_open_interest(VENUE, SYMBOL, 1_000_000.0)
    subject.observe_traded_volume(VENUE, SYMBOL, 100.0, 500_000.0)
    subject.observe_traded_volume(VENUE, SYMBOL, 110.0, 500_000.0)
    result = subject.map(VENUE, SYMBOL, 100.0)
    assert result.state == MAPPED
    assert result.clusters
    assert result.largest_cluster is not None


def test_each_cluster_carries_the_confidence_of_the_band_mix():
    subject = a_liquidation_mapper(minimum=1000)
    subject.observe_margin_schedule(VENUE, binance_tiers())
    subject.observe_open_interest(VENUE, SYMBOL, 1_000_000.0)
    subject.observe_traded_volume(VENUE, SYMBOL, 100.0, 1_000_000.0)
    result = subject.map(VENUE, SYMBOL, 100.0)
    assert all(cluster.is_measured is False for cluster in result.clusters)


def test_the_map_produces_no_direction():
    subject = a_liquidation_mapper()
    subject.observe_margin_schedule(VENUE, binance_tiers())
    subject.observe_open_interest(VENUE, SYMBOL, 1_000_000.0)
    subject.observe_traded_volume(VENUE, SYMBOL, 100.0, 1_000_000.0)
    result = subject.map(VENUE, SYMBOL, 100.0)
    assert not hasattr(result, "direction")
    assert "estimates" in result.reason


def test_a_one_times_leverage_band_is_refused():
    with pytest.raises(ValueError):
        LiquidationClusterMapper(
            leverage_bands=(1.0,), prior_band_share=0.5, prior_weight=4.0,
            half_life_observations=500, minimum_observations=10, volume_profile_buckets=100,
        )


# ---- forecast-ensembler -----------------------------------------------------

def an_ensembler(minimum_trust=0.5, minimum_members=1):
    return ForecastEnsembler(minimum_trust=minimum_trust, minimum_members=minimum_members)


def a_price_forecast(forecaster, expected, horizon=600.0):
    return PriceForecast(
        venue_id=VENUE, symbol=SYMBOL, forecaster=forecaster, model_name=forecaster,
        state=AVAILABLE, expected_return=expected, lower_return=expected - 0.01,
        upper_return=expected + 0.01, horizon_seconds=horizon, window_length=100,
        reason="f", forecast_at_ns=0,
    )


def a_volatility_forecast(forecaster, volatility, horizon=600.0):
    return VolatilityForecast(
        venue_id=VENUE, symbol=SYMBOL, forecaster=forecaster,
        expected_volatility=volatility, horizon_seconds=horizon, state=AVAILABLE,
        inputs_used=(), reason="v", forecast_at_ns=0,
    )


def test_a_forecaster_with_no_measured_record_contributes_nothing():
    """Including it 'a little' is how an unvalidated model reaches the decision."""
    subject = an_ensembler()
    result = subject.combine(VENUE, SYMBOL, [a_price_forecast("unmeasured", 0.05)], [])
    assert result.state == NO_TRUSTED_MEMBER
    assert result.excluded["unmeasured"] == EXCLUDED_UNMEASURED


def test_members_are_weighted_by_measured_accuracy_never_equally():
    subject = an_ensembler()
    subject.observe_trust("accurate", 0.9, True)
    subject.observe_trust("poor", 0.51, True)
    result = subject.combine(
        VENUE, SYMBOL,
        [a_price_forecast("accurate", 0.10), a_price_forecast("poor", 0.0)], [],
    )
    assert result.expected_return > 0.05, "the accurate member dominates"


def test_a_flagged_forecaster_is_excluded_whatever_its_record():
    """Out-of-distribution is not a discount: its accuracy was measured elsewhere."""
    subject = an_ensembler()
    subject.observe_trust("kronos", 0.95, True)
    subject.observe_out_of_distribution_flag("kronos", VENUE, SYMBOL, True)
    result = subject.combine(VENUE, SYMBOL, [a_price_forecast("kronos", 0.1)], [])
    assert result.excluded["kronos"] == EXCLUDED_FLAGGED


def test_disagreement_is_reported_rather_than_averaged_away():
    """A combined number the members do not hold is not a better estimate."""
    subject = an_ensembler()
    subject.observe_trust("a", 0.7, True)
    subject.observe_trust("b", 0.7, True)
    result = subject.combine(
        VENUE, SYMBOL, [a_price_forecast("a", -0.01), a_price_forecast("b", 0.05)], []
    )
    assert result.disagreement == pytest.approx(0.06)
    assert "disagree" in result.reason


def test_volatility_combines_in_log_space():
    """An arithmetic mean is dominated by whichever member is most alarmed."""
    subject = an_ensembler()
    subject.observe_trust("calm", 0.7, True)
    subject.observe_trust("alarmed", 0.7, True)
    result = subject.combine(
        VENUE, SYMBOL, [],
        [a_volatility_forecast("calm", 0.01), a_volatility_forecast("alarmed", 1.0)],
    )
    assert result.expected_volatility == pytest.approx(math.sqrt(0.01 * 1.0))
    assert result.expected_volatility < (0.01 + 1.0) / 2


def test_nothing_usable_produces_nothing():
    subject = an_ensembler()
    assert subject.combine(VENUE, SYMBOL, [], []).state == NOTHING_USABLE


def test_an_ensemble_of_nothing_is_refused_at_construction():
    with pytest.raises(ValueError):
        ForecastEnsembler(minimum_trust=0.5, minimum_members=0)


# ---- model-drift-monitor ----------------------------------------------------

def a_drift_monitor(established=50, recent=10, minimum=20, noise=2.0, forgetting=0.7,
                    realert=3600.0, clock=None):
    monitor = ModelDriftMonitor(
        established_window=established, recent_window=recent, minimum_established=minimum,
        noise_multiple=noise, forgetting_threshold=forgetting, realert_after_seconds=realert,
    )
    if clock is not None:
        monitor._now_ns = clock
    return monitor


def feed_accuracy(monitor, values, forecaster="kronos-forecaster", model="base"):
    for value in values:
        monitor.observe_accuracy(an_accuracy(model, value))


def test_a_model_that_was_always_mediocre_has_not_drifted():
    """A fixed threshold would call this drift, and be wrong."""
    subject = a_drift_monitor(minimum=20)
    feed_accuracy(subject, [0.51] * 50)
    assert subject.check("kronos-forecaster", "base").state == NO_DRIFT


def test_a_real_fall_against_this_models_own_record_is_drift():
    subject = a_drift_monitor(established=50, recent=10, minimum=20, noise=1.0)
    feed_accuracy(subject, [0.70] * 40 + [0.40] * 10)
    alert = subject.check("kronos-forecaster", "base")
    assert alert.state == ACCURACY_FELL
    assert alert.has_drifted
    assert alert.recommended_action in (RETRAIN, SWAP_CHAMPION)


def test_a_small_sample_does_not_fire_on_noise():
    """Without the bound the monitor fires constantly and everyone ignores it."""
    subject = a_drift_monitor(established=30, recent=5, minimum=20, noise=3.0)
    feed_accuracy(subject, [0.6] * 25 + [0.52] * 5)
    assert subject.check("kronos-forecaster", "base").state == NO_DRIFT


def test_forgetting_is_drift_whatever_the_accuracy_says():
    """It arrives before the accuracy does."""
    subject = a_drift_monitor(forgetting=0.7)
    feed_accuracy(subject, [0.7] * 50)
    subject.observe_forgetting_report("kronos-forecaster", "base", 0.3)
    alert = subject.check("kronos-forecaster", "base")
    assert alert.state == MODEL_IS_FORGETTING
    assert alert.recommended_action == RETRAIN


def test_an_alert_does_not_repeat_every_tick():
    """Retraining takes longer than a tick."""
    clock = Clock()
    subject = a_drift_monitor(recent=10, minimum=20, noise=1.0, realert=3600.0, clock=clock)
    feed_accuracy(subject, [0.70] * 40 + [0.40] * 10)
    assert subject.check("kronos-forecaster", "base").has_drifted
    assert subject.check("kronos-forecaster", "base").has_drifted is False
    assert subject.standing.alerts_suppressed_as_repeats == 1
    clock.advance_seconds(3601)
    assert subject.check("kronos-forecaster", "base").has_drifted


def test_too_little_history_says_so():
    subject = a_drift_monitor(minimum=100)
    feed_accuracy(subject, [0.6] * 10)
    assert subject.check("kronos-forecaster", "base").state == NOT_ENOUGH_HISTORY


def test_a_monitor_with_no_noise_bound_is_refused():
    with pytest.raises(ValueError):
        ModelDriftMonitor(
            established_window=50, recent_window=10, minimum_established=20,
            noise_multiple=0.0, forgetting_threshold=0.7, realert_after_seconds=60.0,
        )


# ---- forecast-distribution-gate ---------------------------------------------

def a_distribution_gate(threshold=3.0, volatility_threshold=3.0, headroom=1.5):
    return ForecastDistributionGate(
        deviation_threshold=threshold, volatility_deviation_threshold=volatility_threshold,
        forecast_headroom=headroom,
    )


def training_statistics(model="base", mean_return=0.0, return_deviation=0.001,
                        mean_volatility=0.002, volatility_deviation=0.0005, largest=0.05):
    return TrainingStatistics(
        model_name=model, symbols=(SYMBOL,), mean_return=mean_return,
        return_deviation=return_deviation, mean_volatility=mean_volatility,
        volatility_deviation=volatility_deviation, largest_absolute_move=largest, windows=1000,
    )


def test_a_model_with_no_recorded_training_statistics_is_flagged_not_passed():
    """Defaulting to in-distribution makes the unknown case the easiest to trade."""
    subject = a_distribution_gate()
    flag = subject.check(a_window([100.0, 100.1, 100.2]), a_forecast())
    assert flag.state == NO_TRAINING_STATISTICS
    assert flag.is_out_of_distribution


def test_a_forecast_larger_than_any_move_in_training_is_the_cheapest_signal():
    subject = a_distribution_gate(headroom=1.5)
    subject.observe_training_statistics(training_statistics(largest=0.05))
    flag = subject.check(a_window([100.0, 100.1]), a_forecast(expected=0.50))
    assert flag.state == FORECAST_LARGER_THAN_ANY_SEEN
    assert "extrapolating" in flag.reason


def test_a_crash_is_out_of_distribution_for_a_model_finetuned_through_a_quiet_month():
    subject = a_distribution_gate(volatility_threshold=3.0)
    subject.observe_training_statistics(
        training_statistics(mean_volatility=0.001, volatility_deviation=0.0002)
    )
    violent = a_window([100.0, 90.0, 105.0, 85.0, 110.0])
    flag = subject.check(violent, a_forecast(expected=0.001))
    assert flag.state == VOLATILITY_UNLIKE_TRAINING
    assert flag.is_out_of_distribution


def test_an_ordinary_window_passes():
    subject = a_distribution_gate()
    subject.observe_training_statistics(
        training_statistics(mean_return=0.001, return_deviation=0.002,
                            mean_volatility=0.001, volatility_deviation=0.001)
    )
    ordinary = a_window([100.0, 100.1, 100.2, 100.3, 100.4])
    flag = subject.check(ordinary, a_forecast(expected=0.001))
    assert flag.state == IN_DISTRIBUTION
    assert flag.is_out_of_distribution is False


def test_a_window_far_from_the_training_mean_return_is_flagged():
    subject = a_distribution_gate(threshold=2.0, volatility_threshold=100.0)
    subject.observe_training_statistics(
        training_statistics(mean_return=0.0, return_deviation=0.0001)
    )
    drifting = a_window([100.0, 101.0, 102.0, 103.0, 104.0])
    assert subject.check(drifting, a_forecast(expected=0.001)).state == WINDOW_UNLIKE_TRAINING


def test_headroom_below_one_is_refused():
    with pytest.raises(ValueError):
        ForecastDistributionGate(
            deviation_threshold=3.0, volatility_deviation_threshold=3.0, forecast_headroom=0.5
        )
