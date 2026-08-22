"""realised-vol-regressor: how much a symbol will move, learned from what moved it.

Volatility is the one quantity in trading that is genuinely forecastable --
returns are close to unpredictable and their magnitude is not, because volatility
clusters. That is what this part exploits, and it is why it is a regression rather
than a classification: sizing needs a number, not a direction.

**HAR-style features, learned online.** The heterogeneous autoregressive form --
volatility over a short, a medium and a long window together -- captures
clustering better than any single window because different participants act on
different horizons, and it does so with three coefficients that can be read. A
regressor whose coefficients cannot be read is a regressor nobody can review.

**It predicts the logarithm of volatility, not volatility.** Volatility is
bounded below by zero and its distribution is heavily right-skewed; a linear
model on the raw quantity predicts negative volatility on quiet days and is
dominated by a handful of violent ones. Working in logs makes the errors
symmetric, which is the assumption least squares actually makes.

**It reports what it cannot support.** Below enough observations, or with the
features it needs missing, it produces no forecast rather than a number from two
coefficients -- the same rule as everything else here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.forecast_types import AVAILABLE, VolatilityForecast
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "realised-vol-regressor"

PART_DECLARATION = PartDeclaration(
    part_id="realised-vol-regressor",
    consumes=("vol-feature-set",),
    produces=("volatility-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NOT_ENOUGH_TRAINING = "too-few-observations-to-support-a-forecast"
FEATURES_MISSING = "the-features-this-regressor-needs-were-not-measured"

# The features this regressor is built on. Named rather than "whatever arrived",
# so a feature set that silently stops carrying one is a refusal rather than a
# quietly worse forecast.
REQUIRED_FEATURES = ("close_to_close_short", "close_to_close_long")
OPTIONAL_FEATURES = (
    "parkinson", "garman_klass", "volatility_ratio_short_to_long",
    "implied_over_realised", "largest_gap_fraction",
)


@dataclass
class RegressorStanding:
    forecasts_made: int = 0
    forecasts_produced: int = 0
    refused_untrained: int = 0
    refused_features_missing: int = 0
    observations: int = 0
    mean_absolute_error: float = 0.0
    features_used: dict = field(default_factory=dict)


class RealisedVolRegressor:
    """Online least squares on log volatility, with coefficients that can be read."""

    def __init__(
        self,
        learning_rate: float,
        l2_regularisation: float,
        feature_half_life_observations: float,
        minimum_feature_observations: int,
        minimum_training_observations: int,
        horizon_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < learning_rate <= 1.0:
            raise ValueError("a learning rate outside (0, 1] either does not learn or diverges")
        if horizon_seconds <= 0:
            raise ValueError("a volatility forecast over no time is not a forecast")
        self._learning_rate = learning_rate
        self._l2 = l2_regularisation
        self._half_life = feature_half_life_observations
        self._minimum_feature = minimum_feature_observations
        self._minimum_training = minimum_training_observations
        self._horizon = horizon_seconds
        self._now_ns = now_ns
        self._weights: dict[str, float] = {}
        self._moments: dict[str, RunningMoments] = {}
        self._intercept: float = 0.0
        self._observations = 0
        self._absolute_error_total = 0.0
        self.standing = RegressorStanding()

    @property
    def is_fitted(self) -> bool:
        return self._observations >= self._minimum_training

    @property
    def coefficients(self) -> dict:
        """The learned weights, in standardised feature units. Readable on purpose."""
        return dict(self._weights)

    def train(self, feature_set, realised_volatility: float) -> float:
        """One observation: the features, and the volatility that followed them.

        Trained on the log, because volatility is bounded below by zero and
        right-skewed -- a linear model on the raw quantity predicts negative
        volatility on quiet days and is dominated by a few violent ones.
        """
        if realised_volatility <= 0:
            raise ValueError(
                "a realised volatility of zero or less cannot be logged; a flat window is "
                "not an observation of zero volatility, it is an absence of one"
            )
        target = math.log(realised_volatility)

        for name, value in feature_set.features.items():
            self._moment_for(name).observe(self._to_log_space(name, value))

        standardised = self._standardise(feature_set)
        predicted = self._intercept + sum(
            self._weights.get(name, 0.0) * value for name, value in standardised.items()
        )
        error = target - predicted
        step = self._learning_rate * error

        if self._l2:
            decay = 1.0 - self._learning_rate * self._l2
            for name in self._weights:
                self._weights[name] *= decay

        for name, value in standardised.items():
            self._weights[name] = self._weights.get(name, 0.0) + step * value
        self._intercept += step

        self._observations += 1
        self._absolute_error_total += abs(error)
        self.standing.observations = self._observations
        self.standing.mean_absolute_error = self._absolute_error_total / self._observations
        return error

    def forecast(self, feature_set) -> VolatilityForecast:
        self.standing.forecasts_made += 1

        absent = [name for name in REQUIRED_FEATURES if name not in feature_set.features]
        if absent:
            self.standing.refused_features_missing += 1
            return self._forecast(
                feature_set, None, FEATURES_MISSING, (),
                f"this regressor is built on {', '.join(REQUIRED_FEATURES)} and "
                f"{', '.join(absent)} was not measured; a forecast without it would be from a "
                f"different model than the one that was trained",
            )

        if not self.is_fitted:
            self.standing.refused_untrained += 1
            return self._forecast(
                feature_set, None, NOT_ENOUGH_TRAINING, (),
                f"{self._observations} observation(s) of the {self._minimum_training} needed; "
                f"a number from two coefficients is not a forecast",
            )

        standardised = self._standardise(feature_set)
        if not standardised:
            self.standing.refused_features_missing += 1
            return self._forecast(
                feature_set, None, FEATURES_MISSING, (),
                "no feature has enough history to standardise, so nothing can be predicted from",
            )

        log_volatility = self._intercept + sum(
            self._weights.get(name, 0.0) * value for name, value in standardised.items()
        )
        for name in standardised:
            self.standing.features_used[name] = self.standing.features_used.get(name, 0) + 1

        self.standing.forecasts_produced += 1
        strongest = max(
            standardised,
            key=lambda name: abs(self._weights.get(name, 0.0) * standardised[name]),
        )
        return self._forecast(
            feature_set, math.exp(log_volatility), AVAILABLE, tuple(sorted(standardised)),
            f"{math.exp(log_volatility):.4%} expected over {self._horizon:.0f}s from "
            f"{len(standardised)} feature(s) over {self._observations} trained observation(s); "
            f"{strongest} carries the most weight. Predicted in logs, because volatility is "
            f"bounded below by zero and a linear model on the raw quantity predicts negative "
            f"volatility on quiet days",
        )

    def _to_log_space(self, name: str, value: float) -> float:
        """Volatility-like features are logged; ratios and gaps are not.

        A ratio is already scale-free and logging it a second time compresses
        exactly the range that carries the signal.
        """
        if name.startswith("close_to_close") or name in ("parkinson", "garman_klass"):
            return math.log(value) if value > 0 else math.log(1e-12)
        return value

    def _standardise(self, feature_set) -> dict:
        standardised = {}
        for name in REQUIRED_FEATURES + OPTIONAL_FEATURES:
            value = feature_set.features.get(name)
            if value is None:
                continue
            moments = self._moments.get(name)
            if moments is None:
                continue
            scaled = moments.standardise(self._to_log_space(name, value), self._minimum_feature)
            if scaled is not None:
                standardised[name] = scaled
        return standardised

    def _moment_for(self, name: str) -> RunningMoments:
        moments = self._moments.get(name)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            self._moments[name] = moments
        return moments

    def _forecast(self, feature_set, volatility, state, inputs, reason) -> VolatilityForecast:
        return VolatilityForecast(
            venue_id=feature_set.venue_id,
            symbol=feature_set.symbol,
            forecaster=PART_ID,
            expected_volatility=volatility,
            horizon_seconds=self._horizon,
            state=state,
            inputs_used=inputs,
            reason=reason,
            forecast_at_ns=self._now_ns(),
        )


def describe_vol_regression(regressor: RealisedVolRegressor) -> dict:
    return {
        "part_id": PART_ID,
        "is_fitted": regressor.is_fitted,
        "observations": regressor.standing.observations,
        "forecasts_made": regressor.standing.forecasts_made,
        "forecasts_produced": regressor.standing.forecasts_produced,
        "refused_untrained": regressor.standing.refused_untrained,
        "refused_features_missing": regressor.standing.refused_features_missing,
        "mean_absolute_error_in_log_space": regressor.standing.mean_absolute_error,
        "coefficients": dict(sorted(regressor.coefficients.items())),
        "features_used": dict(sorted(regressor.standing.features_used.items())),
    }


def run_realised_vol_regressor(
    regressor: RealisedVolRegressor, control_socket, read_feature_sets, publish_forecasts,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        feature_sets = read_feature_sets(regressor)
        publish_forecasts(tuple(regressor.forecast(feature_set) for feature_set in feature_sets))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
