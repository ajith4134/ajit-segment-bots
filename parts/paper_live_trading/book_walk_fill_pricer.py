"""book-walk-fill-pricer: the price an order of this size would really fill at.

The single largest source of fantasy in paper trading. A simulator that fills at
the touch produces a strategy that looks profitable and is not, because the whole
edge of a high-turnover strategy can be smaller than the spread it never paid.

So this walks the real book depth: it consumes levels until the order is filled
and reports the volume-weighted price that produces, plus how far that is from
the touch. On a shallow symbol a modest order eats several levels and the
difference is enormous; on a deep one it is nothing. Only the book knows which.

**An order the book cannot fill is reported as partial, never as filled at the
last price.** A simulator that filled the remainder at the deepest level it saw
would produce a fill that could not have happened, and the strategy built on it
would be sized for liquidity that is not there.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY

PART_ID = "book-walk-fill-pricer"

PART_DECLARATION = PartDeclaration(
    part_id="book-walk-fill-pricer",
    consumes=("order-request", "order-book-snapshot"),
    produces=("fill-price-estimate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FILLED = "fills-completely"
PARTIAL = "fills-partially"
NO_BOOK = "no-book-to-walk"
EMPTY_SIDE = "no-liquidity-on-that-side"


@dataclass(frozen=True)
class FillPriceEstimate:
    """What an order of this size would actually pay, and how deep it had to go."""

    venue_id: str
    symbol: str
    side: str
    requested_quantity: float
    fillable_quantity: float
    average_price: float | None
    touch_price: float | None
    slippage_fraction: float | None
    levels_consumed: int
    book_depth_available: float
    outcome: str
    reason: str
    estimated_at_ns: int

    @property
    def fills_completely(self) -> bool:
        return self.outcome == FILLED


@dataclass
class PricerStanding:
    estimates: int = 0
    complete_fills: int = 0
    partial_fills: int = 0
    no_book: int = 0
    deepest_walk_levels: int = 0
    worst_slippage_fraction: float = 0.0


class BookWalkFillPricer:
    """Walks real book levels to price an order the way the market would."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], tuple[tuple, tuple]] = {}
        self.standing = PricerStanding()

    def observe_book(
        self,
        venue_id: str,
        symbol: str,
        bids: tuple[tuple[float, float], ...],
        asks: tuple[tuple[float, float], ...],
    ) -> None:
        """One book snapshot: (price, quantity) per level, best first."""
        self._books[(venue_id, symbol)] = (
            tuple(sorted(bids, key=lambda level: -level[0])),
            tuple(sorted(asks, key=lambda level: level[0])),
        )

    def price(self, venue_id: str, symbol: str, side: str, quantity: float) -> FillPriceEstimate:
        self.standing.estimates += 1
        book = self._books.get((venue_id, symbol))

        if book is None:
            self.standing.no_book += 1
            return self._estimate(
                venue_id, symbol, side, quantity, 0.0, None, None, None, 0, 0.0, NO_BOOK,
                "no book snapshot for this symbol; filling at the last price seen would be "
                "a fill that could not have happened",
            )

        bids, asks = book
        # A buy takes from the asks; a sell hits the bids.
        levels = asks if side == BUY else bids
        if not levels:
            return self._estimate(
                venue_id, symbol, side, quantity, 0.0, None, None, None, 0, 0.0, EMPTY_SIDE,
                f"the {'ask' if side == BUY else 'bid'} side of the book is empty",
            )

        touch = levels[0][0]
        available = sum(level_quantity for _, level_quantity in levels)
        remaining = quantity
        cost = 0.0
        consumed = 0

        for level_price, level_quantity in levels:
            if remaining <= 0:
                break
            taken = min(level_quantity, remaining)
            cost += taken * level_price
            remaining -= taken
            consumed += 1

        filled = quantity - remaining
        average = cost / filled if filled > 0 else None
        slippage = abs(average - touch) / touch if average and touch else None

        self.standing.deepest_walk_levels = max(self.standing.deepest_walk_levels, consumed)
        if slippage is not None:
            self.standing.worst_slippage_fraction = max(
                self.standing.worst_slippage_fraction, slippage
            )

        if remaining > 0:
            self.standing.partial_fills += 1
            return self._estimate(
                venue_id, symbol, side, quantity, filled, average, touch, slippage, consumed,
                available, PARTIAL,
                f"the book holds {available:g} against {quantity:g} asked; {remaining:g} could "
                f"not fill at any price shown",
            )

        self.standing.complete_fills += 1
        return self._estimate(
            venue_id, symbol, side, quantity, filled, average, touch, slippage, consumed,
            available, FILLED,
            f"walked {consumed} level(s) to fill {quantity:g} at {average:g}, "
            f"{slippage:.3%} from the touch of {touch:g}",
        )

    def _estimate(
        self, venue_id, symbol, side, requested, fillable, average, touch,
        slippage, consumed, available, outcome, reason
    ) -> FillPriceEstimate:
        return FillPriceEstimate(
            venue_id=venue_id, symbol=symbol, side=side,
            requested_quantity=requested, fillable_quantity=fillable,
            average_price=average, touch_price=touch, slippage_fraction=slippage,
            levels_consumed=consumed, book_depth_available=available,
            outcome=outcome, reason=reason, estimated_at_ns=self._now_ns(),
        )


def describe_pricing(pricer: BookWalkFillPricer) -> dict:
    return {
        "part_id": PART_ID,
        "estimates": pricer.standing.estimates,
        "complete_fills": pricer.standing.complete_fills,
        "partial_fills": pricer.standing.partial_fills,
        "no_book": pricer.standing.no_book,
        "deepest_walk_levels": pricer.standing.deepest_walk_levels,
        "worst_slippage_fraction": pricer.standing.worst_slippage_fraction,
        "books_held": len(pricer._books),
    }


def run_book_walk_fill_pricer(
    pricer: BookWalkFillPricer, control_socket, read_books_and_orders, publish_estimates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        books, orders = read_books_and_orders()
        for book in books:
            pricer.observe_book(**book)
        publish_estimates(tuple(pricer.price(**order) for order in orders))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_pricing(pricer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every book that crosses the bus updates the pricer; every routed order is
    priced against the latest book for its symbol, including a stop order,
    whose fill is a walk of the book the moment it triggers.
    """
    from runtime.input_assembly import Batch
    from runtime.order_book import OrderBookSnapshot

    orders = Batch(read=context.bus.reader("order-request"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    publish_estimates = context.bus.publisher_for("fill-price-estimate")
    pricer = BookWalkFillPricer()

    def read_books_and_orders():
        seen = [
            {"venue_id": book.venue_id, "symbol": book.symbol, "bids": book.bids, "asks": book.asks}
            for book in books.payloads() if isinstance(book, OrderBookSnapshot)
        ]
        requests = [
            {"venue_id": order.venue_id, "symbol": order.symbol, "side": order.side, "quantity": order.quantity}
            for order in orders.payloads() if order.quantity > 0
        ]
        return seen, requests

    def publish(estimates) -> None:
        if estimates:
            publish_estimates(estimates)

    return run_book_walk_fill_pricer(
        pricer=pricer,
        control_socket=context.control_socket,
        read_books_and_orders=read_books_and_orders,
        publish_estimates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
