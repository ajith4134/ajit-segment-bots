"""order-latency-simulator: hold a paper order for a real round trip before it may fill.

A paper order that fills instantly is trading on information from the future. The
price it fills at is the price that was on screen when the decision was made,
which is not the price that exists when a real order arrives -- and the faster the
strategy, the more of its apparent edge is made of exactly that gap.

So a paper order waits. The delay is **measured from live orders where any exist**
(RL-060): the round trip to a venue is a property of this machine, this network
and that venue right now, and a number typed into a config is wrong by an amount
nobody can see. Until real round trips have been observed, the operator's prior
stands in and the part says which it used.

The distribution matters more than the mean. Latency has a long tail, and a
simulator that always used the median would never reproduce the case that costs
money -- the order that arrived late into a moving market. So each order draws
from the observed distribution rather than taking its centre.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-latency-simulator"

PART_DECLARATION = PartDeclaration(
    part_id="order-latency-simulator",
    consumes=("order-request", "money-mode"),
    produces=("delayed-order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HELD = "held"
RELEASED = "released"
LIVE_NOT_DELAYED = "live-order-not-delayed"

PAPER = "paper"
LIVE = "live"

# The quantile reported as the part's own summary. The tail is what costs money,
# so the number worth watching is not the middle one.
REPORTED_QUANTILE = 0.95


@dataclass(frozen=True)
class DelayedOrderRequest:
    """A paper order and the moment it is allowed to reach the book."""

    client_order_id: str
    venue_id: str
    symbol: str
    state: str
    delay_seconds: float
    releases_at_monotonic: float
    latency_estimate: Estimate
    reason: str
    decided_at_ns: int

    @property
    def may_fill_now(self) -> bool:
        return self.state in (RELEASED, LIVE_NOT_DELAYED)


@dataclass
class _HeldOrder:
    venue_id: str
    symbol: str
    releases_at: float
    delay: float


@dataclass
class SimulatorStanding:
    orders_held: int = 0
    orders_released: int = 0
    live_orders_passed: int = 0
    round_trips_measured: int = 0
    longest_delay_seconds: float = 0.0
    venues_measured: int = 0


class OrderLatencySimulator:
    """Delays each paper order by a latency drawn from what live orders really took."""

    def __init__(
        self,
        prior_latency_seconds: float,
        maximum_latency_seconds: float,
        minimum_observations: int,
        window: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
        draw=None,
    ) -> None:
        if prior_latency_seconds <= 0:
            raise ValueError("a paper order that fills instantly trades on the future")
        self._prior = prior_latency_seconds
        self._maximum = maximum_latency_seconds
        self._minimum_observations = minimum_observations
        self._window = window
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._draw = draw or random.random
        self._latencies: dict[str, QuantileEstimator] = {}
        self._held: dict[str, _HeldOrder] = {}
        self.standing = SimulatorStanding()

    def observe_live_round_trip(self, venue_id: str, seconds: float) -> None:
        """How long a real order actually took to reach the venue and be acknowledged."""
        self._estimator_for(venue_id).observe(seconds)
        self.standing.round_trips_measured += 1
        self.standing.venues_measured = len(self._latencies)

    def hold(self, client_order_id: str, venue_id: str, symbol: str, money_mode: str) -> DelayedOrderRequest:
        """Hold a paper order; pass a live one straight through."""
        estimate = self._latency_estimate(venue_id)

        if money_mode == LIVE:
            self.standing.live_orders_passed += 1
            return self._request(
                client_order_id, venue_id, symbol, LIVE_NOT_DELAYED, 0.0,
                self._monotonic(), estimate,
                "a live order is delayed by the network, not by this part",
            )

        delay = self._draw_latency(venue_id, estimate)
        releases_at = self._monotonic() + delay
        self._held[client_order_id] = _HeldOrder(venue_id, symbol, releases_at, delay)
        self.standing.orders_held += 1
        self.standing.longest_delay_seconds = max(self.standing.longest_delay_seconds, delay)

        return self._request(
            client_order_id, venue_id, symbol, HELD, delay, releases_at, estimate,
            f"held {delay:.3f}s for a simulated round trip "
            f"({'measured' if estimate.is_fitted else 'the operator prior'})",
        )

    def released_orders(self) -> tuple[DelayedOrderRequest, ...]:
        """Every held order whose round trip has now elapsed."""
        now = self._monotonic()
        released = []
        for client_order_id, held in list(self._held.items()):
            if now < held.releases_at:
                continue
            del self._held[client_order_id]
            self.standing.orders_released += 1
            released.append(
                self._request(
                    client_order_id, held.venue_id, held.symbol, RELEASED, held.delay,
                    held.releases_at, self._latency_estimate(held.venue_id),
                    f"{held.delay:.3f}s round trip elapsed; the order may now fill at the price "
                    f"that exists rather than the one that was on screen",
                )
            )
        return tuple(released)

    def _draw_latency(self, venue_id: str, estimate: Estimate) -> float:
        """A latency from the observed distribution, not its centre.

        The tail is what costs money: an order that arrived late into a moving
        market. Always using the median would never reproduce it.
        """
        estimator = self._latencies.get(venue_id)
        if estimator is None or not estimate.is_fitted:
            return min(self._prior, self._maximum)
        quantile = min(0.999, max(0.001, self._draw()))
        drawn = estimator.estimate(
            quantile, self._minimum_observations, bound_low=0.0, bound_high=self._maximum
        )
        return drawn.value

    def _latency_estimate(self, venue_id: str) -> Estimate:
        return self._estimator_for(venue_id).estimate(
            REPORTED_QUANTILE, self._minimum_observations, bound_low=0.0, bound_high=self._maximum
        )

    def _estimator_for(self, venue_id: str) -> QuantileEstimator:
        estimator = self._latencies.get(venue_id)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior)
            self._latencies[venue_id] = estimator
        return estimator

    def _request(
        self, client_order_id, venue_id, symbol, state, delay, releases_at, estimate, reason
    ) -> DelayedOrderRequest:
        return DelayedOrderRequest(
            client_order_id=client_order_id, venue_id=venue_id, symbol=symbol,
            state=state, delay_seconds=delay, releases_at_monotonic=releases_at,
            latency_estimate=estimate, reason=reason, decided_at_ns=self._now_ns(),
        )

    @property
    def held_count(self) -> int:
        return len(self._held)


def describe_latency(simulator: OrderLatencySimulator) -> dict:
    return {
        "part_id": PART_ID,
        "orders_held": simulator.standing.orders_held,
        "orders_released": simulator.standing.orders_released,
        "still_held": simulator.held_count,
        "live_orders_passed": simulator.standing.live_orders_passed,
        "round_trips_measured": simulator.standing.round_trips_measured,
        "venues_measured": simulator.standing.venues_measured,
        "longest_delay_seconds": simulator.standing.longest_delay_seconds,
        "latency_by_venue": {
            venue_id: {
                "p95_seconds": simulator._latency_estimate(venue_id).value,
                "is_fitted": simulator._latency_estimate(venue_id).is_fitted,
            }
            for venue_id in sorted(simulator._latencies)
        },
    }


def run_order_latency_simulator(
    simulator: OrderLatencySimulator, control_socket, read_orders, publish_delayed,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        orders = read_orders(simulator)
        held = [simulator.hold(**order) for order in orders]
        publish_delayed(tuple(held) + simulator.released_orders())

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

    Every routed order is held for a simulated round trip in paper mode and
    passed straight through in live mode, which the simulator decides from
    the money mode it is handed. Live round trips are observed by the parts
    that hold venue sockets; none exist in phase 1, so the prior stands and
    every delay says so.
    """
    from runtime.input_assembly import Batch, LatestByKey

    orders = Batch(read=context.bus.reader("order-request"))
    modes = LatestByKey(read=context.bus.reader("money-mode"), key_of=lambda m: m.segment)
    publish_delayed = context.bus.publisher_for("delayed-order-request")
    segment = str(context.setting("segment_id").value)
    simulator = OrderLatencySimulator(
        prior_latency_seconds=context.number("order_latency_prior"),
        maximum_latency_seconds=context.number("order_latency_maximum"),
        minimum_observations=int(context.number("order_latency_minimum_observations")),
        window=int(context.number("order_latency_window")),
    )

    def read_orders(_simulator):
        mode = modes.mapping().get(segment)
        money_mode = mode.mode if mode is not None else PAPER
        return tuple(
            {
                "client_order_id": order.client_order_id, "venue_id": order.venue_id,
                "symbol": order.symbol, "money_mode": money_mode,
            }
            for order in orders.payloads()
        )

    def publish(delayed) -> None:
        if delayed:
            publish_delayed(delayed)

    return run_order_latency_simulator(
        simulator=simulator,
        control_socket=context.control_socket,
        read_orders=read_orders,
        publish_delayed=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
