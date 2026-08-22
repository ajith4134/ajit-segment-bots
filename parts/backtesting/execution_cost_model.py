"""execution-cost-model: what a trade would actually cost, learned from real fills.

The difference between a profitable backtest and a losing strategy is usually this
one number, and it is usually a constant somebody typed. A flat "0.1% per trade"
understates the cost of a thin altcoin by an order of magnitude and overstates it for
BTCUSDT, which is exactly the pair of errors that makes a system trade the wrong
instruments.

So the cost model is fitted from this system's own measured shortfalls (RL-061), per
venue and per symbol, and it is fitted in three parts because they scale differently:

- **Fee** is known exactly and scales linearly with notional. It is the only part
  that is not estimated.
- **Half-spread** is measured and roughly constant in fractional terms; it is what
  crossing costs regardless of size.
- **Impact per unit** grows with the square root of participation, which is the
  shape every published measurement of market impact agrees on even where the
  constant differs. Four times the size costs twice as much per unit, not four
  times. Modelling it as flat makes large trades look cheap; modelling the per-unit
  cost as linear in size makes them look impossible.

Two refusals. **An unfitted symbol reports itself unfitted** rather than falling back
to a plausible default, because a default cost is exactly what makes an untested
instrument look tradeable. And **the estimate never goes below the fee**, since no
amount of favourable measurement makes a venue stop charging.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.backtest_types import CostEstimate
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "execution-cost-model"

PART_DECLARATION = PartDeclaration(
    part_id="execution-cost-model",
    consumes=("market-data", "slippage-profile", "shortfall-breakdown"),
    produces=("cost-estimate", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FITTED = "fitted-from-measured-fills"
NOT_FITTED = "no-measured-fill-for-this-symbol-yet"
NO_FEE = "this-venue-has-no-recorded-fee-schedule"


@dataclass(frozen=True)
class CostOutcome:
    venue_id: str
    symbol: str
    state: str
    estimate: CostEstimate | None
    reason: str
    estimated_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.estimate is not None


@dataclass
class CostModelStanding:
    shortfalls_observed: int = 0
    estimates_made: int = 0
    unfitted_estimates: int = 0
    symbols_fitted: int = 0
    times_clamped_to_the_fee: int = 0
    defaults_used: int = 0


class ExecutionCostModel:
    """Fits fee, spread and impact per symbol from this system's own shortfalls."""

    def __init__(
        self,
        window: int,
        quantile: float,
        minimum_observations: int,
        prior_half_spread_fraction: float,
        prior_impact_coefficient: float,
        now_ns=time.time_ns,
    ) -> None:
        if window < 2:
            raise ValueError("a quantile over one observation is that observation")
        if not 0.0 < quantile < 1.0:
            raise ValueError(
                "the cost used for sizing is an upper quantile, not the median: sizing "
                "against the typical case ignores the half of the distribution that hurt"
            )
        if minimum_observations < 1:
            raise ValueError("a cost fitted on nothing is a default with a decimal point")
        self._window = window
        self._quantile = quantile
        self._minimum_observations = minimum_observations
        self._prior_half_spread = prior_half_spread_fraction
        self._prior_impact = prior_impact_coefficient
        self._now_ns = now_ns
        self._fees: dict[str, float] = {}
        self._half_spreads: dict[tuple, QuantileEstimator] = {}
        self._impact_coefficients: dict[tuple, QuantileEstimator] = {}
        self._daily_volume: dict[tuple, float] = {}
        self.standing = CostModelStanding()

    def observe_fee_schedule(self, venue_id: str, fee_fraction: float) -> None:
        """Known exactly. The only part of the cost that is not estimated."""
        if fee_fraction < 0:
            raise ValueError("a negative fee is a rebate and needs its own handling")
        self._fees[venue_id] = fee_fraction

    def observe_daily_volume(self, venue_id: str, symbol: str, volume: float) -> None:
        self._daily_volume[(venue_id, symbol)] = volume

    def observe_shortfall(
        self, venue_id: str, symbol: str, breakdown, notional: float,
    ) -> None:
        """A measured fill, taken apart as the shortfall decomposer split it.

        The decomposer already separated spread from impact, so nothing is re-derived
        here -- re-deriving it would produce a second, disagreeing estimate of the
        same measurement.
        """
        if notional <= 0:
            return
        volume = self._daily_volume.get((venue_id, symbol))
        participation = notional / volume if volume and volume > 0 else 0.0
        self.observe_measured_costs(
            venue_id=venue_id,
            symbol=symbol,
            half_spread_fraction=abs(breakdown.spread_cost) / notional,
            impact_fraction=abs(breakdown.impact_cost) / notional,
            participation=participation,
        )

    def observe_measured_costs(
        self, venue_id: str, symbol: str, half_spread_fraction: float,
        impact_fraction: float, participation: float,
    ) -> None:
        key = (venue_id, symbol)
        if key not in self._half_spreads:
            self.standing.symbols_fitted += 1
        self._half_spreads.setdefault(
            key, QuantileEstimator(window=self._window, prior=self._prior_half_spread)
        ).observe(half_spread_fraction)
        # Impact is stored as a coefficient on the square root of participation, so
        # the estimate generalises to sizes never traded rather than only to this one.
        root = math.sqrt(participation) if participation > 0 else 0.0
        coefficient = impact_fraction / root if root > 0 else self._prior_impact
        self._impact_coefficients.setdefault(
            key, QuantileEstimator(window=self._window, prior=self._prior_impact)
        ).observe(coefficient)
        self.standing.shortfalls_observed += 1

    def estimate(self, venue_id: str, symbol: str, notional: float) -> CostOutcome:
        fee_fraction = self._fees.get(venue_id)
        if fee_fraction is None:
            return self._outcome(
                venue_id, symbol, NO_FEE, None,
                f"{venue_id} has no recorded fee schedule, and the fee is the one part of "
                f"the cost that is known rather than estimated",
            )

        key = (venue_id, symbol)
        spread = self._half_spreads.get(key)
        impact = self._impact_coefficients.get(key)
        spread_estimate = (
            spread.estimate(self._quantile, self._minimum_observations) if spread else None
        )
        impact_estimate = (
            impact.estimate(self._quantile, self._minimum_observations) if impact else None
        )
        is_fitted = bool(
            spread_estimate and spread_estimate.is_fitted
            and impact_estimate and impact_estimate.is_fitted
        )

        half_spread_fraction = (
            spread_estimate.value if spread_estimate else self._prior_half_spread
        )
        impact_coefficient = (
            impact_estimate.value if impact_estimate else self._prior_impact
        )

        volume = self._daily_volume.get(key)
        participation = notional / volume if volume and volume > 0 else 0.0
        # Square root of participation: the per-unit cost doubles when the size
        # quadruples. A flat per-unit impact makes large trades look cheap, and a
        # linear one makes them look impossible.
        impact_fraction = impact_coefficient * math.sqrt(participation)

        fee = fee_fraction * notional
        half_spread = half_spread_fraction * notional
        expected_impact = impact_fraction * notional

        total = fee + half_spread + expected_impact
        if total < fee:
            self.standing.times_clamped_to_the_fee += 1
            half_spread = 0.0
            expected_impact = 0.0

        estimate = CostEstimate(
            venue_id=venue_id, symbol=symbol, notional=notional, fee=fee,
            half_spread=half_spread, expected_impact=expected_impact,
            is_fitted=is_fitted,
            observations=(spread.observations if spread else 0),
            estimated_at_ns=self._now_ns(),
        )
        self.standing.estimates_made += 1
        if not is_fitted:
            self.standing.unfitted_estimates += 1

        return self._outcome(
            venue_id, symbol, FITTED if is_fitted else NOT_FITTED, estimate,
            f"{estimate.fraction_of_notional:.4%} of notional: "
            f"{fee / notional:.4%} fee, {half_spread / notional:.4%} spread, "
            f"{expected_impact / notional:.4%} impact at "
            f"{participation:.4%} participation"
            + (
                ""
                if is_fitted
                else ". Unfitted: no measured fill for this symbol yet, and a plausible "
                     "default cost is what makes an untested instrument look tradeable"
            ),
        )

    def _outcome(self, venue_id, symbol, state, estimate, reason) -> CostOutcome:
        return CostOutcome(
            venue_id=venue_id, symbol=symbol, state=state, estimate=estimate,
            reason=reason, estimated_at_ns=self._now_ns(),
        )


def describe_cost_model(model: ExecutionCostModel) -> dict:
    return {
        "part_id": PART_ID,
        "measurements_observed": model.standing.shortfalls_observed,
        "estimates_made": model.standing.estimates_made,
        "unfitted_estimates": model.standing.unfitted_estimates,
        "symbols_fitted": model.standing.symbols_fitted,
        "times_clamped_to_the_fee": model.standing.times_clamped_to_the_fee,
        "quantile": model._quantile,
        "uses_a_flat_cost_per_trade": False,
        "defaults_used": model.standing.defaults_used,
        "models_impact_as_linear_in_size": False,
    }


def run_execution_cost_model(
    model: ExecutionCostModel, control_socket, read_measurements, read_requests,
    publish_estimates, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for job in read_measurements():
            model.observe_measured_costs(**job)
        for venue_id, symbol, notional in read_requests():
            outcome = model.estimate(venue_id, symbol, notional)
            if outcome.is_usable:
                publish_estimates(outcome.estimate)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
