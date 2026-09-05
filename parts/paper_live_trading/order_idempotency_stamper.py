"""order-idempotency-stamper: one stable client id per order, stamped once.

Stamped here, before the order can reach any destination, so that every later
part -- the router, the resubmitter, the paper book -- is talking about the same
order rather than about a request that happens to look similar.

The id is derived from the order's own content plus the intent that produced it,
which gives the two properties that matter and are in tension:

- **The same order retried is the same id**, so a venue that already has it
  rejects the duplicate rather than opening a second position.
- **Two genuinely different decisions asking for identical orders are different
  ids**, so a strategy that legitimately wants to buy the same thing twice can.

Stamped **once**: re-stamping an order that already carries an id would break the
first property at exactly the moment it is needed, which is a retry.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import UNLEVERED, leverage_behind

PART_ID = "order-idempotency-stamper"

PART_DECLARATION = PartDeclaration(
    part_id="order-idempotency-stamper",
    consumes=("bounded-order",),
    produces=("stamped-order", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Both venues cap the client id field. A truncated SHA-256 at this width has a
# collision probability far below the number of orders this system could place
# in its lifetime. A wire-format width, not a decision number.
CLIENT_ID_LENGTH = 32

STAMPED = "stamped"
ALREADY_STAMPED = "already-stamped"


@dataclass(frozen=True)
class StampedOrder:
    """A bounded order with the id it will carry everywhere, forever."""

    client_order_id: str
    venue_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    intent_id: str
    outcome: str
    reason: str
    stamped_at_ns: int
    # What the desk sized this order at. Stamping is about identity and changes
    # nothing about the order, so the leverage travels through untouched -- and it
    # has to travel, because the account that pays for the fill computes what the
    # position ties up as its notional over this number and a fill states only a
    # price and a quantity, which are the same at 1x and at 10x.
    leverage: float = UNLEVERED
    # Which segment's money this is. Three segment bots share one spine since
    # 2026-09-05, and every part further along that holds money -- the capital
    # bounds, the money mode, the account that pays for the fill -- publishes one
    # level per segment. An order that did not carry its own would be matched
    # against whichever segment's level arrived last, which is a wrong answer that
    # reports nothing. Empty means the producer named no segment, which is what a
    # spine trading one segment looked like before this.
    segment: str = ""


@dataclass
class StamperStanding:
    stamped: int = 0
    already_stamped: int = 0
    collisions_seen: int = 0
    distinct_ids: int = 0
    # Orders stamped from their own numbers because no decision id travelled with
    # them. Counted because that id is what makes a republished intent one order
    # rather than one per tick, and its absence is not visible any other way.
    stamped_without_a_decision: int = 0


class OrderIdempotencyStamper:
    """Gives each order one id, derived from what the order is."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._issued: dict[str, tuple] = {}
        self.standing = StamperStanding()

    def client_order_id(
        self, venue_id: str, symbol: str, side: str, quantity: float,
        entry_price: float, intent_id: str,
    ) -> str:
        """The id for one order, computed the same way every time it is asked.

        **From the decision, never from the market.** Quantity and entry price
        were part of this hash until 2026-08-23, and both move on every print --
        so a standing intent, republished tick after tick as the arbiter is
        designed to republish it, produced a different id every second and the
        book filled each one as a new order. Measured on the live run at 09:01: a
        single ENAUSDT long became 13 orders and 13 fills, 12,982 USDT of notional
        against a 1,000 per-trade cap. The venue would have done exactly the same.

        Where no decision id is supplied the order's own numbers are still used,
        because an id derived from a partial key would collide across genuinely
        different orders -- but a caller in that position is asking for a
        best-effort id, and the standing counts it.
        """
        if intent_id:
            body = f"{intent_id}"
        else:
            self.standing.stamped_without_a_decision += 1
            body = f"{venue_id}|{symbol}|{side}|{quantity!r}|{entry_price!r}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:CLIENT_ID_LENGTH]

    def stamp(self, bounded_order, intent_id: str, existing_id: str | None = None) -> StampedOrder:
        """Stamp an order, or hand back the id it already carries.

        `existing_id` is what a caller does when replaying or retrying: the order
        keeps the identity it was first given, which is the entire point.
        """
        if existing_id:
            self.standing.already_stamped += 1
            return self._stamped(
                existing_id, bounded_order, intent_id, ALREADY_STAMPED,
                "this order already carries an id; re-stamping would break a retry",
            )

        client_order_id = self.client_order_id(
            bounded_order.venue_id, bounded_order.symbol, bounded_order.side,
            bounded_order.quantity, bounded_order.entry_price, intent_id,
        )

        fingerprint = (
            bounded_order.venue_id, bounded_order.symbol, bounded_order.side,
            bounded_order.quantity, bounded_order.entry_price, intent_id,
        )
        previous = self._issued.get(client_order_id)
        if previous is not None and previous != fingerprint:
            # Two different orders producing one id would let a venue reject a
            # real order as a duplicate. Recorded rather than assumed impossible.
            self.standing.collisions_seen += 1
        self._issued[client_order_id] = fingerprint
        self.standing.stamped += 1
        self.standing.distinct_ids = len(self._issued)

        return self._stamped(
            client_order_id, bounded_order, intent_id, STAMPED,
            "derived from the order's own content and its intent",
        )

    def _stamped(self, client_order_id, order, intent_id, outcome, reason) -> StampedOrder:
        return StampedOrder(
            client_order_id=client_order_id,
            venue_id=order.venue_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            entry_price=order.entry_price,
            stop_price=order.stop_price,
            intent_id=intent_id,
            outcome=outcome,
            reason=reason,
            stamped_at_ns=self._now_ns(),
            leverage=leverage_behind(order),
            # Straight through. Stamping is about identity and changes nothing
            # about the order, and the segment has to survive every step between
            # the sizer and the venue for the same reason the leverage does: the
            # money mode the router reads is one level per segment (2026-09-05).
            segment=getattr(order, "segment", ""),
        )


def describe_stamping(stamper: OrderIdempotencyStamper) -> dict:
    return {
        "part_id": PART_ID,
        "stamped": stamper.standing.stamped,
        "already_stamped": stamper.standing.already_stamped,
        "distinct_ids": stamper.standing.distinct_ids,
        "collisions_seen": stamper.standing.collisions_seen,
    }


def run_order_idempotency_stamper(
    stamper: OrderIdempotencyStamper, control_socket, read_bounded_orders, publish_stamped,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_stamped(
            tuple(
                stamper.stamp(order, intent_id, existing)
                for order, intent_id, existing in read_bounded_orders()
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
        read_standing=lambda: describe_stamping(stamper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Gives each order the identity that makes a retry safe. The id it stamps is
    derived from the order rather than from a counter, so the same order stamped
    twice carries the same id and the venue rejects the duplicate instead of filling
    it -- which is the whole reason this part exists between the gate and the router.
    """
    from runtime.input_assembly import Batch

    bounded = Batch(read=context.bus.reader("bounded-order"))
    publish_stamped = context.bus.publisher_for("stamped-order")

    def read_bounded_orders():
        # The intent id and any existing id travel with the order itself. The
        # decision id is set by position-sizer from the intent and carried through
        # the bounds gate, so an order for a standing intent keeps one identity
        # however many times that intent is republished.
        return tuple(
            (order, getattr(order, "intent_id", "") or None,
             getattr(order, "client_order_id", None))
            for order in bounded.payloads()
        )

    return run_order_idempotency_stamper(
        stamper=OrderIdempotencyStamper(),
        control_socket=context.control_socket,
        read_bounded_orders=read_bounded_orders,
        publish_stamped=publish_stamped,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
