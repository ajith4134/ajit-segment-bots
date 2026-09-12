"""broker-order-router: place each live order with the broker.

**The part that can spend real money on NSE.** Everything about it is shaped by
that, and it is the Indian replacement for `ccxt-order-router` — the last thing
standing between this project and a live Upstox order.

`UpstoxAdapter` has had the whole broker side since the cutover:
`order_endpoint_url()`, `build_order_request_payload()`, `read_order_result()`
and an `OrderRequest` in the fields Upstox's place-order API actually takes. **No
part ever called any of them**, so the live path on this market was unreachable
— and unnoticed, because the paper book filled everything and reported healthy.

## Five refusals, and why there are five

The crypto router's own docstring says what a spending part needs: an idempotent
id, a budget check before the call, and nothing sent to a venue refusing us.
This one adds two, because it talks to a real broker holding real rupees.

1. **Not destined for the live venue.** A paper order belongs to
   `paper-fill-simulator`, whose own first refusal points the other way — so
   each book refuses what the other owns rather than both trying.
2. **The segment's money mode is not exactly `live`.** A second gate reading a
   *different producer*: `destination` is decided by `order-destination-router`,
   `money-mode` is read from the segment's own settings file by
   `money-mode-reader`. One flag is one bug away from spending real money, and
   these two would have to be wrong together.
3. **No valid broker token**, age-bounded. A token that stopped being restated
   is not a token — the same rule, and the same setting, the margin quoter uses.
4. **No instrument key for the symbol.** Upstox places orders by
   `instrument_key` (`NSE_FO|51420`) while every other part names the instrument
   by its trading symbol (`NIFTY 24550 CE 08 SEP 26`). Sending the symbol where
   a token is expected is not one rejected order; it is an order for whatever
   that string resolves to, or for nothing.
5. **An id already sent.** The client order id is a hash of the order's own
   content plus its intent id, so one decision retried is the same id and two
   genuinely different decisions asking for an identical order are not. Held
   locally and sent as Upstox's `tag`.

**Every refusal is published, never dropped.** A `raw-venue-order-status` with
its own named outcome comes out of each one. An order that vanishes silently is
the shape this project keeps being bitten by: a gate, a router and a book each
reporting nothing wrong while no order exists.

## What this deliberately does not do

It places orders. It does not cancel or reprice them — Upstox has separate
endpoints for both and neither is on the adapter yet, and declaring a consume
for a capability that would then refuse everything is how a part comes to look
built. It does not poll for status; `order-state-poller` is that part. Its
standing names all three as absent rather than leaving them to be assumed.

**It is not on the live spine.** Declared, built and tested, and
`operate/run_live_spine.py` does not start it: both segments are on paper, so a
router that can reach Upstox's place-order endpoint has nothing legitimate to
do, and a part that can spend money is started deliberately on the day a segment
goes live rather than inherited from a commit.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from runtime.brokers.broker_http_request import build_broker_request
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, LIVE_VENUE

PART_ID = "broker-order-router"

PART_DECLARATION = PartDeclaration(
    part_id="broker-order-router",
    consumes=("order-request", "broker-token-standing", "money-mode", "symbol-universe"),
    produces=("raw-venue-order-status", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

PLACED = "placed"
REFUSED_NOT_LIVE_DESTINATION = "refused-this-order-is-not-addressed-to-the-live-venue"
REFUSED_SEGMENT_IS_ON_PAPER = "refused-this-segment-is-not-in-live-money-mode"
REFUSED_NO_TOKEN = "refused-no-valid-broker-token"
REFUSED_NO_INSTRUMENT_KEY = "refused-no-instrument-key-for-this-symbol"
REFUSED_DUPLICATE = "refused-an-order-with-this-id-has-already-been-sent"
BROKER_REFUSED = "the-broker-refused-the-order"
FAILED = "the-call-to-the-broker-failed"

# The length of a client order id. Upstox's `tag` field is short, and a hash
# prefix this long is what the crypto router already uses -- same reasoning,
# same length, so the two ids are comparable in a journal.
CLIENT_ORDER_ID_LENGTH = 32

# Upstox's own words. BUY/SELL rather than buy/sell, and its own order-type
# spelling. Stated here because they are the broker's vocabulary and a part
# names the data it sends (T-4), not because they are a choice.
TRANSACTION_BUY = "BUY"
TRANSACTION_SELL = "SELL"
BROKER_MARKET = "MARKET"
BROKER_LIMIT = "LIMIT"

# The one money mode that lets an order through. Compared exactly: a mode that
# is not this string is not live, whatever it is, and `money-mode-reader`
# already refuses anything that is not exactly "paper" or "live".
LIVE = "live"


@dataclass(frozen=True)
class RawVenueOrderStatus:
    """What the broker said back, unmodified, plus how the request went out.

    The same shape `ccxt-order-router` publishes, deliberately: four parts
    already read this type and a broker-specific one would mean four more
    (T-6).
    """

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
    orders_seen: int = 0
    placed: int = 0
    refused_not_live_destination: int = 0
    refused_segment_on_paper: int = 0
    refused_no_token: int = 0
    refused_no_instrument_key: int = 0
    refused_duplicate: int = 0
    broker_refused: int = 0
    failures: int = 0
    last_failure: str | None = None
    # Named rather than left to be assumed: this part places orders and does
    # none of these, and a reader of the standing should not have to infer that
    # from their absence.
    cancels_orders: bool = False
    reprices_orders: bool = False
    polls_for_status: bool = False


class BrokerOrderRouter:
    """Sends live orders to the broker, behind five refusals."""

    def __init__(
        self,
        adapter,
        place,
        read_money_mode,
        read_instrument_key,
        read_token,
        product: str,
        validity: str,
        now_ns=time.time_ns,
    ) -> None:
        self._adapter = adapter
        self._place = place
        self._read_money_mode = read_money_mode
        self._read_instrument_key = read_instrument_key
        self._read_token = read_token
        self._product = product
        self._validity = validity
        self._now_ns = now_ns
        self._sent: set[str] = set()
        self.standing = RouterStanding()

    def client_order_id(self, order) -> str:
        """An id derived from the order itself, so a retry is the same order.

        The intent id is included because two genuinely different decisions may
        ask for an identical order and must not collide into one, while one
        decision retried must.
        """
        body = (
            f"{order.venue_id}|{order.symbol}|{order.side}|{order.quantity!r}"
            f"|{getattr(order, 'limit_price', 0.0)!r}|{getattr(order, 'intent_id', '')}"
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:CLIENT_ORDER_ID_LENGTH]

    def route(self, order) -> RawVenueOrderStatus:
        self.standing.orders_seen += 1
        requested_at_ns = self._now_ns()
        client_order_id = self.client_order_id(order)

        def answer(outcome: str, reason: str, response=None, responded=None):
            return RawVenueOrderStatus(
                client_order_id=client_order_id,
                venue_id=order.venue_id,
                symbol=order.symbol,
                action="place",
                outcome=outcome,
                venue_response=response,
                reason=reason,
                requested_at_ns=requested_at_ns,
                responded_at_ns=responded,
            )

        # 1. The paper book owns anything not addressed to the live venue.
        if getattr(order, "destination", None) != LIVE_VENUE:
            self.standing.refused_not_live_destination += 1
            return answer(
                REFUSED_NOT_LIVE_DESTINATION,
                f"this order is addressed to {getattr(order, 'destination', None)!r}; "
                f"paper-fill-simulator fills those and this part must not",
            )

        # 2. A second, independent gate on a different producer's word.
        segment = getattr(order, "segment", "") or ""
        mode = self._read_money_mode(segment)
        if mode is None or getattr(mode, "mode", None) != LIVE:
            self.standing.refused_segment_on_paper += 1
            stated = "nothing" if mode is None else repr(getattr(mode, "mode", None))
            return answer(
                REFUSED_SEGMENT_IS_ON_PAPER,
                f"the money mode for segment {segment!r} says {stated}, not 'live'. The "
                f"destination and the mode come from different parts on purpose: one "
                f"flag is one bug away from spending real money",
            )

        # 3. A token that stopped being restated is not a token.
        token = self._read_token()
        if token is None or not token.is_still_valid():
            self.standing.refused_no_token += 1
            return answer(
                REFUSED_NO_TOKEN,
                "no valid broker token; an order cannot be authenticated and must not "
                "be attempted unauthenticated",
            )

        # 4. Upstox places by instrument key, not by trading symbol.
        instrument_key = self._read_instrument_key(order.symbol)
        if not instrument_key:
            self.standing.refused_no_instrument_key += 1
            return answer(
                REFUSED_NO_INSTRUMENT_KEY,
                f"no instrument key is known for {order.symbol!r}; sending the trading "
                f"symbol where the broker expects a token is an order for whatever that "
                f"string resolves to",
            )

        # 5. The same order twice is one order.
        if client_order_id in self._sent:
            self.standing.refused_duplicate += 1
            return answer(
                REFUSED_DUPLICATE,
                f"an order with id {client_order_id} has already been sent; a repeat is "
                f"the same decision retried, not a second position",
            )

        broker_order = self._broker_order_for(order, instrument_key, client_order_id)
        payload = self._adapter.build_order_request_payload(broker_order)
        self._sent.add(client_order_id)
        try:
            response = self._place(
                self._adapter.order_endpoint_url(), payload, token.access_token
            )
        except Exception as failure:  # noqa: BLE001 - every failure is one fact here
            self.standing.failures += 1
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return answer(
                FAILED,
                f"the call to the broker failed: {type(failure).__name__}: {failure}. "
                f"The id is remembered, so a retry is refused as a duplicate rather "
                f"than opening a second position -- a timeout is the case where the "
                f"order may or may not have arrived",
                responded=self._now_ns(),
            )

        try:
            result = self._adapter.read_order_result(response)
        except Exception as refusal:  # noqa: BLE001 - the broker said no
            self.standing.broker_refused += 1
            return answer(
                BROKER_REFUSED, str(refusal), response=response, responded=self._now_ns()
            )

        self.standing.placed += 1
        return answer(
            PLACED,
            f"the broker accepted it as order {result.order_id}",
            response=response,
            responded=self._now_ns(),
        )

    def _broker_order_for(self, order, instrument_key: str, client_order_id: str):
        """This order in the fields Upstox's place-order API actually takes."""
        from runtime.brokers.upstox import OrderRequest as BrokerOrderRequest

        is_limit = getattr(order, "order_type", "") == "limit"
        return BrokerOrderRequest(
            instrument_key=instrument_key,
            # Whole units. The exchange trades in lots and
            # trade-capital-bounds-gate has already snapped the quantity to a
            # whole number of them; int() here is the type the API takes, not a
            # second rounding, and a fractional quantity arriving means that
            # gate was bypassed.
            quantity=int(order.quantity),
            product=self._product,
            order_type=BROKER_LIMIT if is_limit else BROKER_MARKET,
            transaction_type=TRANSACTION_BUY if order.side == BUY else TRANSACTION_SELL,
            validity=self._validity,
            price=float(getattr(order, "limit_price", 0.0) or 0.0) if is_limit else 0.0,
            tag=client_order_id,
        )


def describe_router(router: BrokerOrderRouter) -> dict:
    return {
        "part_id": PART_ID,
        "orders_seen": router.standing.orders_seen,
        "placed": router.standing.placed,
        "refused_not_live_destination": router.standing.refused_not_live_destination,
        "refused_segment_on_paper": router.standing.refused_segment_on_paper,
        "refused_no_token": router.standing.refused_no_token,
        "refused_no_instrument_key": router.standing.refused_no_instrument_key,
        "refused_duplicate": router.standing.refused_duplicate,
        "broker_refused": router.standing.broker_refused,
        "failures": router.standing.failures,
        "last_failure": router.standing.last_failure,
        "cancels_orders": router.standing.cancels_orders,
        "reprices_orders": router.standing.reprices_orders,
        "polls_for_status": router.standing.polls_for_status,
    }


def place_order(url: str, payload: dict, access_token: str, timeout_seconds: float) -> dict:
    """One call to the broker's place-order endpoint.

    Separate from the class so every test injects its own and none can reach
    api-hft.upstox.com. A test that could place an order is a test that might.
    """
    request = build_broker_request(
        url,
        access_token=access_token,
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.brokers.upstox import UpstoxAdapter
    from runtime.input_assembly import Batch, LatestByKey

    adapter = UpstoxAdapter()
    orders = Batch(read=context.bus.reader("order-request"))
    tokens = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    # Per segment, and age-bounded, for the same reason
    # order-destination-router keys its own by segment: a spine where one
    # segment is live and another is on paper must not route either by
    # whichever mode arrived last.
    modes = LatestByKey(
        read=context.bus.reader("money-mode"),
        key_of=lambda mode: mode.segment,
        maximum_age_seconds=context.number("money_mode_maximum_age_seconds"),
    )
    universe = LatestByKey(
        read=context.bus.reader("symbol-universe"),
        key_of=lambda entry: entry.symbol,
    )
    publish_statuses = context.bus.publisher_for("raw-venue-order-status")
    timeout_seconds = context.number("broker_order_timeout_seconds")

    def read_instrument_key(symbol: str):
        entry = universe.mapping().get(symbol)
        return None if entry is None else getattr(entry, "venue_instrument_id", None)

    router = BrokerOrderRouter(
        adapter=adapter,
        place=lambda url, payload, token: place_order(url, payload, token, timeout_seconds),
        read_money_mode=lambda segment: modes.mapping().get(segment),
        read_instrument_key=read_instrument_key,
        read_token=lambda: tokens.mapping().get(adapter.broker_id),
        product=str(context.setting("broker_order_product").value),
        validity=str(context.setting("broker_order_validity").value),
    )

    def tick() -> None:
        tokens.take_in_what_arrived()
        modes.take_in_what_arrived()
        universe.take_in_what_arrived()
        statuses = [router.route(order) for order in orders.payloads()]
        if statuses:
            publish_statuses(tuple(statuses))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_router(router),
    )


__all__ = [
    "BROKER_REFUSED",
    "BrokerOrderRouter",
    "CLIENT_ORDER_ID_LENGTH",
    "FAILED",
    "PART_DECLARATION",
    "PART_ID",
    "PLACED",
    "REFUSED_DUPLICATE",
    "REFUSED_NOT_LIVE_DESTINATION",
    "REFUSED_NO_INSTRUMENT_KEY",
    "REFUSED_NO_TOKEN",
    "REFUSED_SEGMENT_IS_ON_PAPER",
    "RawVenueOrderStatus",
    "RouterStanding",
    "describe_router",
    "place_order",
    "start_part",
]
