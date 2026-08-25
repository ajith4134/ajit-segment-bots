"""position-close-detector: a closed trade when a position goes flat.

Closing fills match the oldest open lots first, so a partial close resolves
against what was actually bought first rather than against an average that hides
which parcel was sold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

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
    ClosedTrade,
    Lot,
    LotBook,
    RecentFillIds,
    exact_quantity,
)

PART_ID = "position-close-detector"

# One checkpoint per part per component; this part keeps exactly one thing.
CHECKPOINT_COMPONENT = "positions"

PART_DECLARATION = PartDeclaration(
    part_id="position-close-detector",
    consumes=("position", "fill", "peak-excursion"),
    produces=("closed-trade", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass
class DetectorStanding:
    fills_seen: int = 0
    trades_closed: int = 0
    partial_closes: int = 0
    reversals: int = 0
    open_symbols: int = 0
    # Not counters: what happened to the checkpoint at start. On the board these
    # separate a part that has never run from one that came back holding nothing.
    restored_symbols: int = 0
    checkpoint_verdict: str = ""



class PositionCloseDetector:
    """Emits a closed trade the moment a symbol's position reaches flat.

    A partial close is not a closed trade: the position is still open and its
    remaining lots still carry their own entry prices. Only reaching flat ends a
    round trip, which is what a later phase can score.

    A fill that reverses through flat closes the round trip and opens a new one
    at the same instant, so neither is lost.
    """

    def __init__(self, now_ns=time.time_ns, remembered_fill_ids: int = 5000) -> None:
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], LotBook] = {}
        self._direction: dict[tuple[str, str], str] = {}
        self._realised: dict[tuple[str, str], float] = {}
        self._fees: dict[tuple[str, str], float] = {}
        self._entered_quantity: dict[tuple[str, str], Decimal] = {}
        self._entry_cost: dict[tuple[str, str], float] = {}
        self._opened_at: dict[tuple[str, str], int] = {}
        self._excursion: dict[tuple[str, str], tuple[float, float]] = {}
        self._remembered_fill_ids = int(remembered_fill_ids)
        self._seen_fills = RecentFillIds(self._remembered_fill_ids)
        self.standing = DetectorStanding()

    def read_checkpoint_state(self) -> dict:
        """Everything needed to carry an unfinished round trip into the next process.

        Quantities are written as **strings**, not numbers. `json` has no decimal:
        writing `Decimal("0.01")` as a float and reading it back would restore the
        binary approximation and reintroduce exactly the residue `exact_quantity`
        exists to prevent -- a position that could never reach flat, rebuilt by the
        thing meant to save it.

        The standing counters are deliberately absent. They count what *this
        process* has seen, and a restored count would make `fills_seen` mean
        something other than what it says. `open_symbols` is recomputed from the
        restored books, because that one is a fact about the books rather than
        about the process.
        """
        return {
            "books": books_as_documents(self._books),
            "direction": {book_key_text(k): v for k, v in self._direction.items()},
            "realised": {book_key_text(k): v for k, v in self._realised.items()},
            "fees": {book_key_text(k): v for k, v in self._fees.items()},
            "entered_quantity": {book_key_text(k): str(v) for k, v in self._entered_quantity.items()},
            "entry_cost": {book_key_text(k): v for k, v in self._entry_cost.items()},
            "opened_at": {book_key_text(k): v for k, v in self._opened_at.items()},
            "excursion": {book_key_text(k): list(v) for k, v in self._excursion.items()},
            "seen_fills": self._seen_fills.as_list(),
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Rebuild the open round trips. Returns how many symbols came back."""
        self._books = books_from_documents(state.get("books"))
        self._direction = {book_key_of(k): v for k, v in (state.get("direction") or {}).items()}
        self._realised = {book_key_of(k): float(v) for k, v in (state.get("realised") or {}).items()}
        self._fees = {book_key_of(k): float(v) for k, v in (state.get("fees") or {}).items()}
        self._entered_quantity = {
            book_key_of(k): exact_quantity(v) for k, v in (state.get("entered_quantity") or {}).items()
        }
        self._entry_cost = {book_key_of(k): float(v) for k, v in (state.get("entry_cost") or {}).items()}
        self._opened_at = {book_key_of(k): int(v) for k, v in (state.get("opened_at") or {}).items()}
        self._excursion = {
            book_key_of(k): (v[0], v[1]) for k, v in (state.get("excursion") or {}).items()
        }
        # The window is this process's setting, not the one the checkpoint was
        # written under: an operator who narrowed it means it to apply now.
        self._seen_fills = RecentFillIds(
            self._remembered_fill_ids, state.get("seen_fills") or ()
        )
        self.standing.open_symbols = sum(1 for b in self._books.values() if b.lots)
        return self.standing.open_symbols

    def observe_excursion(self, venue_id: str, symbol: str, best: float, worst: float) -> None:
        self._excursion[(venue_id, symbol)] = (best, worst)

    def observe_fill(self, fill) -> ClosedTrade | None:
        if fill.fill_id in self._seen_fills:
            return None
        self._seen_fills.remember(fill.fill_id)
        self.standing.fills_seen += 1

        key = (fill.venue_id, fill.symbol)
        book = self._books.setdefault(key, LotBook())
        direction = self._direction.get(key, FLAT)
        fill_direction = LONG if fill.side == BUY else SHORT
        self._fees[key] = self._fees.get(key, 0.0) + fill.fee

        # Exact from here down. Every quantity comparison below decides whether a
        # round trip is over, and a float that missed flat by 9e-18 left the
        # position open forever and the trade unscoreable.
        filled = exact_quantity(fill.quantity)

        if direction in (FLAT, fill_direction):
            if direction == FLAT:
                self._opened_at[key] = fill.filled_at_ns
                self._realised[key] = 0.0
                self._entered_quantity[key] = exact_quantity(0)
                self._entry_cost[key] = 0.0
            self._direction[key] = fill_direction
            self._entered_quantity[key] = self._entered_quantity.get(key, exact_quantity(0)) + filled
            self._entry_cost[key] = self._entry_cost.get(key, 0.0) + float(filled) * fill.price
            book.add(Lot(filled, fill.price, fill.filled_at_ns, fill.fee))
            self.standing.open_symbols = sum(1 for b in self._books.values() if b.lots)
            return None

        closing = min(filled, book.total_quantity)
        taken = book.take(closing)
        # Prices are floats, so the gain is computed as one -- a profit is a
        # measurement, not a count, and pretending otherwise would give it a
        # precision it does not have.
        gain = sum(
            (fill.price - lot.price) * float(used) if direction == LONG
            else (lot.price - fill.price) * float(used)
            for lot, used in taken
        )
        self._realised[key] = self._realised.get(key, 0.0) + gain

        if not book.is_flat:
            self.standing.partial_closes += 1
            return None

        trade = self._close(key, fill, direction)
        remaining = filled - closing
        if remaining > 0:
            self.standing.reversals += 1
            self._direction[key] = fill_direction
            self._opened_at[key] = fill.filled_at_ns
            self._realised[key] = 0.0
            self._fees[key] = 0.0
            self._entered_quantity[key] = remaining
            self._entry_cost[key] = float(remaining) * fill.price
            book.add(Lot(remaining, fill.price, fill.filled_at_ns, 0.0))
        else:
            self._direction[key] = FLAT
        self.standing.open_symbols = sum(1 for b in self._books.values() if b.lots)
        return trade

    def _close(self, key, fill, direction) -> ClosedTrade:
        """The round trip as a whole: everything entered, at what it cost to enter.

        Not the closing fill. A round trip sold in slices realises its profit
        across all of them, so backing an entry price out of the last slice alone
        divides the whole trip's profit by a fraction of its quantity and invents
        an entry the market never printed -- the smaller the last slice, the
        further from the truth. What was entered is tracked as it is entered.
        """
        best, worst = self._excursion.get(key, (None, None))
        self.standing.trades_closed += 1
        entered = self._entered_quantity.get(key, exact_quantity(0))
        entry_price = self._entry_cost.get(key, 0.0) / float(entered) if entered else fill.price
        return ClosedTrade(
            venue_id=fill.venue_id,
            symbol=fill.symbol,
            direction=direction,
            # Float at the edge. The book counts exactly; `closed-trade` is read by
            # twenty closed-trade-decoding parts that multiply this against prices,
            # and handing them a Decimal would raise in every one of them.
            quantity=float(entered),
            entry_price=entry_price,
            exit_price=fill.price,
            realised_pnl=self._realised[key],
            fees_paid=self._fees.get(key, 0.0),
            opened_at_ns=self._opened_at.get(key, fill.filled_at_ns),
            closed_at_ns=fill.filled_at_ns,
            best_unrealised=best,
            worst_unrealised=worst,
        )


def describe_closes(detector: PositionCloseDetector) -> dict:
    return {
        "part_id": PART_ID,
        "fills_seen": detector.standing.fills_seen,
        "trades_closed": detector.standing.trades_closed,
        "partial_closes": detector.standing.partial_closes,
        "reversals": detector.standing.reversals,
        "open_symbols": detector.standing.open_symbols,
        # What survived the last off switch, and why -- so a board can tell a part
        # that came back holding four positions from one that came back cold
        # because its checkpoint was unreadable (Rule 8).
        "restored_symbols": detector.standing.restored_symbols,
        # `countable_standing` carries numbers only, so the verdict travels as
        # one: 1 restored, 0 started cold. Which *kind* of cold stays in the
        # string below for logs and tests -- a board that sees 0 restored
        # symbols and 0 here knows it started cold, which is the fact that
        # must never be mistaken for "nothing was open" (Rule 8).
        "checkpoint_restored": 1.0 if detector.standing.checkpoint_verdict == RESTORED else 0.0,
        "checkpoint_verdict": detector.standing.checkpoint_verdict,
    }


def run_position_close_detector(
    detector: PositionCloseDetector, control_socket, read_fills, publish_closed_trade,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        """One batch of closed trades per tick, for the same reason.

        A publisher takes an iterable; a single ClosedTrade is not one.
        """
        closed = []
        observed = 0
        for fill in read_fills():
            observed += 1
            trade = detector.observe_fill(fill)
            if trade is not None:
                closed.append(trade)
        publish_closed_trade(tuple(closed))
        # After publishing, not before. A checkpoint written first would promise a
        # closed trade that no reader had been handed, and a crash between the two
        # would lose the trade while the books said it was already over.
        if observed and write_checkpoint is not None:
            write_checkpoint(detector.standing.fills_seen)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_closes(detector),
    )



def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The part that says a trade is over. Every closed-trade reader in the system --
    the pnl accountant, the label builder, the whole closed-trade-decoding block --
    has an empty inbox until this runs, which is why a system that could open a
    position but not close one could also not learn from one.

    It decides from fills rather than from a position going flat. A position
    reaching zero says the quantity is gone; the fills say at what price, in what
    order, and against which lots -- which is the difference between knowing a
    trade closed and knowing what it made.
    """
    from runtime.input_assembly import Batch

    fills = Batch(read=context.bus.reader("fill"))
    positions = Batch(read=context.bus.reader("position"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    publish_closed_trade = context.bus.publisher_for("closed-trade")

    def read_fills():
        # Excursions first: a closed trade carries the best and worst it went
        # through, and one applied after the close would be attached to the next
        # trade in that symbol instead of to the one that just ended.
        for excursion in excursions.payloads():
            detector.observe_excursion(
                excursion.venue_id, excursion.symbol,
                excursion.best_unrealised, excursion.worst_unrealised,
            )
        # `position` is declared and drained. The close is decided from fills, and
        # a second source of truth for the same event would let the two disagree
        # about when a trade ended.
        positions.payloads()
        return tuple(fills.payloads())

    detector = PositionCloseDetector(
        remembered_fill_ids=int(context.number("remembered_fill_ids"))
    )
    write_checkpoint = restore_and_arm_lot_checkpoint(
        context, PART_ID, CHECKPOINT_COMPONENT, detector
    )

    return run_position_close_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_fills=read_fills,
        publish_closed_trade=publish_closed_trade,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
