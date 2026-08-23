"""limit-price-walker: step an unfilled limit order toward the market, a little at a time.

The alternative to walking is crossing the spread immediately, which pays the
full spread on every order. Walking pays some of it and sometimes none -- but a
walk that is too eager is just a slow market order, and one that is too timid
never fills and locks capital instead.

So the step size is **learned** (RL-060): the part records how far each of its
orders had to walk before it filled, and steps by what has actually been enough
on that symbol. It also learns from what did *not* work -- an order cancelled
without filling is evidence the walk was too timid, and that is recorded
separately so the two are never confused.

Three bounds the walk may never cross, in order of severity:

- **Never through the touch.** A buy walking past the best ask is a market order
  wearing a limit order's name, and it would cross the spread it exists to save.
- **Never past the operator's ceiling** on total movement from the original price
  (RL-061): estimation may not exceed a stated bound.
- **Never faster than the cadence**, because each step is a venue request and the
  rate budget is shared with everything else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY

PART_ID = "limit-price-walker"

PART_DECLARATION = PartDeclaration(
    part_id="limit-price-walker",
    consumes=("order-request", "market-data", "order-book-snapshot"),
    produces=("order-reprice", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The quantile of walks that filled, used as the step. Chosen above the median
# because a step short of what usually works is a step that must be repeated,
# and each repeat is another venue request and another cadence wait.
FILLED_WALK_QUANTILE = 0.75

HELD = "held"
STEPPED = "stepped"
AT_TOUCH = "held-at-touch"
AT_CEILING = "held-at-operator-ceiling"


@dataclass(frozen=True)
class OrderReprice:
    """One step of one order's price, or the reason it did not move."""

    order_id: str
    venue_id: str
    symbol: str
    action: str
    from_price: float
    to_price: float
    step: float
    total_walked_fraction: float
    step_estimate: Estimate
    reason: str
    decided_at_ns: int

    @property
    def did_move(self) -> bool:
        return self.action == STEPPED


@dataclass
class _WalkingOrder:
    venue_id: str
    symbol: str
    side: str
    original_price: float
    current_price: float
    placed_at_monotonic: float
    last_step_monotonic: float
    steps_taken: int = 0


@dataclass
class WalkerStanding:
    orders_walking: int = 0
    steps_taken: int = 0
    held_at_touch: int = 0
    held_at_ceiling: int = 0
    filled_walks_learned: int = 0
    unfilled_walks_learned: int = 0
    symbols_fitted: int = 0


class LimitPriceWalker:
    """Steps an order's price toward the touch by a learned amount, on a cadence."""

    def __init__(
        self,
        cadence_seconds: float,
        prior_step_fraction: float,
        maximum_total_walk_fraction: float,
        minimum_observations: int,
        window: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_total_walk_fraction <= 0:
            raise ValueError("an order that may not move at all cannot be walked")
        self._cadence = cadence_seconds
        self._prior_step = prior_step_fraction
        self._maximum_walk = maximum_total_walk_fraction
        self._minimum_observations = minimum_observations
        self._window = window
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._orders: dict[str, _WalkingOrder] = {}
        self._touch: dict[tuple[str, str], tuple[float, float]] = {}
        self._filled_walks: dict[tuple[str, str], QuantileEstimator] = {}
        self._unfilled_walks: dict[tuple[str, str], QuantileEstimator] = {}
        self.standing = WalkerStanding()

    def observe_order_placed(
        self, order_id: str, venue_id: str, symbol: str, side: str, limit_price: float
    ) -> None:
        now = self._monotonic()
        self._orders[order_id] = _WalkingOrder(
            venue_id=venue_id, symbol=symbol, side=side,
            original_price=limit_price, current_price=limit_price,
            placed_at_monotonic=now, last_step_monotonic=now,
        )
        self.standing.orders_walking = len(self._orders)

    def observe_touch(self, venue_id: str, symbol: str, best_bid: float, best_ask: float) -> None:
        self._touch[(venue_id, symbol)] = (best_bid, best_ask)

    def observe_order_filled(self, order_id: str) -> None:
        """How far this order had to walk before it filled -- the positive evidence."""
        order = self._orders.pop(order_id, None)
        if order is None:
            return
        walked = abs(order.current_price - order.original_price) / order.original_price
        self._filled_estimator((order.venue_id, order.symbol)).observe(walked)
        self.standing.filled_walks_learned += 1
        self.standing.orders_walking = len(self._orders)

    def observe_order_cancelled(self, order_id: str) -> None:
        """A walk that never filled -- evidence the step was too timid, kept apart."""
        order = self._orders.pop(order_id, None)
        if order is None:
            return
        walked = abs(order.current_price - order.original_price) / order.original_price
        self._unfilled_estimator((order.venue_id, order.symbol)).observe(walked)
        self.standing.unfilled_walks_learned += 1
        self.standing.orders_walking = len(self._orders)

    def step(self, order_id: str) -> OrderReprice | None:
        order = self._orders.get(order_id)
        if order is None:
            return None

        key = (order.venue_id, order.symbol)
        estimate = self._step_estimate(key)
        now = self._monotonic()

        if now - order.last_step_monotonic < self._cadence:
            return self._reprice(order_id, order, HELD, order.current_price, estimate, "within the cadence")

        touch = self._touch.get(key)
        if touch is None:
            return self._reprice(
                order_id, order, HELD, order.current_price, estimate,
                "no touch for this symbol, so there is nothing to walk toward",
            )

        best_bid, best_ask = touch
        step = order.current_price * estimate.value
        proposed = order.current_price + step if order.side == BUY else order.current_price - step

        # Never through the touch: that would cross the spread this exists to save.
        if order.side == BUY and proposed >= best_ask:
            proposed = best_ask
            if order.current_price >= best_ask:
                self.standing.held_at_touch += 1
                return self._reprice(order_id, order, AT_TOUCH, order.current_price, estimate,
                                     "already at the ask; stepping further would cross the spread")
        if order.side != BUY and proposed <= best_bid:
            proposed = best_bid
            if order.current_price <= best_bid:
                self.standing.held_at_touch += 1
                return self._reprice(order_id, order, AT_TOUCH, order.current_price, estimate,
                                     "already at the bid; stepping further would cross the spread")

        walked = abs(proposed - order.original_price) / order.original_price
        if walked > self._maximum_walk:
            self.standing.held_at_ceiling += 1
            return self._reprice(
                order_id, order, AT_CEILING, order.current_price, estimate,
                f"a further step would walk {walked:.2%} from the original price, past the "
                f"operator's ceiling of {self._maximum_walk:.2%}",
            )

        order.current_price = proposed
        order.last_step_monotonic = now
        order.steps_taken += 1
        self.standing.steps_taken += 1
        return self._reprice(
            order_id, order, STEPPED, proposed, estimate,
            f"step {order.steps_taken} of {estimate.value:.3%} toward the touch "
            f"({'learned' if estimate.is_fitted else 'the operator prior'})",
        )

    def step_all(self) -> tuple[OrderReprice, ...]:
        return tuple(
            reprice for order_id in list(self._orders) if (reprice := self.step(order_id)) is not None
        )

    def _step_estimate(self, key) -> Estimate:
        return self._filled_estimator(key).estimate(
            FILLED_WALK_QUANTILE,
            self._minimum_observations,
            bound_low=0.0,
            bound_high=self._maximum_walk,
        )

    def _filled_estimator(self, key) -> QuantileEstimator:
        estimator = self._filled_walks.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_step)
            self._filled_walks[key] = estimator
        return estimator

    def _unfilled_estimator(self, key) -> QuantileEstimator:
        estimator = self._unfilled_walks.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_step)
            self._unfilled_walks[key] = estimator
        return estimator

    def _reprice(self, order_id, order, action, to_price, estimate, reason) -> OrderReprice:
        return OrderReprice(
            order_id=order_id,
            venue_id=order.venue_id,
            symbol=order.symbol,
            action=action,
            from_price=order.original_price,
            to_price=to_price,
            step=abs(to_price - order.original_price),
            total_walked_fraction=abs(to_price - order.original_price) / order.original_price,
            step_estimate=estimate,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_walking(walker: LimitPriceWalker) -> dict:
    fitted = sum(
        1
        for key in walker._filled_walks
        if walker._step_estimate(key).is_fitted
    )
    return {
        "part_id": PART_ID,
        "orders_walking": walker.standing.orders_walking,
        "steps_taken": walker.standing.steps_taken,
        "held_at_touch": walker.standing.held_at_touch,
        "held_at_operator_ceiling": walker.standing.held_at_ceiling,
        "filled_walks_learned": walker.standing.filled_walks_learned,
        "unfilled_walks_learned": walker.standing.unfilled_walks_learned,
        "symbols_with_a_fitted_step": fitted,
    }


def run_limit_price_walker(
    walker: LimitPriceWalker, control_socket, read_events, publish_reprices,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(walker)
        publish_reprices(walker.step_all())

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
    from runtime.order_book import OrderBookSnapshot
    from runtime.venues.venue_adapter import NormalisedTrade

    orders = Batch(read=context.bus.reader("order-request"))
    trades = Batch(read=context.bus.reader("market-data"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    publish_reprices = context.bus.publisher_for("order-reprice")
    walker = LimitPriceWalker(
        cadence_seconds=context.number("limit_walk_cadence"),
        prior_step_fraction=context.number("limit_walk_prior_step_fraction"),
        maximum_total_walk_fraction=context.number("limit_walk_maximum_total_fraction"),
        minimum_observations=int(context.number("execution_minimum_observations")),
        window=int(context.number("execution_window")),
    )

    def read_events(_walker) -> None:
        for order in orders.payloads():
            if order.order_type == "limit" and order.limit_price > 0 and order.outcome == "routed":
                walker.observe_order_placed(order.client_order_id, order.venue_id, order.symbol, order.side, order.limit_price)
            elif order.cancels_client_order_id:
                walker.observe_order_cancelled(order.cancels_client_order_id)
        for book in books.payloads():
            if isinstance(book, OrderBookSnapshot) and book.best_bid and book.best_ask:
                walker.observe_touch(book.venue_id, book.symbol, book.best_bid, book.best_ask)
        trades.payloads()

    def publish(reprices) -> None:
        kept = tuple(item for item in reprices if item is not None)
        if kept:
            publish_reprices(kept)

    return run_limit_price_walker(
        walker=walker,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_reprices=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
