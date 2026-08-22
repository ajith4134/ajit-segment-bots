"""paper-fill-simulator: fill a paper order at the live price, with real costs applied.

The part that decides whether paper results mean anything. Every shortcut here
produces a strategy that works on paper and loses money live, and the failure is
invisible until real capital is behind it.

Four things it will not do, each corresponding to a way paper trading lies:

- **It does not fill at the touch.** The fill price comes from walking real book
  depth, so an order larger than the top level pays for the levels it eats.
- **It does not fill instantly.** An order fills only after its simulated round
  trip has elapsed, at whatever price exists *then* -- not the one that was on
  screen when the decision was made.
- **It does not fill a limit order the market never reached.** A buy limit below
  the market waits, exactly as it would live, and may never fill at all.
- **It does not ignore fees.** Every fill is charged the venue's real taker or
  maker rate, because a strategy profitable before fees is not profitable.

And one thing it refuses: **a fill during a feed jump**. If the price series
jumped, the prices around the gap are not prices anything could have traded at,
and filling there manufactures profit out of a data artefact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, Fill

PART_ID = "paper-fill-simulator"

PART_DECLARATION = PartDeclaration(
    part_id="paper-fill-simulator",
    consumes=(
        "order-request", "market-data", "cost-estimate", "money-mode",
        "delayed-order-request", "fill-price-estimate", "feed-jump", "consolidated-price",
    ),
    produces=("fill", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FILLED = "filled"
PARTIALLY_FILLED = "partially-filled"
RESTING = "resting-limit-not-reached"
HELD_IN_FLIGHT = "held-until-the-round-trip-elapses"
REFUSED_FEED_JUMP = "refused-price-jumped"
REFUSED_NO_PRICE = "refused-no-price"
REFUSED_LIVE_ORDER = "refused-live-orders-are-not-simulated"
# The id has already been filled for everything it asked for. Its own outcome
# rather than a refusal for no price: nothing is wrong with the order or the
# book, and an operator reading "no price" for a duplicate would look at the feed.
ALREADY_FILLED = "already-filled"

MARKET = "market"
LIMIT = "limit"


@dataclass(frozen=True)
class PaperFillResult:
    """What happened to one paper order this tick, and why."""

    client_order_id: str
    venue_id: str
    symbol: str
    side: str
    outcome: str
    fill: Fill | None
    filled_quantity: float
    remaining_quantity: float
    fill_price: float | None
    fee_charged: float
    slippage_fraction: float | None
    reason: str
    decided_at_ns: int

    @property
    def did_fill(self) -> bool:
        return self.outcome in (FILLED, PARTIALLY_FILLED) and self.fill is not None


@dataclass
class SimulatorStanding:
    orders_seen: int = 0
    filled: int = 0
    partially_filled: int = 0
    resting: int = 0
    held_in_flight: int = 0
    refused_feed_jump: int = 0
    refused_no_price: int = 0
    refused_already_filled: int = 0
    fees_charged: float = 0.0
    worst_slippage_fraction: float = 0.0


class PaperFillSimulator:
    """Fills paper orders the way a venue would, and refuses the fills a venue would not give."""

    def __init__(self, taker_fee_rate: float, maker_fee_rate: float, now_ns=time.time_ns) -> None:
        if taker_fee_rate < 0 or maker_fee_rate < 0:
            raise ValueError("a fee rate cannot be negative")
        self._taker_fee = taker_fee_rate
        self._maker_fee = maker_fee_rate
        self._now_ns = now_ns
        self._jumped_symbols: set[tuple[str, str]] = set()
        self._filled_so_far: dict[str, float] = {}
        self._fill_sequence = 0
        self.standing = SimulatorStanding()

    def observe_feed_jump(self, venue_id: str, symbol: str) -> None:
        """A discontinuity in this symbol's prices; nothing may fill across it."""
        self._jumped_symbols.add((venue_id, symbol))

    def clear_feed_jump(self, venue_id: str, symbol: str) -> None:
        self._jumped_symbols.discard((venue_id, symbol))

    def simulate(
        self,
        client_order_id: str,
        venue_id: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        limit_price: float | None,
        money_mode: str,
        is_in_flight: bool,
        fill_price_estimate=None,
        market_price: float | None = None,
    ) -> PaperFillResult:
        self.standing.orders_seen += 1

        if money_mode != "paper":
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_LIVE_ORDER, None, 0.0, quantity,
                None, 0.0, None, "a live order is filled by the venue, not simulated here",
            )

        if is_in_flight:
            self.standing.held_in_flight += 1
            return self._result(
                client_order_id, venue_id, symbol, side, HELD_IN_FLIGHT, None, 0.0, quantity,
                None, 0.0, None,
                "the simulated round trip has not elapsed; filling now would trade on the price "
                "that was on screen when the decision was made",
            )

        if (venue_id, symbol) in self._jumped_symbols:
            self.standing.refused_feed_jump += 1
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_FEED_JUMP, None, 0.0, quantity,
                None, 0.0, None,
                "this symbol's price series jumped; prices around a gap are not prices anything "
                "could have traded at, and filling there manufactures profit from an artefact",
            )

        if fill_price_estimate is None or fill_price_estimate.average_price is None:
            if market_price is None:
                self.standing.refused_no_price += 1
                return self._result(
                    client_order_id, venue_id, symbol, side, REFUSED_NO_PRICE, None, 0.0, quantity,
                    None, 0.0, None, "no book estimate and no market price; nothing to fill against",
                )
            price, fillable, slippage, is_taker = market_price, quantity, None, True
        else:
            price = fill_price_estimate.average_price
            fillable = min(quantity, fill_price_estimate.fillable_quantity)
            slippage = fill_price_estimate.slippage_fraction
            is_taker = True

        if order_type == LIMIT and limit_price is not None:
            reached = price <= limit_price if side == BUY else price >= limit_price
            if not reached:
                self.standing.resting += 1
                return self._result(
                    client_order_id, venue_id, symbol, side, RESTING, None, 0.0, quantity,
                    None, 0.0, None,
                    f"the market is at {price:g} and the limit is {limit_price:g}; a real order "
                    f"would rest here and may never fill",
                )
            # A limit order that the market came to is a maker fill, and the
            # difference in fee is the whole reason to place one.
            price = limit_price
            is_taker = False

        # What this id still has outstanding. A venue holds one order per client
        # id -- that is the whole reason `order-idempotency-stamper` derives a
        # stable one -- so an id already filled in full is not filled again, and a
        # partially filled one fills only what is left. Without this the simulator
        # counted what it had filled and then filled it again anyway, so a
        # resubmitted order opened a second position on paper while the same
        # order against a real venue would have been rejected as a duplicate.
        already_filled = self._filled_so_far.get(client_order_id, 0.0)
        outstanding = quantity - already_filled
        if outstanding <= 0:
            self.standing.refused_already_filled += 1
            return self._result(
                client_order_id, venue_id, symbol, side, ALREADY_FILLED, None, 0.0, 0.0,
                None, 0.0, None,
                f"{client_order_id} has already been filled for {already_filled:g}, which is "
                f"the whole order; a venue holding this id would reject the duplicate rather "
                f"than open a second position",
            )
        fillable = min(fillable, outstanding)

        if fillable <= 0:
            self.standing.refused_no_price += 1
            return self._result(
                client_order_id, venue_id, symbol, side, REFUSED_NO_PRICE, None, 0.0, quantity,
                None, 0.0, None, "the book showed no fillable quantity at any price",
            )

        fee_rate = self._taker_fee if is_taker else self._maker_fee
        fee = fillable * price * fee_rate
        self.standing.fees_charged += fee
        if slippage is not None:
            self.standing.worst_slippage_fraction = max(
                self.standing.worst_slippage_fraction, slippage
            )

        self._filled_so_far[client_order_id] = already_filled + fillable
        remaining = outstanding - fillable
        outcome = FILLED if remaining <= 0 else PARTIALLY_FILLED
        if outcome == FILLED:
            self.standing.filled += 1
        else:
            self.standing.partially_filled += 1

        self._fill_sequence += 1
        fill = Fill(
            fill_id=f"paper-{client_order_id}-{self._fill_sequence}",
            venue_id=venue_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=fillable,
            fee=fee,
            filled_at_ns=self._now_ns(),
            order_id=client_order_id,
            is_paper=True,
        )
        return self._result(
            client_order_id, venue_id, symbol, side, outcome, fill, fillable, remaining,
            price, fee, slippage,
            f"{fillable:g} at {price:g} as a {'taker' if is_taker else 'maker'}, "
            f"fee {fee:,.4f}"
            + (f", {slippage:.3%} from the touch" if slippage else ""),
        )

    def _result(
        self, client_order_id, venue_id, symbol, side, outcome, fill,
        filled, remaining, price, fee, slippage, reason
    ) -> PaperFillResult:
        return PaperFillResult(
            client_order_id=client_order_id, venue_id=venue_id, symbol=symbol, side=side,
            outcome=outcome, fill=fill, filled_quantity=filled, remaining_quantity=remaining,
            fill_price=price, fee_charged=fee, slippage_fraction=slippage,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_paper_fills(simulator: PaperFillSimulator) -> dict:
    return {
        "part_id": PART_ID,
        "orders_seen": simulator.standing.orders_seen,
        "filled": simulator.standing.filled,
        "partially_filled": simulator.standing.partially_filled,
        "resting": simulator.standing.resting,
        "held_in_flight": simulator.standing.held_in_flight,
        "refused_feed_jump": simulator.standing.refused_feed_jump,
        "refused_no_price": simulator.standing.refused_no_price,
        "refused_already_filled": simulator.standing.refused_already_filled,
        "fees_charged": simulator.standing.fees_charged,
        "worst_slippage_fraction": simulator.standing.worst_slippage_fraction,
    }


def run_paper_fill_simulator(
    simulator: PaperFillSimulator, control_socket, read_orders, publish_fills,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        orders = read_orders(simulator)
        results = [simulator.simulate(**order) for order in orders]
        publish_fills(tuple(result.fill for result in results if result.did_fill))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Where the first paper fill happens. It refuses anything whose money mode is not
    exactly paper -- a live order is filled by the venue, not simulated here -- so
    the paper-only guarantee is enforced twice, once by the router addressing the
    order and once here by refusing to pretend.

    The market price it fills against is the last trade this part saw for that
    symbol, live off the bus. A cost estimate or a book-walk price would be better
    and neither is running, so an order with neither is filled at the last price and
    the fill says which it used.
    """
    from runtime.input_assembly import Batch, LatestByKey, LatestValue

    requests = Batch(read=context.bus.reader("order-request"))
    trades = Batch(read=context.bus.reader("market-data"))
    modes = LatestValue(read=context.bus.reader("money-mode"))
    costs = Batch(read=context.bus.reader("cost-estimate"))
    delayed = Batch(read=context.bus.reader("delayed-order-request"))
    jumps = Batch(read=context.bus.reader("feed-jump"))
    consolidated = Batch(read=context.bus.reader("consolidated-price"))
    prices = LatestByKey(
        read=context.bus.reader("fill-price-estimate"),
        key_of=lambda estimate: (estimate.venue_id, estimate.symbol),
    )
    publish_fills = context.bus.publisher_for("fill")

    last_price: dict[tuple[str, str], float] = {}

    def read_orders(simulator):
        for trade in trades.payloads():
            last_price[(trade.venue_id, trade.symbol)] = trade.price
        for jump in jumps.payloads():
            simulator.observe_feed_jump(jump.venue_id, jump.symbol)
        costs.payloads()
        consolidated.payloads()
        estimate_by_symbol = prices.mapping()
        mode = modes.value()
        mode_name = getattr(mode, "mode", None)

        orders = []
        for request in list(requests.payloads()) + list(delayed.payloads()):
            if not request.may_be_sent:
                continue
            key = (request.venue_id, request.symbol)
            orders.append(
                {
                    "client_order_id": request.client_order_id,
                    "venue_id": request.venue_id,
                    "symbol": request.symbol,
                    "side": request.side,
                    "quantity": request.quantity,
                    "order_type": LIMIT if request.limit_price else MARKET,
                    "limit_price": request.limit_price or None,
                    # None when the mode could not be read, which this part refuses
                    # rather than treating as paper.
                    "money_mode": mode_name,
                    "is_in_flight": False,
                    "fill_price_estimate": estimate_by_symbol.get(key),
                    "market_price": last_price.get(key),
                }
            )
        return tuple(orders)

    return run_paper_fill_simulator(
        simulator=PaperFillSimulator(
            taker_fee_rate=context.number("taker_fee_rate"),
            maker_fee_rate=context.number("maker_fee_rate"),
        ),
        control_socket=context.control_socket,
        read_orders=read_orders,
        publish_fills=publish_fills,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
