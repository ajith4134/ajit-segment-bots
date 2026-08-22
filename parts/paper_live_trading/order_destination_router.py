"""order-destination-router: send an order to the paper book or the live venue.

The switch that decides whether money is real. It is deliberately the dullest
part in this block -- it reads the money mode and addresses the order -- because a
router that reasoned about anything else would be a second place where the
paper/live decision could go wrong.

Two rules it does not bend:

- **No mode, no order.** An order whose money mode could not be read goes
  nowhere. Not to paper, which would silently drop a live trade; not to the
  venue, which would spend real money on an unread setting.
- **A stamped id is required.** An unstamped order cannot be retried safely, and
  something further down the line would have to invent an identity for it.

The execution schedule, where one exists, decides *when* each slice goes. The
router carries that through rather than flattening it: an order that was split to
avoid market impact must not be reassembled into one order at the destination.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-destination-router"

PART_DECLARATION = PartDeclaration(
    part_id="order-destination-router",
    consumes=("bounded-order", "money-mode", "stamped-order", "execution-schedule"),
    produces=("order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PAPER_BOOK = "paper-book"
LIVE_VENUE = "live-venue"

ROUTED = "routed"
REFUSED_NO_MODE = "refused-money-mode-unknown"
REFUSED_UNSTAMPED = "refused-order-carries-no-client-id"

PAPER = "paper"
LIVE = "live"


@dataclass(frozen=True)
class OrderRequest:
    """One order, addressed to exactly one destination."""

    client_order_id: str
    destination: str
    venue_id: str
    symbol: str
    side: str
    quantity: float
    limit_price: float
    stop_price: float
    slice_sequence: int
    slice_count: int
    at_second: float
    outcome: str
    reason: str
    routed_at_ns: int

    @property
    def is_live_money(self) -> bool:
        return self.destination == LIVE_VENUE

    @property
    def may_be_sent(self) -> bool:
        return self.outcome == ROUTED and self.quantity > 0


@dataclass
class RouterStanding:
    routed_to_paper: int = 0
    routed_to_live: int = 0
    refused_no_mode: int = 0
    refused_unstamped: int = 0
    slices_routed: int = 0
    orders_split: int = 0


class OrderDestinationRouter:
    """Addresses each order by money mode, preserving any execution schedule."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self.standing = RouterStanding()

    def route(self, stamped_order, money_mode, execution_schedule=None) -> tuple[OrderRequest, ...]:
        """One request per slice, or a single refusal naming what was missing."""
        if money_mode is None:
            self.standing.refused_no_mode += 1
            return (
                self._refusal(
                    stamped_order, REFUSED_NO_MODE,
                    "the money mode could not be read; paper would silently drop a live trade "
                    "and live would spend real money on an unread setting",
                ),
            )

        if not getattr(stamped_order, "client_order_id", None):
            self.standing.refused_unstamped += 1
            return (
                self._refusal(
                    stamped_order, REFUSED_UNSTAMPED,
                    "this order carries no client id, so it could not be retried safely",
                ),
            )

        destination = LIVE_VENUE if money_mode.mode == LIVE else PAPER_BOOK
        slices = self._slices_of(stamped_order, execution_schedule)
        if len(slices) > 1:
            self.standing.orders_split += 1

        requests = []
        for index, (quantity, at_second) in enumerate(slices, start=1):
            if destination == LIVE_VENUE:
                self.standing.routed_to_live += 1
            else:
                self.standing.routed_to_paper += 1
            self.standing.slices_routed += 1
            requests.append(
                OrderRequest(
                    client_order_id=(
                        stamped_order.client_order_id
                        if len(slices) == 1
                        else f"{stamped_order.client_order_id}-{index}"
                    ),
                    destination=destination,
                    venue_id=stamped_order.venue_id,
                    symbol=stamped_order.symbol,
                    side=stamped_order.side,
                    quantity=quantity,
                    limit_price=stamped_order.entry_price,
                    stop_price=stamped_order.stop_price,
                    slice_sequence=index,
                    slice_count=len(slices),
                    at_second=at_second,
                    outcome=ROUTED,
                    reason=(
                        f"money mode is {money_mode.mode}"
                        + (f", slice {index} of {len(slices)}" if len(slices) > 1 else "")
                    ),
                    routed_at_ns=self._now_ns(),
                )
            )
        return tuple(requests)

    def _slices_of(self, stamped_order, execution_schedule) -> tuple[tuple[float, float], ...]:
        """The schedule's slices, or one slice for the whole order.

        A split order is never reassembled here: it was split to avoid market
        impact, and putting it back together would spend exactly what the split
        was meant to save.
        """
        if execution_schedule is None or not getattr(execution_schedule, "slices", ()):
            return ((stamped_order.quantity, 0.0),)
        return tuple(
            (one.quantity, one.at_second) for one in execution_schedule.slices
        )

    def _refusal(self, stamped_order, outcome, reason) -> OrderRequest:
        return OrderRequest(
            client_order_id=getattr(stamped_order, "client_order_id", "") or "",
            destination="",
            venue_id=getattr(stamped_order, "venue_id", ""),
            symbol=getattr(stamped_order, "symbol", ""),
            side=getattr(stamped_order, "side", ""),
            quantity=0.0,
            limit_price=0.0,
            stop_price=0.0,
            slice_sequence=0,
            slice_count=0,
            at_second=0.0,
            outcome=outcome,
            reason=reason,
            routed_at_ns=self._now_ns(),
        )


def describe_routing(router: OrderDestinationRouter) -> dict:
    return {
        "part_id": PART_ID,
        "routed_to_paper": router.standing.routed_to_paper,
        "routed_to_live": router.standing.routed_to_live,
        "refused_no_mode": router.standing.refused_no_mode,
        "refused_unstamped": router.standing.refused_unstamped,
        "slices_routed": router.standing.slices_routed,
        "orders_split": router.standing.orders_split,
    }


def run_order_destination_router(
    router: OrderDestinationRouter, control_socket, read_orders, publish_requests,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        requests = []
        for stamped_order, money_mode, schedule in read_orders():
            requests.extend(router.route(stamped_order, money_mode, schedule))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
