"""resting-order-cancel-policy: when a still-open order is stale enough to pull.

Two ways an order goes stale, and they are not the same failure:

- **Time.** It has been sitting long enough that the reason for placing it has
  probably passed. A signal that was true two minutes ago is not evidence now.
- **Distance.** The market has walked away from it. An order five percent from
  the touch is not going to fill, and holding it locks capital against nothing.

Both thresholds are **learned** (RL-060), because both are properties of the
symbol rather than of the system. A minute is an eternity in BTCUSDT and nothing
at all in a thin altcoin; a spread that is one tick on one symbol is thirty on
another. The part measures how long its own orders actually take to fill and how
far from the touch they were when they did, and it pulls an order once it is past
what has ever worked for that symbol.

Cancelling costs something too -- a cancelled order that would have filled is a
trade not taken -- so both estimates are set at a high quantile of what has
worked, and the operator's settings are hard ceilings over them (RL-061).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_staleness import ObservedPrice
from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "resting-order-cancel-policy"

PART_DECLARATION = PartDeclaration(
    part_id="resting-order-cancel-policy",
    consumes=("order-request", "market-data"),
    produces=("cancel-decision", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HOLD = "hold"
CANCEL_TIME = "cancel-past-time-to-live"
CANCEL_DISTANCE = "cancel-too-far-from-market"

# The quantile of fills that worked, used as the tolerance. High on purpose: the
# error of cancelling too early is a missed trade, which is silent, while the
# error of cancelling too late is locked capital, which is visible.
FILLED_QUANTILE = 0.95


@dataclass(frozen=True)
class CancelDecision:
    """Whether to pull one resting order, and which staleness triggered it."""

    order_id: str
    venue_id: str
    symbol: str
    action: str
    resting_seconds: float
    distance_fraction: float | None
    time_to_live_estimate: Estimate
    distance_estimate: Estimate
    reason: str
    decided_at_ns: int

    @property
    def should_cancel(self) -> bool:
        return self.action != HOLD


@dataclass
class _RestingOrder:
    venue_id: str
    symbol: str
    limit_price: float
    placed_at_monotonic: float


@dataclass
class PolicyStanding:
    orders_watched: int = 0
    decisions: int = 0
    cancelled_for_time: int = 0
    cancelled_for_distance: int = 0
    fills_learned_from: int = 0
    symbols_fitted: int = 0


class RestingOrderCancelPolicy:
    """Pulls an order once it is staler than anything that has ever filled here."""

    def __init__(
        self,
        prior_time_to_live_seconds: float,
        maximum_time_to_live_seconds: float,
        prior_distance_fraction: float,
        maximum_distance_fraction: float,
        minimum_observations: int,
        window: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._prior_ttl = prior_time_to_live_seconds
        self._maximum_ttl = maximum_time_to_live_seconds
        self._prior_distance = prior_distance_fraction
        self._maximum_distance = maximum_distance_fraction
        self._minimum_observations = minimum_observations
        self._window = window
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._resting: dict[str, _RestingOrder] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self._fill_seconds: dict[tuple[str, str], QuantileEstimator] = {}
        self._fill_distance: dict[tuple[str, str], QuantileEstimator] = {}
        self.standing = PolicyStanding()

    def observe_order_placed(self, order_id: str, venue_id: str, symbol: str, limit_price: float) -> None:
        self._resting[order_id] = _RestingOrder(venue_id, symbol, limit_price, self._monotonic())
        self.standing.orders_watched = len(self._resting)

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, kept with the venue's own time for it.

        `at_ns` has no default. A price with no age cannot be told apart from a
        price that stopped arriving, which is how a symbol frozen for 56 minutes
        was traded on 2026-08-23.
        """
        self._prices[(venue_id, symbol)] = ObservedPrice(price=price, observed_at_ns=at_ns)

    def observe_order_filled(self, order_id: str) -> None:
        """A fill is the only evidence of what patience is worth on this symbol."""
        order = self._resting.pop(order_id, None)
        if order is None:
            return
        key = (order.venue_id, order.symbol)
        self._seconds_estimator(key).observe(self._monotonic() - order.placed_at_monotonic)
        observed = self._prices.get(key)
        price = None if observed is None else observed.price
        if price and order.limit_price:
            self._distance_estimator(key).observe(abs(price - order.limit_price) / price)
        self.standing.fills_learned_from += 1
        self.standing.orders_watched = len(self._resting)

    def observe_order_finished(self, order_id: str) -> None:
        """Cancelled or rejected: stop watching, and learn nothing from it."""
        self._resting.pop(order_id, None)
        self.standing.orders_watched = len(self._resting)

    def decide(self, order_id: str) -> CancelDecision | None:
        order = self._resting.get(order_id)
        if order is None:
            return None

        key = (order.venue_id, order.symbol)
        resting = self._monotonic() - order.placed_at_monotonic
        observed = self._prices.get(key)
        price = None if observed is None else observed.price
        distance = abs(price - order.limit_price) / price if price and order.limit_price else None

        ttl = self._seconds_estimator(key).estimate(
            FILLED_QUANTILE, self._minimum_observations, bound_low=0.0, bound_high=self._maximum_ttl
        )
        allowed_distance = self._distance_estimator(key).estimate(
            FILLED_QUANTILE, self._minimum_observations, bound_low=0.0, bound_high=self._maximum_distance
        )
        self.standing.decisions += 1

        if resting > ttl.value:
            self.standing.cancelled_for_time += 1
            self._resting.pop(order_id, None)
            return self._decision(
                order_id, order, CANCEL_TIME, resting, distance, ttl, allowed_distance,
                f"resting {resting:.1f}s past a tolerance of {ttl.value:.1f}s "
                f"({'learned' if ttl.is_fitted else 'the operator prior'})",
            )

        if distance is not None and distance > allowed_distance.value:
            self.standing.cancelled_for_distance += 1
            self._resting.pop(order_id, None)
            return self._decision(
                order_id, order, CANCEL_DISTANCE, resting, distance, ttl, allowed_distance,
                f"{distance:.2%} from the market against a tolerance of {allowed_distance.value:.2%}",
            )

        return self._decision(
            order_id, order, HOLD, resting, distance, ttl, allowed_distance,
            "still within both tolerances",
        )

    def decide_all(self) -> tuple[CancelDecision, ...]:
        return tuple(
            decision
            for order_id in list(self._resting)
            if (decision := self.decide(order_id)) is not None
        )

    def _decision(self, order_id, order, action, resting, distance, ttl, allowed, reason) -> CancelDecision:
        return CancelDecision(
            order_id=order_id,
            venue_id=order.venue_id,
            symbol=order.symbol,
            action=action,
            resting_seconds=resting,
            distance_fraction=distance,
            time_to_live_estimate=ttl,
            distance_estimate=allowed,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )

    def _seconds_estimator(self, key) -> QuantileEstimator:
        estimator = self._fill_seconds.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_ttl)
            self._fill_seconds[key] = estimator
        return estimator

    def _distance_estimator(self, key) -> QuantileEstimator:
        estimator = self._fill_distance.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_distance)
            self._fill_distance[key] = estimator
        return estimator


def describe_cancel_policy(policy: RestingOrderCancelPolicy) -> dict:
    fitted = sum(
        1
        for key, estimator in policy._fill_seconds.items()
        if estimator.estimate(FILLED_QUANTILE, policy._minimum_observations).is_fitted
    )
    return {
        "part_id": PART_ID,
        "orders_watched": policy.standing.orders_watched,
        "decisions": policy.standing.decisions,
        "cancelled_for_time": policy.standing.cancelled_for_time,
        "cancelled_for_distance": policy.standing.cancelled_for_distance,
        "fills_learned_from": policy.standing.fills_learned_from,
        "symbols_with_a_fitted_tolerance": fitted,
    }


def run_resting_order_cancel_policy(
    policy: RestingOrderCancelPolicy, control_socket, read_events, publish_decisions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(policy)
        publish_decisions(policy.decide_all())

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    orders = Batch(read=context.bus.reader("order-request"))
    trades = Batch(read=context.bus.reader("market-data"))
    publish_decisions = context.bus.publisher_for("cancel-decision")
    policy = RestingOrderCancelPolicy(
        prior_time_to_live_seconds=context.number("resting_order_prior_time_to_live"),
        maximum_time_to_live_seconds=context.number("resting_order_maximum_time_to_live"),
        prior_distance_fraction=context.number("resting_order_prior_distance_fraction"),
        maximum_distance_fraction=context.number("resting_order_maximum_distance_fraction"),
        minimum_observations=int(context.number("execution_minimum_observations")),
        window=int(context.number("execution_window")),
    )

    def read_events(_policy) -> None:
        for order in orders.payloads():
            if order.cancels_client_order_id:
                policy.observe_order_finished(order.cancels_client_order_id)
            elif order.order_type == "limit" and order.limit_price > 0 and order.outcome == "routed":
                policy.observe_order_placed(order.client_order_id, order.venue_id, order.symbol, order.limit_price)
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                policy.observe_price(
                    trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
                )

    def publish(decisions) -> None:
        acted = tuple(d for d in decisions if d.action != HOLD)
        if acted:
            publish_decisions(acted)

    return run_resting_order_cancel_policy(
        policy=policy,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_decisions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
