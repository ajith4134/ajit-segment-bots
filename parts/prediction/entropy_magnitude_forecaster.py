"""entropy-magnitude-forecaster: how big the next move is, from the structure in the flow.

The third part from `docs/research/order-flow-entropy.md` (arXiv:2512.15720), and
the one that turns the entropy measurement into a number something can size on.

**Magnitude only. Never direction.** Theorem 2: entropy is invariant under
swapping the buy and sell labels, so `E[sgn(r) | H] = 0` -- the paper measured
45.0% directional accuracy, indistinguishable from chance, and that is a theorem
rather than a weak result. This part produces a `volatility-forecast`, which is
directionless by construction; there is nowhere in its output for a direction to
appear.

**It learns the entropy-to-magnitude relation rather than assuming the paper's
numbers.** The measured ratio on SPY was 2.17 between the lowest and highest
entropy quintiles, and 2.89 below the fifth percentile. Those are that market's
numbers over 36 days; this system trades a different asset class and must measure
its own (RL-061). So the forecaster keeps the observed absolute return per
entropy quintile and reports the multiple it has actually seen -- and says so
while it is still the prior.

**It is a third independent estimator of the same quantity**, beside the realised
volatility regressor and the implied surface. That is what makes it a spare part
rather than a rewrite: when the three disagree, the ensembler can see it.

**Unproven here.** The paper is 36 days on one equity ETF, VIX 14-22 throughout,
with 38.5% of its profit from a single day. This part measures its own edge and
reports how thin the evidence is, rather than inheriting a result from a market
it was not measured in.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.forecast_types import AVAILABLE, VolatilityForecast
from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "entropy-magnitude-forecaster"

PART_DECLARATION = PartDeclaration(
    part_id="entropy-magnitude-forecaster",
    consumes=("flow-entropy", "vol-feature-set"),
    produces=("volatility-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NO_ENTROPY = "no-usable-entropy-measurement"
NOT_YET_MEASURED = "this-market's-entropy-to-magnitude-relation-is-not-measured-yet"

# The quintiles the paper reports its results in. Five because that is what its
# table is built on, so the numbers here are comparable to it.
QUINTILE_COUNT = 5


@dataclass
class ForecasterStanding:
    forecasts_made: int = 0
    forecasts_produced: int = 0
    refused_no_entropy: int = 0
    refused_unmeasured: int = 0
    outcomes_observed: int = 0
    by_quintile: dict = field(default_factory=dict)
    strongest_multiple_seen: float | None = None


class EntropyMagnitudeForecaster:
    """Learns what low entropy has actually meant here, and forecasts magnitude only."""

    def __init__(
        self,
        horizon_seconds: float,
        minimum_observations_per_quintile: int,
        outcome_window: int,
        prior_absolute_return: float,
        low_entropy_percentile: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < low_entropy_percentile < 0.5:
            raise ValueError(
                "the structured condition is the low tail of entropy; a threshold at or "
                "above the median is not a tail"
            )
        self._horizon = horizon_seconds
        self._minimum = minimum_observations_per_quintile
        self._outcome_window = outcome_window
        self._prior_absolute_return = prior_absolute_return
        self._low_percentile = low_entropy_percentile
        self._now_ns = now_ns
        self._by_quintile: dict[int, QuantileEstimator] = {}
        self._overall = QuantileEstimator(window=outcome_window, prior=prior_absolute_return)
        self.standing = ForecasterStanding()

    def observe_outcome(self, entropy_percentile: float, absolute_return: float) -> None:
        """One measured pair: where entropy sat, and how far price then moved.

        The relation is learned rather than taken from the paper: 2.17 and 2.89
        are SPY's numbers over 36 days, and this system trades a different asset
        class (RL-061).
        """
        if absolute_return < 0:
            raise ValueError("an absolute return cannot be negative")
        quintile = self._quintile_of(entropy_percentile)
        self._estimator_for(quintile).observe(absolute_return)
        self._overall.observe(absolute_return)
        self.standing.outcomes_observed += 1
        self.standing.by_quintile[quintile] = self.standing.by_quintile.get(quintile, 0) + 1

    def multiple_for(self, quintile: int) -> tuple[float | None, bool]:
        """How much larger moves have been in this quintile than overall, here."""
        estimator = self._by_quintile.get(quintile)
        overall = self._overall.estimate(0.5, minimum_observations=self._minimum)
        if estimator is None or overall.value <= 0:
            return None, False
        quintile_median = estimator.estimate(0.5, minimum_observations=self._minimum)
        measured = quintile_median.is_fitted and overall.is_fitted
        return quintile_median.value / overall.value, measured

    def forecast(self, entropy, feature_set=None) -> VolatilityForecast:
        self.standing.forecasts_made += 1

        if entropy is None or not entropy.is_usable or entropy.percentile is None:
            self.standing.refused_no_entropy += 1
            return self._forecast(
                entropy, None, NO_ENTROPY, (),
                "no usable entropy measurement, so there is nothing to forecast magnitude from",
            )

        quintile = self._quintile_of(entropy.percentile)
        multiple, measured = self.multiple_for(quintile)
        baseline = self._overall.estimate(0.5, minimum_observations=self._minimum)

        if multiple is None or not baseline.is_fitted:
            self.standing.refused_unmeasured += 1
            return self._forecast(
                entropy, None, NOT_YET_MEASURED, (),
                f"entropy is at the {entropy.percentile:.0%} percentile, in quintile "
                f"{quintile}, but this market's own entropy-to-magnitude relation has "
                f"{self.standing.outcomes_observed} observation(s) and is not measured yet. "
                f"The paper's 2.17 quintile ratio is SPY's over 36 days and is not inherited",
            )

        expected = baseline.value * multiple
        if self.standing.strongest_multiple_seen is None or multiple > self.standing.strongest_multiple_seen:
            self.standing.strongest_multiple_seen = multiple

        inputs = ["flow-entropy"]
        if feature_set is not None and feature_set.features.get("close_to_close_short") is not None:
            inputs.append("vol-feature-set")

        self.standing.forecasts_produced += 1
        return self._forecast(
            entropy, expected, AVAILABLE, tuple(inputs),
            f"entropy {entropy.entropy:.4f} is at the {entropy.percentile:.0%} percentile "
            f"(quintile {quintile}); moves in that quintile have been {multiple:.2f}x the "
            f"{baseline.value:.4%} median here, so {expected:.4%} is expected over "
            f"{self._horizon:.0f}s"
            + (
                ". This is a magnitude and carries no direction: entropy is invariant under "
                "swapping the buy and sell labels, which is a theorem rather than a limit of "
                "this implementation"
            )
            + (
                f". Structured condition: entropy below the {self._low_percentile:.0%} "
                f"percentile, which this is"
                if entropy.percentile <= self._low_percentile
                else ""
            ),
        )

    def _quintile_of(self, percentile: float) -> int:
        return min(QUINTILE_COUNT, max(1, int(percentile * QUINTILE_COUNT) + 1))

    def _estimator_for(self, quintile: int) -> QuantileEstimator:
        estimator = self._by_quintile.get(quintile)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._outcome_window, prior=self._prior_absolute_return
            )
            self._by_quintile[quintile] = estimator
        return estimator

    def _forecast(self, entropy, volatility, state, inputs, reason) -> VolatilityForecast:
        return VolatilityForecast(
            venue_id=entropy.venue_id if entropy is not None else "",
            symbol=entropy.symbol if entropy is not None else "",
            forecaster=PART_ID,
            expected_volatility=volatility,
            horizon_seconds=self._horizon,
            state=state,
            inputs_used=inputs,
            reason=reason,
            forecast_at_ns=self._now_ns(),
        )


def describe_entropy_magnitude(forecaster: EntropyMagnitudeForecaster) -> dict:
    multiples = {
        quintile: forecaster.multiple_for(quintile)[0]
        for quintile in range(1, QUINTILE_COUNT + 1)
    }
    return {
        "part_id": PART_ID,
        "forecasts_made": forecaster.standing.forecasts_made,
        "forecasts_produced": forecaster.standing.forecasts_produced,
        "refused_no_entropy": forecaster.standing.refused_no_entropy,
        "refused_relation_not_measured": forecaster.standing.refused_unmeasured,
        "outcomes_observed": forecaster.standing.outcomes_observed,
        "observations_by_quintile": dict(sorted(forecaster.standing.by_quintile.items())),
        "measured_multiple_by_quintile": multiples,
        "strongest_multiple_seen": forecaster.standing.strongest_multiple_seen,
        "produces_a_direction": False,
    }


def run_entropy_magnitude_forecaster(
    forecaster: EntropyMagnitudeForecaster, control_socket, read_entropy_and_features,
    publish_forecasts, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_forecasts(
            tuple(
                forecaster.forecast(entropy, feature_set)
                for entropy, feature_set in read_entropy_and_features(forecaster)
            )
        )

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

    Outcomes are learned from the realised volatility the next feature set
    carries for the symbol; the forecast pairs the latest entropy with the
    latest features.
    """
    from runtime.input_assembly import Batch, LatestByKey

    entropies = Batch(read=context.bus.reader("flow-entropy"))
    feature_sets = LatestByKey(read=context.bus.reader("vol-feature-set"), key_of=lambda f: (f.venue_id, f.symbol))
    publish_forecasts = context.bus.publisher_for("volatility-forecast")
    forecaster = EntropyMagnitudeForecaster(
        horizon_seconds=context.number("forecast_horizon"),
        minimum_observations_per_quintile=int(context.number("learning_minimum_observations")),
        outcome_window=int(context.number("forecast_outcome_window")),
        prior_absolute_return=context.number("forecast_prior_absolute_return"),
        low_entropy_percentile=context.number("forecast_low_entropy_percentile"),
    )
    last_percentile: dict[tuple[str, str], float] = {}

    def read_entropy_and_features(_forecaster):
        by_symbol = feature_sets.mapping()
        jobs = []
        for entropy in entropies.payloads():
            key = (entropy.venue_id, entropy.symbol)
            feature_set = by_symbol.get(key)
            if feature_set is None:
                continue
            realised = feature_set.features.get("close_to_close_short")
            earlier = last_percentile.get(key)
            if earlier is not None and realised is not None:
                forecaster.observe_outcome(earlier, float(realised))
            if entropy.percentile is not None:
                last_percentile[key] = entropy.percentile
            jobs.append((entropy, feature_set))
        return tuple(jobs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_forecasts(kept)

    return run_entropy_magnitude_forecaster(
        forecaster=forecaster,
        control_socket=context.control_socket,
        read_entropy_and_features=read_entropy_and_features,
        publish_forecasts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
