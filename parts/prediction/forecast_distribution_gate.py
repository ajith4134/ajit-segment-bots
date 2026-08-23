"""forecast-distribution-gate: whether the model is being asked about something it saw.

A forecaster's accuracy is measured over the inputs it was measured on. Hand it a
window unlike anything in its training data and its accuracy record does not
apply -- and it will not say so, because a transformer given an unfamiliar
sequence produces a confident continuation rather than an error.

This part is the check the model cannot do for itself. It compares the window it
is about to be given against the distribution the model was finetuned on:

- **Per-feature distance**, not a single joint one. One feature far outside its
  range is the case that matters, and a joint distance over twelve features
  averages it away.
- **The realised volatility of the window specifically.** A model finetuned
  through a quiet month and run through a crash is out of distribution in the
  only way that matters, even when every other feature looks ordinary.
- **The forecast itself.** A model predicting a move larger than anything in its
  training data is announcing that it has left the data, and that is the cheapest
  signal available -- it needs no window comparison at all.

**A window that cannot be judged is flagged, not passed.** A model asked about a
symbol whose training statistics were never recorded is exactly the case where
being wrong is most likely, and defaulting to "in distribution" makes the
unknown symbol the easiest one to trade.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forecast-distribution-gate"

PART_DECLARATION = PartDeclaration(
    part_id="forecast-distribution-gate",
    consumes=("kline-window", "finetuned-model", "price-forecast"),
    produces=("forecast-out-of-distribution-flag", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

IN_DISTRIBUTION = "in-distribution"
WINDOW_UNLIKE_TRAINING = "the-window-is-unlike-the-training-data"
VOLATILITY_UNLIKE_TRAINING = "the-window's-volatility-is-outside-what-this-model-was-trained-through"
FORECAST_LARGER_THAN_ANY_SEEN = "the-forecast-is-larger-than-any-move-in-the-training-data"
NO_TRAINING_STATISTICS = "no-training-statistics-were-recorded-for-this-model"


@dataclass(frozen=True)
class TrainingStatistics:
    """What a finetuned model was actually trained through. Recorded, never assumed."""

    model_name: str
    symbols: tuple
    mean_return: float
    return_deviation: float
    mean_volatility: float
    volatility_deviation: float
    largest_absolute_move: float
    windows: int


@dataclass(frozen=True)
class OutOfDistributionFlag:
    """Whether a model is being asked about something it saw, and what is unfamiliar."""

    venue_id: str
    symbol: str
    forecaster: str
    model_name: str
    state: str
    is_out_of_distribution: bool
    worst_measure: str | None
    worst_deviation: float | None
    reason: str
    flagged_at_ns: int


@dataclass
class GateStanding:
    checks: int = 0
    flagged: int = 0
    flagged_window: int = 0
    flagged_volatility: int = 0
    flagged_forecast: int = 0
    no_statistics: int = 0
    largest_deviation_seen: float | None = None


class ForecastDistributionGate:
    """Checks a window and a forecast against what the model was trained through."""

    def __init__(
        self,
        deviation_threshold: float,
        volatility_deviation_threshold: float,
        forecast_headroom: float,
        now_ns=time.time_ns,
    ) -> None:
        if deviation_threshold <= 0 or volatility_deviation_threshold <= 0:
            raise ValueError("a threshold of zero flags every window including the ordinary ones")
        if forecast_headroom < 1.0:
            raise ValueError(
                "headroom below one would flag forecasts smaller than the training data's "
                "largest move, which is every ordinary forecast"
            )
        self._threshold = deviation_threshold
        self._volatility_threshold = volatility_deviation_threshold
        self._headroom = forecast_headroom
        self._now_ns = now_ns
        self._statistics: dict[str, TrainingStatistics] = {}
        self.standing = GateStanding()

    def observe_training_statistics(self, statistics: TrainingStatistics) -> None:
        self._statistics[statistics.model_name] = statistics

    def check(self, window, forecast) -> OutOfDistributionFlag:
        self.standing.checks += 1
        statistics = self._statistics.get(forecast.model_name)

        if statistics is None:
            # Flagged, not passed. A model asked about a symbol whose training
            # statistics were never recorded is exactly where being wrong is
            # most likely, and defaulting to "in distribution" makes the unknown
            # case the easiest one to trade.
            self.standing.no_statistics += 1
            self.standing.flagged += 1
            return self._flag(
                window, forecast, NO_TRAINING_STATISTICS, True, None, None,
                f"no training statistics were recorded for {forecast.model_name}, so there is "
                f"nothing to judge this window against; an unjudgeable window is flagged "
                f"rather than waved through",
            )

        # The cheapest check first: a forecast larger than any move in training
        # is the model announcing it has left the data.
        if forecast.expected_return is not None and statistics.largest_absolute_move > 0:
            ratio = abs(forecast.expected_return) / statistics.largest_absolute_move
            if ratio > self._headroom:
                self.standing.flagged += 1
                self.standing.flagged_forecast += 1
                return self._flag(
                    window, forecast, FORECAST_LARGER_THAN_ANY_SEEN, True, "forecast", ratio,
                    f"the forecast of {forecast.expected_return:+.2%} is {ratio:.1f}x the "
                    f"largest move in {forecast.model_name}'s training data "
                    f"({statistics.largest_absolute_move:.2%}); a model predicting past its "
                    f"own data is extrapolating, and this needs no window comparison to see",
                )

        returns = window.returns
        if not returns:
            self.standing.flagged += 1
            return self._flag(
                window, forecast, WINDOW_UNLIKE_TRAINING, True, "window", None,
                "the window has no returns to compare against the training distribution",
            )

        mean_return = sum(returns) / len(returns)
        volatility = math.sqrt(
            sum((value - mean_return) ** 2 for value in returns) / max(1, len(returns) - 1)
        )

        return_deviation = (
            abs(mean_return - statistics.mean_return) / statistics.return_deviation
            if statistics.return_deviation > 0
            else 0.0
        )
        volatility_deviation = (
            abs(volatility - statistics.mean_volatility) / statistics.volatility_deviation
            if statistics.volatility_deviation > 0
            else 0.0
        )

        worst = max(return_deviation, volatility_deviation)
        if self.standing.largest_deviation_seen is None or worst > self.standing.largest_deviation_seen:
            self.standing.largest_deviation_seen = worst

        # Volatility separately, because a model finetuned through a quiet month
        # and run through a crash is out of distribution in the only way that
        # matters even when every other feature looks ordinary.
        if volatility_deviation > self._volatility_threshold:
            self.standing.flagged += 1
            self.standing.flagged_volatility += 1
            return self._flag(
                window, forecast, VOLATILITY_UNLIKE_TRAINING, True, "volatility",
                volatility_deviation,
                f"this window's realised volatility is {volatility:.4%} against the "
                f"{statistics.mean_volatility:.4%} {forecast.model_name} was trained through, "
                f"{volatility_deviation:.1f} deviations away and past the "
                f"{self._volatility_threshold:.1f} this gate allows",
            )

        if return_deviation > self._threshold:
            self.standing.flagged += 1
            self.standing.flagged_window += 1
            return self._flag(
                window, forecast, WINDOW_UNLIKE_TRAINING, True, "mean return", return_deviation,
                f"this window's mean return is {return_deviation:.1f} deviations from the "
                f"training distribution, past the {self._threshold:.1f} this gate allows",
            )

        return self._flag(
            window, forecast, IN_DISTRIBUTION, False, None, worst,
            f"the window sits {return_deviation:.1f} deviations from the training mean return "
            f"and {volatility_deviation:.1f} from its volatility, both inside what "
            f"{forecast.model_name} was trained through over {statistics.windows} window(s)",
        )

    def _flag(
        self, window, forecast, state, is_out, worst_measure, worst_deviation, reason
    ) -> OutOfDistributionFlag:
        return OutOfDistributionFlag(
            venue_id=window.venue_id,
            symbol=window.symbol,
            forecaster=forecast.forecaster,
            model_name=forecast.model_name,
            state=state,
            is_out_of_distribution=is_out,
            worst_measure=worst_measure,
            worst_deviation=worst_deviation,
            reason=reason,
            flagged_at_ns=self._now_ns(),
        )


def describe_distribution_gating(gate: ForecastDistributionGate) -> dict:
    return {
        "part_id": PART_ID,
        "checks": gate.standing.checks,
        "flagged_out_of_distribution": gate.standing.flagged,
        "flagged_on_the_window": gate.standing.flagged_window,
        "flagged_on_volatility": gate.standing.flagged_volatility,
        "flagged_on_the_forecast_itself": gate.standing.flagged_forecast,
        "flagged_for_no_training_statistics": gate.standing.no_statistics,
        "largest_deviation_seen": gate.standing.largest_deviation_seen,
        "models_with_training_statistics": sorted(gate._statistics),
    }


def run_forecast_distribution_gate(
    gate: ForecastDistributionGate, control_socket, read_windows_and_forecasts, publish_flags,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        pairs = read_windows_and_forecasts(gate)
        publish_flags(tuple(gate.check(window, forecast) for window, forecast in pairs))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A fine-tuned model carries the statistics of what it was trained on; a
    forecast is checked against the window it was made from and those
    statistics. No model has been fine-tuned in phase 1, so every forecast is
    flagged as from a model whose training is unknown -- which is the flag's
    own rule, not this part's invention.
    """
    from runtime.input_assembly import Batch, LatestByKey

    windows = LatestByKey(read=context.bus.reader("kline-window"), key_of=lambda w: (w.venue_id, w.symbol))
    models = Batch(read=context.bus.reader("finetuned-model"))
    forecasts = Batch(read=context.bus.reader("price-forecast"))
    publish_flags = context.bus.publisher_for("forecast-out-of-distribution-flag")
    gate = ForecastDistributionGate(
        deviation_threshold=context.number("forecast_deviation_threshold"),
        volatility_deviation_threshold=context.number("forecast_volatility_deviation_threshold"),
        forecast_headroom=context.number("forecast_headroom"),
    )

    def read_windows_and_forecasts(_gate):
        for model in models.payloads():
            statistics = getattr(model, "training_statistics", None)
            if statistics is not None:
                gate.observe_training_statistics(statistics)
        by_symbol = windows.mapping()
        pairs = []
        for forecast in forecasts.payloads():
            window = by_symbol.get((forecast.venue_id, forecast.symbol))
            if window is not None:
                pairs.append((window, forecast))
        return tuple(pairs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_flags(kept)

    return run_forecast_distribution_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_windows_and_forecasts=read_windows_and_forecasts,
        publish_flags=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
