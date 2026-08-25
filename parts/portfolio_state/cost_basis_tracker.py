"""cost-basis-tracker: the average price each position was actually built at."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.durable_state import RESTORED
from runtime.lot_book_checkpoint import (
    book_key_of,
    book_key_text,
    books_as_documents,
    books_from_documents,
    restore_and_arm_lot_checkpoint,
)
from runtime.part_process import run_part
from runtime.trading_types import (
    BUY,
    FLAT,
    LONG,
    SHORT,
    Lot,
    LotBook,
    RecentFillIds,
    exact_quantity,
)

PART_ID = "cost-basis-tracker"

CHECKPOINT_COMPONENT = "lots"

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
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


class CostBasisTracker:
    """Keeps open lots per symbol and reports their weighted average price.

    Idempotent on fill id, because a venue re-sending a fill is ordinary and
    counting it twice would misstate the basis of every later trade.

    A fill that crosses through flat closes the old side and opens the new one
    rather than netting to a nonsense average -- a long of 1 hit by a sell of 3
    is a short of 2 opened at the sell price, not a long at some blended figure.
    """

    def __init__(self, now_ns=time.time_ns, remembered_fill_ids: int = 5000) -> None:
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], LotBook] = {}
        self._direction: dict[tuple[str, str], str] = {}
        self._fees: dict[tuple[str, str], float] = {}
        self._remembered_fill_ids = int(remembered_fill_ids)
        self._seen_fills = RecentFillIds(self._remembered_fill_ids)
        self.standing = CostBasisStanding()

    def read_checkpoint_state(self) -> dict:
        """The open lots, so a basis is not forgotten when the process ends.

        Quantities as strings: `json` has no decimal, and a float round-trip would
        restore the binary approximation `exact_quantity` exists to keep out.
        """
        return {
            "books": books_as_documents(self._books),
            "direction": {book_key_text(k): v for k, v in self._direction.items()},
            "fees": {book_key_text(k): v for k, v in self._fees.items()},
            "seen_fills": self._seen_fills.as_list(),
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Rebuild the open lots. Returns how many symbols came back."""
        self._books = books_from_documents(state.get("books"))
        self._direction = {book_key_of(k): v for k, v in (state.get("direction") or {}).items()}
        self._fees = {book_key_of(k): float(v) for k, v in (state.get("fees") or {}).items()}
        self._seen_fills = RecentFillIds(
            self._remembered_fill_ids, state.get("seen_fills") or ()
        )
        self.standing.symbols_tracked = len(self._books)
        return len(self._books)

    def observe_fill(self, fill) -> CostBasis:
        key = (fill.venue_id, fill.symbol)
        if fill.fill_id in self._seen_fills:
            self.standing.duplicates_ignored += 1
            return self.read(fill.venue_id, fill.symbol)
        self._seen_fills.remember(fill.fill_id)
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
        "restored_symbols": tracker.standing.restored_symbols,
        # `countable_standing` carries numbers only, so the verdict travels as
        # one: 1 restored, 0 started cold. Which *kind* of cold stays in the
        # string below for logs and tests -- a board that sees 0 restored
        # symbols and 0 here knows it started cold, which is the fact that
        # must never be mistaken for "nothing was open" (Rule 8).
        "checkpoint_restored": 1.0 if tracker.standing.checkpoint_verdict == RESTORED else 0.0,
        "checkpoint_verdict": tracker.standing.checkpoint_verdict,
    }


def run_cost_basis_tracker(
    tracker: CostBasisTracker, control_socket, read_fills, publish_cost_basis,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        observed = 0
        for fill in read_fills():
            observed += 1
            tracker.observe_fill(fill)
        publish_cost_basis(tracker.read_all())
        # After publishing: a checkpoint written first would record a basis no
        # reader had been handed yet.
        if observed and write_checkpoint is not None:
            write_checkpoint(tracker.standing.fills_seen)

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

    tracker = CostBasisTracker(
        remembered_fill_ids=int(context.number("remembered_fill_ids"))
    )
    write_checkpoint = restore_and_arm_lot_checkpoint(
        context, PART_ID, CHECKPOINT_COMPONENT, tracker
    )

    return run_cost_basis_tracker(
        tracker=tracker,
        control_socket=context.control_socket,
        read_fills=fills.payloads,
        publish_cost_basis=publish_cost_basis,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
