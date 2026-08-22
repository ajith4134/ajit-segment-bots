"""slippage-learner: what execution actually costs here, learned rather than assumed.

Every part that decides whether a trade is worth taking needs a cost number.
Assuming one is how a system trades an edge that never survived its own
execution, and the assumed number is always too small -- it is estimated from the
quoted spread, and the quoted spread is what you see, not what you get.

So the cost is measured from real fills, and split by what it actually depends
on:

- **By symbol and by size.** Slippage is not a property of a symbol; it is a
  property of a symbol at a size, and the two are related nonlinearly. A profile
  that averaged over sizes would understate the large orders that matter.
- **By order kind.** A market order pays the spread; a limit order pays waiting
  and sometimes not filling at all. The unfilled ones are the expensive half, and
  a learner that only saw fills would report limit orders as free.
- **By condition.** Slippage in a fast market is a different distribution, and
  the fast market is where the system most wants to act.

**A quantile, not a mean.** The cost distribution has a long tail, and the mean
describes no order at all: sizing must be done against what happens when it goes
badly, not on average.

**Unfilled orders are counted.** An order that never filled cost its whole
opportunity, and a slippage profile ignoring them makes patient execution look
free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "slippage-learner"

PART_DECLARATION = PartDeclaration(
    part_id="slippage-learner",
    consumes=("fill", "bounded-order", "shortfall-breakdown"),
    produces=("slippage-profile", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
NOT_MEASURED = "too-few-fills-in-this-bucket"

MARKET = "market"
LIMIT = "limit"

CALM = "calm"
FAST = "fast"


@dataclass(frozen=True)
class SlippageProfile:
    """What execution costs for one symbol, size band, order kind and condition."""

    venue_id: str
    symbol: str
    size_band: str
    order_kind: str
    condition: str
    state: str
    typical_cost: float
    tail_cost: float
    fill_rate: Estimate
    fills_observed: int
    unfilled_observed: int
    reason: str
    learned_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == MEASURED

    def expected_cost(self, include_unfilled_opportunity: float = 0.0) -> float:
        """The cost including what never filling costs, when that is known.

        A profile ignoring unfilled orders makes patient execution look free.
        """
        if not self.fill_rate.is_fitted or self.fill_rate.value <= 0:
            return self.tail_cost
        return self.typical_cost + (1.0 - self.fill_rate.value) * include_unfilled_opportunity


@dataclass
class LearnerStanding:
    fills_observed: int = 0
    unfilled_observed: int = 0
    buckets_tracked: int = 0
    measured_buckets: int = 0
    worst_tail_cost: float | None = None
    by_order_kind: dict = field(default_factory=dict)
    by_condition: dict = field(default_factory=dict)


class SlippageLearner:
    """Learns real execution cost per symbol, size band, order kind and condition."""

    def __init__(
        self,
        size_bands: tuple,
        typical_quantile: float,
        tail_quantile: float,
        window: int,
        minimum_fills: int,
        prior_cost_fraction: float,
        prior_fill_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not size_bands:
            raise ValueError(
                "slippage is a property of a symbol at a size; a profile with no size bands "
                "averages the large orders that matter into the small ones that do not"
            )
        if not 0.5 < tail_quantile < 1.0:
            raise ValueError(
                "sizing must be done against what happens when execution goes badly, so the "
                "tail quantile is in the upper half"
            )
        self._size_bands = tuple(sorted(size_bands))
        self._typical_quantile = typical_quantile
        self._tail_quantile = tail_quantile
        self._window = window
        self._minimum = minimum_fills
        self._prior_cost = prior_cost_fraction
        self._prior_fill_rate = prior_fill_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._costs: dict[tuple, QuantileEstimator] = {}
        self._fill_rates: dict[tuple, RateEstimator] = {}
        self._unfilled: dict[tuple, int] = {}
        self.standing = LearnerStanding()

    def band_for(self, notional: float) -> str:
        """Which size band an order falls in. Bands, because the relation is nonlinear."""
        for band in self._size_bands:
            if notional <= band:
                return f"<= {band:,.0f}"
        return f"> {self._size_bands[-1]:,.0f}"

    def observe_fill(
        self, venue_id: str, symbol: str, notional: float, order_kind: str,
        condition: str, cost_fraction: float,
    ) -> None:
        """One real fill, and what it actually cost against its decision price."""
        key = (venue_id, symbol, self.band_for(notional), order_kind, condition)
        self._cost_for(key).observe(abs(cost_fraction))
        self._fill_rate_for(key).observe(True)
        self.standing.fills_observed += 1
        self.standing.by_order_kind[order_kind] = (
            self.standing.by_order_kind.get(order_kind, 0) + 1
        )
        self.standing.by_condition[condition] = (
            self.standing.by_condition.get(condition, 0) + 1
        )
        self.standing.buckets_tracked = len(self._costs)

    def observe_unfilled(
        self, venue_id: str, symbol: str, notional: float, order_kind: str, condition: str
    ) -> None:
        """An order that never filled. The expensive half of patient execution."""
        key = (venue_id, symbol, self.band_for(notional), order_kind, condition)
        self._fill_rate_for(key).observe(False)
        self._unfilled[key] = self._unfilled.get(key, 0) + 1
        self.standing.unfilled_observed += 1

    def profile(
        self, venue_id: str, symbol: str, notional: float, order_kind: str, condition: str
    ) -> SlippageProfile:
        band = self.band_for(notional)
        key = (venue_id, symbol, band, order_kind, condition)
        costs = self._cost_for(key)
        typical = costs.estimate(self._typical_quantile, self._minimum)
        tail = costs.estimate(self._tail_quantile, self._minimum)
        fill_rate = self._fill_rate_for(key).estimate(self._minimum)
        fills = costs.observations
        unfilled = self._unfilled.get(key, 0)

        if not typical.is_fitted:
            return self._profile(
                venue_id, symbol, band, order_kind, condition, NOT_MEASURED,
                typical.value, tail.value, fill_rate, fills, unfilled,
                f"{fills} fill(s) of the {self._minimum} needed for {symbol} at {band} with a "
                f"{order_kind} order in a {condition} market; the setting stands and is not a "
                f"measurement",
            )

        self.standing.measured_buckets += 1
        if self.standing.worst_tail_cost is None or tail.value > self.standing.worst_tail_cost:
            self.standing.worst_tail_cost = tail.value

        return self._profile(
            venue_id, symbol, band, order_kind, condition, MEASURED,
            typical.value, tail.value, fill_rate, fills, unfilled,
            f"{symbol} at {band} with a {order_kind} order in a {condition} market costs "
            f"{typical.value:.4%} typically and {tail.value:.4%} at the "
            f"{self._tail_quantile:.0%} quantile over {fills} fill(s)"
            + (
                f"; {unfilled} order(s) never filled, a {fill_rate.value:.0%} fill rate -- the "
                f"expensive half of patient execution, and a profile ignoring them makes it "
                f"look free"
                if unfilled
                else ""
            )
            + ". A quantile rather than a mean, because the cost distribution has a long tail "
            "and the mean describes no order at all",
        )

    def _cost_for(self, key) -> QuantileEstimator:
        estimator = self._costs.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_cost)
            self._costs[key] = estimator
        return estimator

    def _fill_rate_for(self, key) -> RateEstimator:
        estimator = self._fill_rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_fill_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._fill_rates[key] = estimator
        return estimator

    def _profile(
        self, venue_id, symbol, band, order_kind, condition, state,
        typical, tail, fill_rate, fills, unfilled, reason,
    ) -> SlippageProfile:
        return SlippageProfile(
            venue_id=venue_id,
            symbol=symbol,
            size_band=band,
            order_kind=order_kind,
            condition=condition,
            state=state,
            typical_cost=typical,
            tail_cost=tail,
            fill_rate=fill_rate,
            fills_observed=fills,
            unfilled_observed=unfilled,
            reason=reason,
            learned_at_ns=self._now_ns(),
        )


def describe_slippage(learner: SlippageLearner) -> dict:
    return {
        "part_id": PART_ID,
        "fills_observed": learner.standing.fills_observed,
        "unfilled_observed": learner.standing.unfilled_observed,
        "buckets_tracked": learner.standing.buckets_tracked,
        "measured_buckets": learner.standing.measured_buckets,
        "worst_tail_cost": learner.standing.worst_tail_cost,
        "by_order_kind": dict(sorted(learner.standing.by_order_kind.items())),
        "by_condition": dict(sorted(learner.standing.by_condition.items())),
        "size_bands": list(learner._size_bands),
    }


def run_slippage_learner(
    learner: SlippageLearner, control_socket, read_fills, publish_profiles,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        requests = read_fills(learner)
        publish_profiles(
            tuple(
                learner.profile(venue_id, symbol, notional, order_kind, condition)
                for venue_id, symbol, notional, order_kind, condition in requests
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
