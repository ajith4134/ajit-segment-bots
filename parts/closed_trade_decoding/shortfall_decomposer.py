"""shortfall-decomposer: the gap between the assumed price and the achieved one.

"Slippage" as one number is unactionable, because its three causes have three
different fixes and they are frequently in opposite directions. This part splits it:

- **Spread cost** -- the half-spread that had to be crossed. Fixed by choosing more
  liquid symbols or by not crossing, and it is the component that a limit order
  removes and a market order always pays.
- **Delay cost** -- the price moved between the decision and the order arriving.
  Fixed by being faster, and this is the component that a system optimising its
  order type will never touch because it is not about the order at all.
- **Impact cost** -- the price moved because of this order's own size. Fixed by
  trading smaller or slower, and it is the only component that grows with size.

The decomposition is the standard implementation-shortfall split, and its value here
is diagnostic rather than academic: a system paying mostly spread should change what
it trades, one paying mostly delay should change how fast it decides, and one paying
mostly impact should change how large it goes. Reporting one blended number sends
all three to the wrong fix.

Two properties keep it honest. **The decision price must be recorded at decision
time**, not reconstructed afterwards -- a reconstructed decision price is the
arrival price wearing a different name, and it makes delay cost vanish. And **the
three components sum to the total shortfall**, so nothing is quietly dropped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import ShortfallBreakdown
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "shortfall-decomposer"

PART_DECLARATION = PartDeclaration(
    part_id="shortfall-decomposer",
    consumes=("closed-trade", "fill", "bounded-order", "order-book-snapshot", "market-data"),
    produces=("shortfall-breakdown", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DECOMPOSED = "decomposed"
NO_DECISION_PRICE = "the-price-the-decision-assumed-was-never-recorded"
NO_ARRIVAL_PRICE = "the-price-when-the-order-arrived-was-never-recorded"
NO_FILL = "nothing-was-filled"


@dataclass(frozen=True)
class ShortfallOutcome:
    trade_id: str
    state: str
    breakdown: ShortfallBreakdown | None
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == DECOMPOSED and self.breakdown is not None


@dataclass
class DecomposerStanding:
    orders_examined: int = 0
    decomposed: int = 0
    without_a_decision_price: int = 0
    without_an_arrival_price: int = 0
    without_a_fill: int = 0
    total_spread_cost: float = 0.0
    total_delay_cost: float = 0.0
    total_impact_cost: float = 0.0
    times_delay_was_the_largest: int = 0
    times_impact_was_the_largest: int = 0
    times_spread_was_the_largest: int = 0


class ShortfallDecomposer:
    """Splits implementation shortfall into spread, delay and impact."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._decision: dict[str, tuple] = {}
        self._arrival: dict[str, tuple] = {}
        self._fills: dict[str, list] = {}
        self.standing = DecomposerStanding()

    def observe_decision(
        self, order_id: str, price: float, decided_at_ns: int, side: str,
    ) -> None:
        """Recorded at decision time. Reconstructed later, this becomes the arrival
        price under a different name and delay cost vanishes."""
        self._decision[order_id] = (price, decided_at_ns, side)

    def observe_arrival(
        self, order_id: str, mid_price: float, half_spread: float, arrived_at_ns: int,
    ) -> None:
        self._arrival[order_id] = (mid_price, half_spread, arrived_at_ns)

    def observe_fill(self, order_id: str, price: float, quantity: float) -> None:
        self._fills.setdefault(order_id, []).append((price, quantity))

    def decompose(self, order_id: str, trade_id: str) -> ShortfallOutcome:
        self.standing.orders_examined += 1
        decision = self._decision.get(order_id)
        if decision is None:
            self.standing.without_a_decision_price += 1
            return self._outcome(
                trade_id, NO_DECISION_PRICE, None,
                "the price the decision assumed was never recorded. Reconstructing it "
                "afterwards produces the arrival price under a different name, which "
                "makes delay cost vanish exactly when it is largest",
            )

        arrival = self._arrival.get(order_id)
        if arrival is None:
            self.standing.without_an_arrival_price += 1
            return self._outcome(
                trade_id, NO_ARRIVAL_PRICE, None,
                "the price when the order reached the venue was never recorded, so delay "
                "and impact cannot be separated",
            )

        fills = self._fills.get(order_id, [])
        if not fills:
            self.standing.without_a_fill += 1
            return self._outcome(
                trade_id, NO_FILL, None,
                "nothing was filled, so there is no achieved price. An unfilled order has "
                "an opportunity cost, which is a different measurement",
            )

        decision_price, _, side = decision
        arrival_mid, half_spread, _ = arrival
        quantity = sum(size for _, size in fills)
        achieved = sum(price * size for price, size in fills) / quantity

        # A buy pays when prices rise; a sell pays when they fall.
        sign = 1.0 if side == "buy" else -1.0

        delay_cost = sign * (arrival_mid - decision_price) * quantity
        spread_cost = half_spread * quantity
        # Whatever is left of the total after delay and the spread that had to be
        # crossed is what this order's own size moved the price by.
        total = sign * (achieved - decision_price) * quantity
        impact_cost = total - delay_cost - spread_cost

        breakdown = ShortfallBreakdown(
            trade_id=trade_id,
            decision_price=decision_price,
            arrival_price=arrival_mid,
            achieved_price=achieved,
            spread_cost=spread_cost,
            delay_cost=delay_cost,
            impact_cost=impact_cost,
            total_shortfall=total,
            quantity=quantity,
            reason="",
            measured_at_ns=self._now_ns(),
        )

        self.standing.decomposed += 1
        self.standing.total_spread_cost += spread_cost
        self.standing.total_delay_cost += delay_cost
        self.standing.total_impact_cost += impact_cost
        largest = breakdown.largest_cause
        if largest == "delay":
            self.standing.times_delay_was_the_largest += 1
        elif largest == "impact":
            self.standing.times_impact_was_the_largest += 1
        else:
            self.standing.times_spread_was_the_largest += 1

        return self._outcome(
            trade_id, DECOMPOSED,
            ShortfallBreakdown(
                trade_id=breakdown.trade_id, decision_price=breakdown.decision_price,
                arrival_price=breakdown.arrival_price,
                achieved_price=breakdown.achieved_price,
                spread_cost=breakdown.spread_cost, delay_cost=breakdown.delay_cost,
                impact_cost=breakdown.impact_cost,
                total_shortfall=breakdown.total_shortfall, quantity=breakdown.quantity,
                reason=(
                    f"{total:+.4f} total: spread {spread_cost:+.4f}, delay "
                    f"{delay_cost:+.4f}, impact {impact_cost:+.4f}. Mostly {largest}, "
                    + {
                        "spread": "which is fixed by trading more liquid symbols or not "
                                  "crossing",
                        "delay": "which is fixed by deciding faster and is untouched by "
                                 "any change of order type",
                        "impact": "which is fixed by trading smaller or slower, and is the "
                                  "only component that grows with size",
                    }[largest]
                ),
                measured_at_ns=breakdown.measured_at_ns,
            ),
            f"decomposed into spread, delay and impact, summing to {total:+.4f}",
        )

    def _outcome(self, trade_id, state, breakdown, reason) -> ShortfallOutcome:
        return ShortfallOutcome(
            trade_id=trade_id, state=state, breakdown=breakdown, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_shortfall(decomposer: ShortfallDecomposer) -> dict:
    return {
        "part_id": PART_ID,
        "orders_examined": decomposer.standing.orders_examined,
        "decomposed": decomposer.standing.decomposed,
        "without_a_decision_price": decomposer.standing.without_a_decision_price,
        "without_an_arrival_price": decomposer.standing.without_an_arrival_price,
        "without_a_fill": decomposer.standing.without_a_fill,
        "total_spread_cost": decomposer.standing.total_spread_cost,
        "total_delay_cost": decomposer.standing.total_delay_cost,
        "total_impact_cost": decomposer.standing.total_impact_cost,
        "times_spread_was_the_largest": decomposer.standing.times_spread_was_the_largest,
        "times_delay_was_the_largest": decomposer.standing.times_delay_was_the_largest,
        "times_impact_was_the_largest": decomposer.standing.times_impact_was_the_largest,
        "reports_one_blended_slippage_number": False,
        "reconstructs_the_decision_price": False,
    }


def run_shortfall_decomposer(
    decomposer: ShortfallDecomposer, control_socket, read_orders, publish_breakdowns,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for order_id, trade_id in read_orders():
            outcome = decomposer.decompose(order_id, trade_id)
            if outcome.is_usable:
                publish_breakdowns(outcome.breakdown)

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

    A bounded order is the decision, the book at the time is the arrival,
    the fill is what was achieved; the three are keyed by the order's intent
    id and decomposed when the trade it belongs to closes.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.order_book import OrderBookSnapshot
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    fills = Batch(read=context.bus.reader("fill"))
    orders = Batch(read=context.bus.reader("bounded-order"))
    books = LatestByKey(read=context.bus.reader("order-book-snapshot"), key_of=lambda b: (b.venue_id, b.symbol))
    trades = Batch(read=context.bus.reader("market-data"))
    publish_breakdowns = context.bus.publisher_for("shortfall-breakdown")
    decomposer = ShortfallDecomposer()
    order_of_symbol: dict[tuple[str, str], list] = {}

    def read_orders():
        trades.payloads()
        latest_books = books.mapping()
        for order in orders.payloads():
            if not order.intent_id:
                continue
            decomposer.observe_decision(order.intent_id, order.entry_price, order.bounded_at_ns, order.side)
            book = latest_books.get((order.venue_id, order.symbol))
            if isinstance(book, OrderBookSnapshot) and book.best_bid and book.best_ask:
                mid = (book.best_bid + book.best_ask) / 2.0
                decomposer.observe_arrival(order.intent_id, mid, (book.best_ask - book.best_bid) / 2.0, book.venue_time_ns)
            order_of_symbol.setdefault((order.venue_id, order.symbol), []).append(order.intent_id)
        for fill in fills.payloads():
            if fill.order_id:
                decomposer.observe_fill(fill.order_id, fill.price, fill.quantity)
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            for order_id in order_of_symbol.pop((trade.venue_id, trade.symbol), []):
                jobs.append((order_id, trade_id))
        return tuple(jobs)

    return run_shortfall_decomposer(
        decomposer=decomposer,
        control_socket=context.control_socket,
        read_orders=read_orders,
        publish_breakdowns=lambda b: publish_breakdowns((b,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
