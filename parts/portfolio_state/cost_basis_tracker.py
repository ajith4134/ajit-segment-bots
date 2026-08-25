"""cost-basis-tracker: the average price each position was actually built at."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, FLAT, LONG, SHORT, Lot, LotBook, exact_quantity

PART_ID = "cost-basis-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="cost-basis-tracker",
    consumes=("fill",),
    produces=("cost-basis", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class CostBasis:
    """What one symbol's open quantity actually cost, fees included."""

    venue_id: str
    symbol: str
    direction: str
    quantity: float
    average_price: float | None
    fees_paid: float
    lots_open: int
    updated_at_ns: int


@dataclass
class CostBasisStanding:
    fills_seen: int = 0
    duplicates_ignored: int = 0
    symbols_tracked: int = 0
    reversals: int = 0


class CostBasisTracker:
    """Keeps open lots per symbol and reports their weighted average price.

    Idempotent on fill id, because a venue re-sending a fill is ordinary and
    counting it twice would misstate the basis of every later trade.

    A fill that crosses through flat closes the old side and opens the new one
    rather than netting to a nonsense average -- a long of 1 hit by a sell of 3
    is a short of 2 opened at the sell price, not a long at some blended figure.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], LotBook] = {}
        self._direction: dict[tuple[str, str], str] = {}
        self._fees: dict[tuple[str, str], float] = {}
        self._seen_fills: set[str] = set()
        self.standing = CostBasisStanding()

    def observe_fill(self, fill) -> CostBasis:
        key = (fill.venue_id, fill.symbol)
        if fill.fill_id in self._seen_fills:
            self.standing.duplicates_ignored += 1
            return self.read(fill.venue_id, fill.symbol)
        self._seen_fills.add(fill.fill_id)
        self.standing.fills_seen += 1

        book = self._books.setdefault(key, LotBook())
        self._fees[key] = self._fees.get(key, 0.0) + fill.fee
        self.standing.symbols_tracked = len(self._books)
        direction = self._direction.get(key, FLAT)
        fill_direction = LONG if fill.side == BUY else SHORT

        # Exact from here down. A quantity that stayed a float would make
        # `book.is_flat` below miss by a fraction of an atom and leave the
        # direction set to a side that is no longer held.
        filled = exact_quantity(fill.quantity)

        if direction in (FLAT, fill_direction):
            self._direction[key] = fill_direction
            book.add(Lot(filled, fill.price, fill.filled_at_ns, fill.fee))
            return self.read(fill.venue_id, fill.symbol)

        remaining = filled - book.total_quantity
        book.take(min(filled, book.total_quantity))
        if remaining > 0:
            self.standing.reversals += 1
            self._direction[key] = fill_direction
            book.lots.clear()
            book.add(Lot(remaining, fill.price, fill.filled_at_ns, 0.0))
        elif book.is_flat:
            self._direction[key] = FLAT
        return self.read(fill.venue_id, fill.symbol)

    def read(self, venue_id: str, symbol: str) -> CostBasis:
        key = (venue_id, symbol)
        book = self._books.get(key, LotBook())
        return CostBasis(
            venue_id=venue_id,
            symbol=symbol,
            direction=self._direction.get(key, FLAT),
            # Float at the edge: the book counts exactly, but `cost-basis` is read
            # by parts that multiply it against prices, and a Decimal would raise
            # in every one of them rather than being quietly wrong.
            quantity=float(book.total_quantity),
            average_price=book.average_price,
            fees_paid=self._fees.get(key, 0.0),
            lots_open=len(book.lots),
            updated_at_ns=self._now_ns(),
        )

    def read_all(self) -> tuple[CostBasis, ...]:
        return tuple(self.read(venue, symbol) for venue, symbol in sorted(self._books))


def describe_cost_basis(tracker: CostBasisTracker) -> dict:
    return {
        "part_id": PART_ID,
        "fills_seen": tracker.standing.fills_seen,
        "duplicates_ignored": tracker.standing.duplicates_ignored,
        "symbols_tracked": tracker.standing.symbols_tracked,
        "reversals": tracker.standing.reversals,
    }


def run_cost_basis_tracker(
    tracker: CostBasisTracker, control_socket, read_fills, publish_cost_basis,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for fill in read_fills():
            tracker.observe_fill(fill)
        publish_cost_basis(tracker.read_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_cost_basis(tracker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    What each open position actually cost, fee included, lot by lot. The excursion
    tracker measures against this rather than against a fill price, because a
    position built from three fills has no single entry price and measuring the
    best excursion against the last one would report a profit the trade never had.
    """
    from runtime.input_assembly import Batch

    fills = Batch(read=context.bus.reader("fill"))
    publish_cost_basis = context.bus.publisher_for("cost-basis")

    return run_cost_basis_tracker(
        tracker=CostBasisTracker(),
        control_socket=context.control_socket,
        read_fills=fills.payloads,
        publish_cost_basis=publish_cost_basis,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
