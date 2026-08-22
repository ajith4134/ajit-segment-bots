"""ccxt-order-router: place each order on its venue, through one interface.

The part that spends money. Everything about it is shaped by that.

**Nothing is sent without a client order id.** The id is derived from the order's
own content, so the same order request produces the same id however many times it
is retried. That is what makes a resubmission safe: a venue that already has the
order rejects the duplicate rather than opening a second position, and a network
timeout -- where the order may or may not have arrived -- stops being unrecoverable.
Without idempotency, "did that order go through?" has no safe answer and both
answers are expensive.

**Nothing is sent without budget.** The rate budget is checked before the call,
not after, because the cost of overrunning is a per-IP ban that outlasts the part.

**Nothing is sent to a venue that is refusing us**, and nothing is sent for a key
that is standing down.

The routing itself goes through ccxt, which is what it was admitted for: one
unified `create_order` across venues, so a third venue is an adapter rather than
another HTTP client. The tape's stream half is deliberately not ccxt's (spec
§2.2), and this is the other half of that same decision.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "ccxt-order-router"

PART_DECLARATION = PartDeclaration(
    part_id="ccxt-order-router",
    consumes=("order-request", "venue-rate-budget", "cancel-decision", "order-reprice", "key-standing"),
    produces=("raw-venue-order-status", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PLACE = "place"
CANCEL = "cancel"
REPRICE = "reprice"

SENT = "sent"
REFUSED_NO_BUDGET = "refused-no-rate-budget"
REFUSED_VENUE_STANDING = "refused-venue-standing"
REFUSED_NO_KEY = "refused-no-key"
REFUSED_DUPLICATE = "refused-already-sent"
FAILED = "failed"

# How many characters of the content digest become the client order id. Both
# venues cap this field, and a truncated SHA-256 at this width has a collision
# probability far below the number of orders this system could place in its
# lifetime. A wire-format width, not a decision number.
CLIENT_ORDER_ID_LENGTH = 32


@dataclass(frozen=True)
class RawVenueOrderStatus:
    """What the venue said back, unmodified, plus how the request went out."""

    client_order_id: str
    venue_id: str
    symbol: str
    action: str
    outcome: str
    venue_response: dict | None
    reason: str
    requested_at_ns: int
    responded_at_ns: int | None


@dataclass
class RouterStanding:
    placed: int = 0
    cancelled: int = 0
    repriced: int = 0
    refused_no_budget: int = 0
    refused_standing: int = 0
    refused_no_key: int = 0
    duplicates_refused: int = 0
    failures: int = 0
    last_failure: str | None = None
    by_venue: dict = field(default_factory=dict)


class CcxtOrderRouter:
    """Sends orders through a venue client, idempotently and within budget."""

    def __init__(
        self,
        clients: dict[str, object],
        has_rate_budget,
        read_venue_standing,
        read_key_standing,
        now_ns=time.time_ns,
    ) -> None:
        self._clients = clients
        self._has_rate_budget = has_rate_budget
        self._read_venue_standing = read_venue_standing
        self._read_key_standing = read_key_standing
        self._now_ns = now_ns
        self._sent: dict[str, str] = {}
        self.standing = RouterStanding()

    def client_order_id(
        self, venue_id: str, symbol: str, side: str, quantity: float, price: float | None, intent_id: str
    ) -> str:
        """An id derived from the order itself, so a retry is the same order.

        The intent id is included because two genuinely different decisions may
        ask for an identical order, and those must not collide into one -- while
        one decision retried must.
        """
        body = f"{venue_id}|{symbol}|{side}|{quantity!r}|{price!r}|{intent_id}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:CLIENT_ORDER_ID_LENGTH]

    def place(
        self,
        venue_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float | None,
        intent_id: str,
        order_type: str = "limit",
        reduce_only: bool = False,
    ) -> RawVenueOrderStatus:
        client_order_id = self.client_order_id(venue_id, symbol, side, quantity, price, intent_id)
        requested_at = self._now_ns()

        refusal = self._refusal_for(venue_id, client_order_id, PLACE)
        if refusal is not None:
            return refusal

        client = self._clients.get(venue_id)
        if client is None:
            return self._status(
                client_order_id, venue_id, symbol, PLACE, FAILED, None,
                f"no client is configured for {venue_id}", requested_at,
            )

        self._sent[client_order_id] = venue_id
        try:
            response = client.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=quantity,
                price=price,
                params={"clientOrderId": client_order_id, "reduceOnly": reduce_only},
            )
        except Exception as failure:
            # The order may still have reached the venue. The client order id is
            # what makes that recoverable: the next poll finds it by that id, or
            # a resubmission is refused as a duplicate rather than doubling up.
            self.standing.failures += 1
            self.standing.last_failure = f"{venue_id} {symbol}: {type(failure).__name__}: {failure}"
            return self._status(
                client_order_id, venue_id, symbol, PLACE, FAILED, None,
                f"{type(failure).__name__}: {failure}; the order may or may not have arrived, "
                f"and is findable by its client order id either way",
                requested_at,
            )

        self.standing.placed += 1
        self.standing.by_venue[venue_id] = self.standing.by_venue.get(venue_id, 0) + 1
        return self._status(
            client_order_id, venue_id, symbol, PLACE, SENT, response, "accepted by the venue", requested_at
        )

    def cancel(self, client_order_id: str, venue_id: str, symbol: str) -> RawVenueOrderStatus:
        requested_at = self._now_ns()
        client = self._clients.get(venue_id)
        if client is None:
            return self._status(
                client_order_id, venue_id, symbol, CANCEL, FAILED, None,
                f"no client is configured for {venue_id}", requested_at,
            )
        try:
            response = client.cancel_order(id=None, symbol=symbol, params={"clientOrderId": client_order_id})
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"cancel {client_order_id}: {type(failure).__name__}: {failure}"
            return self._status(
                client_order_id, venue_id, symbol, CANCEL, FAILED, None,
                f"{type(failure).__name__}: {failure}", requested_at,
            )
        self.standing.cancelled += 1
        return self._status(
            client_order_id, venue_id, symbol, CANCEL, SENT, response, "cancel accepted", requested_at
        )

    def reprice(
        self, client_order_id: str, venue_id: str, symbol: str, new_price: float
    ) -> RawVenueOrderStatus:
        """Move a resting order's price, which every venue does as an edit or a replace."""
        requested_at = self._now_ns()
        client = self._clients.get(venue_id)
        if client is None:
            return self._status(
                client_order_id, venue_id, symbol, REPRICE, FAILED, None,
                f"no client is configured for {venue_id}", requested_at,
            )
        if not self._has_rate_budget(venue_id):
            self.standing.refused_no_budget += 1
            return self._status(
                client_order_id, venue_id, symbol, REPRICE, REFUSED_NO_BUDGET, None,
                "no rate budget remains for this venue", requested_at,
            )
        try:
            response = client.edit_order(
                id=None, symbol=symbol, price=new_price,
                params={"clientOrderId": client_order_id},
            )
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"reprice {client_order_id}: {type(failure).__name__}: {failure}"
            return self._status(
                client_order_id, venue_id, symbol, REPRICE, FAILED, None,
                f"{type(failure).__name__}: {failure}", requested_at,
            )
        self.standing.repriced += 1
        return self._status(
            client_order_id, venue_id, symbol, REPRICE, SENT, response, "reprice accepted", requested_at
        )

    def _refusal_for(self, venue_id: str, client_order_id: str, action: str) -> RawVenueOrderStatus | None:
        """Every reason not to send, checked before anything leaves this process."""
        requested_at = self._now_ns()
        if client_order_id in self._sent:
            self.standing.duplicates_refused += 1
            return self._status(
                client_order_id, venue_id, "", action, REFUSED_DUPLICATE, None,
                "this exact order has already been sent; resending it would risk two positions",
                requested_at,
            )
        standing = self._read_venue_standing(venue_id)
        if standing == "banned":
            self.standing.refused_standing += 1
            return self._status(
                client_order_id, venue_id, "", action, REFUSED_VENUE_STANDING, None,
                f"{venue_id} is refusing us; sending would extend the ban", requested_at,
            )
        if not self._has_rate_budget(venue_id):
            self.standing.refused_no_budget += 1
            return self._status(
                client_order_id, venue_id, "", action, REFUSED_NO_BUDGET, None,
                "no rate budget remains for this venue this window", requested_at,
            )
        if self._read_key_standing(venue_id) is None:
            self.standing.refused_no_key += 1
            return self._status(
                client_order_id, venue_id, "", action, REFUSED_NO_KEY, None,
                "no key is available for this venue", requested_at,
            )
        return None

    def _status(
        self, client_order_id, venue_id, symbol, action, outcome, response, reason, requested_at
    ) -> RawVenueOrderStatus:
        return RawVenueOrderStatus(
            client_order_id=client_order_id,
            venue_id=venue_id,
            symbol=symbol,
            action=action,
            outcome=outcome,
            venue_response=response,
            reason=reason,
            requested_at_ns=requested_at,
            responded_at_ns=self._now_ns() if outcome != FAILED else None,
        )


def describe_routing(router: CcxtOrderRouter) -> dict:
    return {
        "part_id": PART_ID,
        "placed": router.standing.placed,
        "cancelled": router.standing.cancelled,
        "repriced": router.standing.repriced,
        "refused_no_rate_budget": router.standing.refused_no_budget,
        "refused_venue_standing": router.standing.refused_standing,
        "refused_no_key": router.standing.refused_no_key,
        "duplicates_refused": router.standing.duplicates_refused,
        "failures": router.standing.failures,
        "last_failure": router.standing.last_failure,
        "by_venue": dict(router.standing.by_venue),
    }


def run_ccxt_order_router(
    router: CcxtOrderRouter, control_socket, read_requests, publish_statuses,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        places, cancels, reprices = read_requests()
        statuses = [router.place(**request) for request in places]
        statuses += [router.cancel(**request) for request in cancels]
        statuses += [router.reprice(**request) for request in reprices]
        publish_statuses(tuple(statuses))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
