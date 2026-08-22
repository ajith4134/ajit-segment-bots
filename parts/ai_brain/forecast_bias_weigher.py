"""forecast-bias-weigher: how much the forecast is allowed to move the brain.

The ensemble forecast is another opinion, not an oracle, and this part is what
keeps it in that position. Its output is a **bias** -- a bounded shade on the
brain's conviction -- rather than a probability, because a forecast that could
carry a decision by itself would make the three bots decoration.

Trust is what scales it, and trust is measured rather than assumed:

- **A forecast is trusted in proportion to how often it has been right**, in the
  same conditions. A model that is accurate in a trend and useless in a chop has
  two trust numbers, and one over both would import the trend's accuracy into the
  chop.
- **An untrusted forecast contributes exactly nothing.** Not a small amount:
  zero. A forecast with no record is not a weak signal, it is an unmeasured one,
  and letting it contribute "a little" is how an unvalidated model gets into
  every decision.
- **Trust decays.** A forecaster that was right through one regime carries a
  trust number the next one invalidates, and the half-life is what stops that
  authority outliving its evidence.

**The magnitude of the forecast does not raise the bias past its ceiling.** A
model predicting a 40% move is not more trustworthy than one predicting 2%; it is
more likely to be broken, and the ceiling is what makes the two indistinguishable
to everything downstream.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import ForecastBias

PART_ID = "forecast-bias-weigher"

PART_DECLARATION = PartDeclaration(
    part_id="forecast-bias-weigher",
    consumes=("ensemble-forecast", "forecast-trust"),
    produces=("forecast-bias", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TRUSTED = "trusted"
NOT_YET_MEASURED = "the-forecaster-has-no-measured-record-in-these-conditions"
NOT_TRUSTED = "the-forecaster-has-been-wrong-too-often-here"
NO_FORECAST = "no-forecast-has-arrived-for-this-symbol"


@dataclass
class WeigherStanding:
    forecasts_weighed: int = 0
    trusted: int = 0
    untrusted: int = 0
    unmeasured: int = 0
    clipped_at_the_ceiling: int = 0
    largest_bias: float = 0.0
    by_condition: dict = field(default_factory=dict)


class ForecastBiasWeigher:
    """Turns a forecast into a bounded shade, scaled by how often it has been right."""

    def __init__(
        self,
        maximum_bias: float,
        minimum_trust: float,
        prior_trust: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_bias < 0.5:
            raise ValueError(
                "the forecast may shade a decision, never make one; a ceiling of half the "
                "probability range would let it overturn the bots"
            )
        if not 0.0 <= minimum_trust <= 1.0:
            raise ValueError("trust is a hit rate and must be in [0, 1]")
        self._maximum_bias = maximum_bias
        self._minimum_trust = minimum_trust
        self._prior_trust = prior_trust
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._trust: dict[tuple[str, str], RateEstimator] = {}
        self._forecasts: dict[tuple[str, str], float] = {}
        self.standing = WeigherStanding()

    def observe_forecast(self, venue_id: str, symbol: str, expected_return: float) -> None:
        self._forecasts[(venue_id, symbol)] = expected_return

    def observe_forecast_outcome(self, forecaster: str, condition: str, was_right: bool) -> None:
        """Whether the forecast's direction was borne out, in these conditions."""
        self._trust_for(forecaster, condition).observe(was_right)
        self.standing.by_condition[condition] = self.standing.by_condition.get(condition, 0) + 1

    def trust_in(self, forecaster: str, condition: str) -> Estimate:
        return self._trust_for(forecaster, condition).estimate(self._minimum)

    def weigh(self, venue_id: str, symbol: str, forecaster: str, condition: str) -> ForecastBias:
        self.standing.forecasts_weighed += 1
        forecast = self._forecasts.get((venue_id, symbol))
        trust = self.trust_in(forecaster, condition)

        if forecast is None:
            return self._bias(venue_id, symbol, 0.0, trust, 0.0, NO_FORECAST,
                              f"no forecast has arrived for {symbol}")

        if not trust.is_fitted:
            # Zero, not a small amount. An unmeasured forecaster is not a weak
            # signal, and letting it contribute "a little" is how an unvalidated
            # model gets into every decision.
            self.standing.unmeasured += 1
            return self._bias(
                venue_id, symbol, 0.0, trust, forecast, NOT_YET_MEASURED,
                f"{forecaster} has {trust.observations} outcome(s) of the {self._minimum} "
                f"needed in {condition}, so it contributes nothing rather than a little",
            )

        if trust.value < self._minimum_trust:
            self.standing.untrusted += 1
            return self._bias(
                venue_id, symbol, 0.0, trust, forecast, NOT_TRUSTED,
                f"{forecaster} has been right {trust.value:.0%} of the time in {condition}, "
                f"below the {self._minimum_trust:.0%} this brain listens to",
            )

        # Direction times trust, then bounded. The forecast's own magnitude does
        # not enter: a model predicting a 40% move is not more trustworthy than
        # one predicting 2%, it is more likely to be broken.
        direction = 1.0 if forecast > 0 else (-1.0 if forecast < 0 else 0.0)
        bias = direction * trust.value * self._maximum_bias
        clipped = abs(bias) >= self._maximum_bias
        if clipped:
            self.standing.clipped_at_the_ceiling += 1
        self.standing.trusted += 1
        self.standing.largest_bias = max(self.standing.largest_bias, abs(bias))

        return self._bias(
            venue_id, symbol, bias, trust, forecast, TRUSTED,
            f"{forecaster} predicts {forecast:+.2%} and has been right {trust.value:.0%} of "
            f"the time over {trust.observations} outcome(s) in {condition}, so the brain's "
            f"conviction is shaded {bias:+.1%} -- bounded at {self._maximum_bias:.1%} so the "
            f"forecast can never carry a decision by itself",
        )

    def _trust_for(self, forecaster: str, condition: str) -> RateEstimator:
        key = (forecaster, condition)
        estimator = self._trust.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_trust, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._trust[key] = estimator
        return estimator

    def _bias(self, venue_id, symbol, bias, trust, forecast, state, reason) -> ForecastBias:
        return ForecastBias(
            venue_id=venue_id,
            symbol=symbol,
            bias=bias,
            trust=trust,
            forecast=forecast,
            reason=f"[{state}] {reason}",
            weighed_at_ns=self._now_ns(),
        )


def describe_forecast_bias(weigher: ForecastBiasWeigher) -> dict:
    return {
        "part_id": PART_ID,
        "forecasts_weighed": weigher.standing.forecasts_weighed,
        "trusted": weigher.standing.trusted,
        "untrusted": weigher.standing.untrusted,
        "not_yet_measured": weigher.standing.unmeasured,
        "clipped_at_the_ceiling": weigher.standing.clipped_at_the_ceiling,
        "largest_bias_applied": weigher.standing.largest_bias,
        "conditions_with_a_record": dict(sorted(weigher.standing.by_condition.items())),
        "forecaster_condition_records": len(weigher._trust),
    }


def run_forecast_bias_weigher(
    weigher: ForecastBiasWeigher, control_socket, read_forecasts_and_trust, publish_biases,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        publish_biases(
            tuple(
                weigher.weigh(venue_id, symbol, forecaster, condition)
                for venue_id, symbol, forecaster, condition in read_forecasts_and_trust(weigher)
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
