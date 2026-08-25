"""A fine-tuned model carries the distribution it was trained through.

`forecast-distribution-gate` judges every live window against the training
distribution, and read it as `getattr(model, "training_statistics", None)` from a
`FinetunedModel` that had no such field. So no statistics ever arrived, every
forecast was flagged NO_TRAINING_STATISTICS, and the gate was right for a reason
that had nothing to do with the model.

Measured from the windows rather than declared, and carried on the model itself,
so an artefact and the distribution it is judged against cannot drift apart.
"""

from __future__ import annotations

import pytest

from parts.prediction.kronos_finetuner import FinetunedModel
from runtime.forecast_types import Candle, KlineWindow, measure_training_statistics


def window(closes, symbol="BTCUSDT"):
    candles = tuple(
        Candle(
            open_time_ns=index, open=close, high=close, low=close, close=close,
            volume=1.0, quote_volume=close, trades=1, is_closed=True,
        )
        for index, close in enumerate(closes)
    )
    return KlineWindow(
        venue_id="binance-usdm", symbol=symbol, interval="1m", candles=candles,
        length_requested=len(candles), gaps=(), built_at_ns=1,
    )


def test_the_model_carries_its_training_statistics():
    assert "training_statistics" in FinetunedModel.__dataclass_fields__


def test_statistics_are_measured_per_window_not_per_candle():
    """The gate compares one window's mean and volatility, so training must match."""
    calm = window((100.0, 100.1, 100.2, 100.3))
    wild = window((100.0, 104.0, 96.0, 103.0), symbol="ETHUSDT")
    measured = measure_training_statistics("kronos-finetuned-1", (calm, wild))

    assert measured.windows == 2
    assert measured.symbols == ("BTCUSDT", "ETHUSDT")
    assert measured.mean_volatility > 0
    assert measured.volatility_deviation > 0
    assert measured.largest_absolute_move == pytest.approx(max(
        abs(value) for value in wild.returns
    ))


def test_a_window_with_no_returns_does_not_become_a_zero_observation():
    """One candle has no return; counting it as a return of zero invents calm."""
    measured = measure_training_statistics("kronos-finetuned-1", (window((100.0,)),))
    assert measured.windows == 1
    assert measured.mean_return == 0.0
    assert measured.largest_absolute_move == 0.0


def test_the_gate_judges_a_window_against_what_the_model_saw():
    from parts.prediction.forecast_distribution_gate import ForecastDistributionGate

    gate = ForecastDistributionGate(
        deviation_threshold=3.0, volatility_deviation_threshold=3.0, forecast_headroom=2.0,
    )
    # Varied slightly: five identical windows have no spread at all, and a
    # deviation of zero makes every live window exactly zero deviations away.
    training = tuple(
        window((100.0, 100.0 + step / 100, 100.0, 100.0 + step / 50))
        for step in range(1, 6)
    )
    gate.observe_training_statistics(measure_training_statistics("kronos-finetuned-1", training))

    from runtime.forecast_types import PriceForecast

    crash = window((100.0, 90.0, 99.0, 85.0))
    forecast = PriceForecast(
        venue_id="binance-usdm", symbol="BTCUSDT", forecaster="kronos-forecaster",
        model_name="kronos-finetuned-1", state="forecast", expected_return=0.001,
        lower_return=-0.01, upper_return=0.01, horizon_seconds=60.0,
        window_length=4, reason="a test", forecast_at_ns=1,
    )
    flag = gate.check(crash, forecast)
    assert flag.is_out_of_distribution
    assert flag.worst_measure in ("volatility", "window")
