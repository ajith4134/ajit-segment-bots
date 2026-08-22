"""kronos-forecaster: the Kronos model over a candle window, or an honest refusal.

Kronos is a foundation model for K-line sequences: a tokenizer quantises OHLCV
into hierarchical discrete tokens and an autoregressive transformer continues the
sequence. This part runs it. It does not reimplement it, and it does not
approximate it when it is not there.

**The model arrives as a dependency, and its absence is a state.** The weights
are a third-party artefact that may or may not be on this machine, and the honest
answers differ:

- `AVAILABLE` -- weights loaded, an accelerator slot granted, a window long
  enough, and a forecast produced.
- `MODEL_NOT_LOADED` -- no weights here. Not a zero forecast, because zero reads
  as "no move expected", which is a confident claim made by the one component
  that could not run (Rule 8).
- `WINDOW_TOO_SHORT` -- fewer candles than the context the model was trained on.
  Padding would feed the model a series it never saw.
- `NO_ACCELERATOR` -- the governor did not grant a slot. That is the control
  plane doing its job, and a part that ran anyway would be reaching into it (T-2).

**The forecast is a distribution, not a point.** Kronos is autoregressive and
sampling it once gives one path; this part samples several and reports the median
with an interval, because a single sampled path presented as a forecast is a
draw from a distribution wearing the authority of an estimate.

**Champion and challenger, promoted by control.** Which finetuned model is live
is decided elsewhere and delivered as `champion-choice`, so a model that starts
underperforming can be swapped without this part deciding anything.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.forecast_types import (
    AVAILABLE, MODEL_NOT_LOADED, NO_ACCELERATOR, WINDOW_TOO_SHORT, PriceForecast,
    unavailable_forecast,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "kronos-forecaster"

PART_DECLARATION = PartDeclaration(
    part_id="kronos-forecaster",
    consumes=(
        "kline-window", "finetuned-model", "model-choice", "champion-choice", "accelerator-slot",
    ),
    produces=("price-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CHAMPION = "champion"
CHALLENGER = "challenger"


@dataclass(frozen=True)
class LoadedModel:
    """A Kronos model that is actually on this machine and can be called.

    `predict` takes the window's candles and a number of paths and returns that
    many continuations, each a sequence of closes. That is the whole contract:
    this part does not know whether it is talking to Kronos-mini on a CPU or
    Kronos-base on an accelerator, which is what makes the model a spare part.
    """

    name: str
    context_length: int
    predict: object
    device: str
    finetuned_on: str | None

    def continuations(self, candles, steps: int, paths: int) -> list:
        return self.predict(candles, steps, paths)


@dataclass
class ForecasterStanding:
    forecasts_requested: int = 0
    forecasts_produced: int = 0
    refused_no_model: int = 0
    refused_short_window: int = 0
    refused_no_accelerator: int = 0
    champion_swaps: int = 0
    paths_sampled: int = 0
    slowest_forecast_seconds: float = 0.0
    live_model: str = CHAMPION
    by_model: dict = field(default_factory=dict)


class KronosForecaster:
    """Runs whichever Kronos model is live, and says clearly when it cannot."""

    def __init__(
        self,
        forecast_steps: int,
        sample_paths: int,
        interval_quantile: float,
        interval_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if forecast_steps < 1:
            raise ValueError("a forecast over no steps predicts nothing")
        if sample_paths < 2:
            raise ValueError(
                "one sampled path is a draw from a distribution wearing the authority of an "
                "estimate; an interval needs at least two"
            )
        if not 0.5 < interval_quantile < 1.0:
            raise ValueError("the interval quantile is the upper tail and must be in (0.5, 1)")
        self._steps = forecast_steps
        self._paths = sample_paths
        self._quantile = interval_quantile
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._models: dict[str, LoadedModel] = {}
        self._live = CHAMPION
        self._accelerator_granted = True
        self.standing = ForecasterStanding()

    def load_model(self, role: str, model: LoadedModel) -> None:
        """Install real weights under a role. Nothing here creates a model."""
        if role not in (CHAMPION, CHALLENGER):
            raise ValueError(f"{role!r} is not a role this part holds")
        self._models[role] = model

    def apply_champion_choice(self, chosen: str) -> None:
        if chosen not in (CHAMPION, CHALLENGER):
            raise ValueError(f"{chosen!r} is not a role this part holds")
        if chosen != self._live:
            self.standing.champion_swaps += 1
        self._live = chosen
        self.standing.live_model = chosen

    def set_accelerator_slot(self, granted: bool) -> None:
        """The governor's decision, delivered rather than taken (T-2)."""
        self._accelerator_granted = granted

    @property
    def live_model(self) -> LoadedModel | None:
        return self._models.get(self._live)

    def forecast(self, window) -> PriceForecast:
        self.standing.forecasts_requested += 1
        model = self.live_model
        model_name = model.name if model is not None else "none"

        if model is None:
            self.standing.refused_no_model += 1
            return unavailable_forecast(
                window.venue_id, window.symbol, PART_ID, model_name, MODEL_NOT_LOADED,
                self._interval_seconds * self._steps, len(window.candles),
                "no Kronos weights are loaded on this machine, so no forecast exists. This "
                "reports as unavailable rather than as zero, because zero would read as 'no "
                "move expected' -- a confident claim from the one part that could not run",
                self._now_ns,
            )

        if not self._accelerator_granted:
            self.standing.refused_no_accelerator += 1
            return unavailable_forecast(
                window.venue_id, window.symbol, PART_ID, model_name, NO_ACCELERATOR,
                self._interval_seconds * self._steps, len(window.candles),
                "the governor granted no accelerator slot; running anyway would be a feature "
                "reaching into the control plane",
                self._now_ns,
            )

        if len(window.candles) < model.context_length:
            self.standing.refused_short_window += 1
            return unavailable_forecast(
                window.venue_id, window.symbol, PART_ID, model_name, WINDOW_TOO_SHORT,
                self._interval_seconds * self._steps, len(window.candles),
                f"{len(window.candles)} candle(s) against the {model.context_length} "
                f"{model.name} was trained on; padding would feed it a series it never saw",
                self._now_ns,
            )

        started = self._monotonic()
        paths = model.continuations(window.candles, self._steps, self._paths)
        duration = self._monotonic() - started
        self.standing.slowest_forecast_seconds = max(
            self.standing.slowest_forecast_seconds, duration
        )
        self.standing.paths_sampled += len(paths)

        last_close = window.candles[-1].close
        finals = [path[-1] for path in paths if path]
        if not finals or last_close <= 0:
            self.standing.refused_no_model += 1
            return unavailable_forecast(
                window.venue_id, window.symbol, PART_ID, model_name, MODEL_NOT_LOADED,
                self._interval_seconds * self._steps, len(window.candles),
                "the model returned no usable continuation",
                self._now_ns,
            )

        returns = sorted((final - last_close) / last_close for final in finals)
        expected = statistics.median(returns)
        lower = returns[max(0, int((1 - self._quantile) * len(returns)) - 1)]
        upper = returns[min(len(returns) - 1, int(self._quantile * len(returns)))]

        self.standing.forecasts_produced += 1
        self.standing.by_model[model.name] = self.standing.by_model.get(model.name, 0) + 1

        return PriceForecast(
            venue_id=window.venue_id,
            symbol=window.symbol,
            forecaster=PART_ID,
            model_name=model.name,
            state=AVAILABLE,
            expected_return=expected,
            lower_return=lower,
            upper_return=upper,
            horizon_seconds=self._interval_seconds * self._steps,
            window_length=len(window.candles),
            reason=(
                f"{model.name} on {model.device} over {len(window.candles)} candle(s), "
                f"{len(returns)} sampled path(s): median {expected:+.3%} with a "
                f"{self._quantile:.0%} interval of [{lower:+.3%}, {upper:+.3%}] over "
                f"{self._steps} step(s)"
                + (
                    f"; finetuned on {model.finetuned_on}"
                    if model.finetuned_on
                    else "; not finetuned, so this is the base model's view of this symbol"
                )
                + (
                    f". The window has {len(window.gaps)} gap(s), so the sequence it read is "
                    f"not consecutive"
                    if window.gaps
                    else ""
                )
            ),
            forecast_at_ns=self._now_ns(),
        )

    def release(self) -> None:
        """Drop the weights. T-3: an off part releases its memory, and these are large."""
        self._models.clear()


def describe_forecasting(forecaster: KronosForecaster) -> dict:
    model = forecaster.live_model
    return {
        "part_id": PART_ID,
        "live_role": forecaster.standing.live_model,
        "live_model": None if model is None else model.name,
        "model_is_loaded": model is not None,
        "forecasts_requested": forecaster.standing.forecasts_requested,
        "forecasts_produced": forecaster.standing.forecasts_produced,
        "refused_no_model_loaded": forecaster.standing.refused_no_model,
        "refused_window_too_short": forecaster.standing.refused_short_window,
        "refused_no_accelerator_slot": forecaster.standing.refused_no_accelerator,
        "paths_sampled": forecaster.standing.paths_sampled,
        "champion_swaps": forecaster.standing.champion_swaps,
        "slowest_forecast_seconds": forecaster.standing.slowest_forecast_seconds,
        "by_model": dict(sorted(forecaster.standing.by_model.items())),
    }


def run_kronos_forecaster(
    forecaster: KronosForecaster, control_socket, read_windows_and_choices, publish_forecasts,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        windows = read_windows_and_choices(forecaster)
        publish_forecasts(tuple(forecaster.forecast(window) for window in windows))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
