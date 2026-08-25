"""What the prediction block passes between its parts, and what it refuses to claim.

Substrate, not a part. Sixteen prediction parts share these shapes and none may
import another (T-4).

Two things are structural here and both exist because a forecast is the easiest
thing in a trading system to believe without evidence:

**Every forecast carries its own horizon and its own uncertainty.** A number
without a horizon is not a prediction -- "price will be higher" is true of every
asset over a long enough window -- and a number without an interval cannot be
distinguished from a guess by anything downstream.

**A model that cannot run says so.** `AVAILABLE` and `MODEL_NOT_LOADED` are
distinct states, and nothing anywhere may treat the second as a neutral forecast.
The Kronos parts are real integrations against real weights; when the weights are
not on this machine they report that, because a forecaster that returned zero
when it could not run would put a confident "no move expected" into every
decision (Rule 8).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate

# What a forecaster can be. Every one of these is a real state something
# downstream must handle differently, which is why none of them is a number.
AVAILABLE = "available"
MODEL_NOT_LOADED = "no-model-weights-are-loaded-on-this-machine"
WINDOW_TOO_SHORT = "the-candle-window-is-shorter-than-the-model-needs"
OUT_OF_DISTRIBUTION = "the-window-does-not-resemble-the-training-data"
NO_ACCELERATOR = "no-accelerator-slot-was-granted"


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar, as the venue reported it."""

    open_time_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    is_closed: bool

    @property
    def range_fraction(self) -> float:
        return (self.high - self.low) / self.close if self.close else 0.0

    @property
    def body_fraction(self) -> float:
        return (self.close - self.open) / self.open if self.open else 0.0


@dataclass(frozen=True)
class KlineWindow:
    """A fixed-length run of candles, shaped for whatever model reads it.

    `gaps` is not diagnostic. A window with a hole in it is a different series
    from one without, and a model handed the shorter series as though it were
    continuous learns that time moves at whatever rate the feed happened to
    deliver.
    """

    venue_id: str
    symbol: str
    interval: str
    candles: tuple
    length_requested: int
    gaps: tuple
    built_at_ns: int

    @property
    def is_complete(self) -> bool:
        return len(self.candles) == self.length_requested and not self.gaps

    @property
    def closes(self) -> tuple:
        return tuple(candle.close for candle in self.candles)

    @property
    def returns(self) -> tuple:
        closes = self.closes
        return tuple(
            (later - earlier) / earlier
            for earlier, later in zip(closes, closes[1:])
            if earlier
        )


@dataclass(frozen=True)
class PriceForecast:
    """What a model expects price to do, over a stated horizon, with an interval.

    `expected_return` is a fraction of the last close, never a price: a price
    forecast is unusable by anything that trades a different symbol, and a
    fraction transfers.
    """

    venue_id: str
    symbol: str
    forecaster: str
    model_name: str
    state: str
    expected_return: float | None
    lower_return: float | None
    upper_return: float | None
    horizon_seconds: float
    window_length: int
    reason: str
    forecast_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == AVAILABLE and self.expected_return is not None

    @property
    def interval_width(self) -> float | None:
        if self.lower_return is None or self.upper_return is None:
            return None
        return self.upper_return - self.lower_return

    @property
    def direction(self) -> int:
        if self.expected_return is None or self.expected_return == 0:
            return 0
        return 1 if self.expected_return > 0 else -1


@dataclass(frozen=True)
class VolatilityForecast:
    """How much a symbol is expected to move, without saying which way.

    Deliberately directionless. Several parts produce this from different
    evidence -- realised volatility, order-flow entropy, the implied surface --
    and a volatility estimate that carried a direction would be a price forecast
    wearing the wrong type.
    """

    venue_id: str
    symbol: str
    forecaster: str
    expected_volatility: float | None
    horizon_seconds: float
    state: str
    inputs_used: tuple
    reason: str
    forecast_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == AVAILABLE and self.expected_volatility is not None


@dataclass(frozen=True)
class ForecastAccuracy:
    """How a forecaster has actually done, against what it actually predicted.

    Direction and magnitude are kept apart because they fail separately: a model
    that calls direction well and overshoots every magnitude is useful for a bot
    and useless for sizing, and one number over both hides which.
    """

    forecaster: str
    model_name: str
    venue_id: str
    symbol: str
    horizon_seconds: float
    directional_accuracy: Estimate
    mean_absolute_error: float | None
    interval_coverage: Estimate
    forecasts_scored: int
    reason: str
    scored_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.directional_accuracy.is_fitted

    @property
    def beats_a_coin_flip(self) -> bool:
        return self.is_measured and self.directional_accuracy.value > 0.5


@dataclass(frozen=True)
class EnsembleForecast:
    """Several forecasters combined by how well each has done, and what they disagree on."""

    venue_id: str
    symbol: str
    expected_return: float | None
    expected_volatility: float | None
    horizon_seconds: float
    contributors: dict
    disagreement: float | None
    excluded: dict
    state: str
    reason: str
    combined_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == AVAILABLE and self.expected_return is not None


def annualise(volatility: float, horizon_seconds: float, seconds_per_year: float) -> float:
    """Scale a volatility from one horizon to a year, by the square root of time.

    Square root because independent increments add in variance, not in
    deviation. Multiplying a daily number by 365 -- which is the mistake this
    function exists to prevent -- overstates a year's volatility nineteenfold.
    """
    if horizon_seconds <= 0:
        raise ValueError("a volatility over no time is not a volatility")
    return volatility * math.sqrt(seconds_per_year / horizon_seconds)


def unavailable_forecast(
    venue_id: str,
    symbol: str,
    forecaster: str,
    model_name: str,
    state: str,
    horizon_seconds: float,
    window_length: int,
    reason: str,
    now_ns=time.time_ns,
) -> PriceForecast:
    """A forecast that could not be made, said out loud rather than returned as zero.

    Zero would be read as "no move expected", which is a confident claim, and it
    would be made by exactly the forecaster that could not run.
    """
    return PriceForecast(
        venue_id=venue_id,
        symbol=symbol,
        forecaster=forecaster,
        model_name=model_name,
        state=state,
        expected_return=None,
        lower_return=None,
        upper_return=None,
        horizon_seconds=horizon_seconds,
        window_length=window_length,
        reason=reason,
        forecast_at_ns=now_ns(),
    )


@dataclass(frozen=True)
class TrainingStatistics:
    """What a finetuned model was actually trained through. Measured, never assumed.

    Lives here rather than beside its reader because both sides need it: the
    finetuner is the only part that saw the training windows, and the
    distribution gate is the part that judges a live window against them. It was
    declared inside `forecast-distribution-gate` until 2026-08-25 and nothing ever
    produced one, so the gate flagged every forecast as unjudgeable -- which was
    the honest answer to a question nobody was answering.
    """

    model_name: str
    symbols: tuple
    mean_return: float
    return_deviation: float
    mean_volatility: float
    volatility_deviation: float
    largest_absolute_move: float
    windows: int


def deviation_of(values: tuple, mean: float) -> float:
    """The sample standard deviation, or zero when one value cannot have one."""
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def measure_training_statistics(model_name: str, windows: tuple) -> TrainingStatistics:
    """The distribution a set of training windows actually covers.

    Per window rather than per candle, because that is the shape the gate compares
    against: it takes one live window's mean return and its realised volatility,
    and asks how many deviations from training each one is. Statistics gathered
    per candle would answer a different question and read as though they answered
    this one.
    """
    means: list[float] = []
    volatilities: list[float] = []
    largest = 0.0
    symbols: set[str] = set()

    for window in windows:
        symbols.add(window.symbol)
        returns = window.returns
        if not returns:
            continue
        mean = sum(returns) / len(returns)
        means.append(mean)
        volatilities.append(deviation_of(tuple(returns), mean))
        largest = max(largest, max(abs(value) for value in returns))

    mean_return = sum(means) / len(means) if means else 0.0
    mean_volatility = sum(volatilities) / len(volatilities) if volatilities else 0.0
    return TrainingStatistics(
        model_name=model_name,
        symbols=tuple(sorted(symbols)),
        mean_return=mean_return,
        return_deviation=deviation_of(tuple(means), mean_return),
        mean_volatility=mean_volatility,
        volatility_deviation=deviation_of(tuple(volatilities), mean_volatility),
        largest_absolute_move=largest,
        windows=len(windows),
    )
