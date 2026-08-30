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
    leverage_behind,
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
    # A closing fill that left behind less than one quantity step. The residue is
    # taken with the rest rather than left open, and this counts how often the
    # venue's arithmetic and the book's disagreed by an unsellable amount.
    residues_absorbed_into_the_close: int = 0
    # A reversal whose overshoot was itself below one step: nothing is opened,
    # because a position no order could ever close is not a position.
    reversal_overshoots_too_small_to_open: int = 0
    # Books restored from a checkpoint written before the bound existed, holding
    # a residue their own closing fill should have taken. Closed at restore from
    # what was already recorded -- see `_close_residues_left_by_an_older_build`.
    residues_closed_at_restore: int = 0
    open_symbols: int = 0
    # Not counters: what happened to the checkpoint at start. On the board these
    # separate a part that has never run from one that came back holding nothing.
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


def widen_excursion_with_exit(
    best: float | None, worst: float | None,
    entry_price: float, exit_price: float, quantity: float, direction: str,
) -> tuple[float, float]:
    """Fold the trade's own exit into its best/worst -- the exit point always
    sat on the position's unrealised path, whatever `peak-excursion-tracker`
    had managed to relay back by the time this trade closed.

    `peak-excursion-tracker` and this part learn of the same moment from two
    independent, unsynchronised feeds -- a price print on the public tape and
    a fill confirmation from the venue -- so a trade that closes exactly at
    its own best (a take-profit) or worst (a stop) point can beat that print's
    excursion update back here. Without this, `best_unrealised` reads as
    whatever the tracker had last relayed, which can be lower than the
    realised profit the exit itself represents -- a closed trade whose net
    exceeds its own recorded peak, found live 2026-08-30.
    """
    exit_unrealised = (exit_price - entry_price) * quantity * (1 if direction == LONG else -1)
    widened_best = exit_unrealised if best is None else max(best, exit_unrealised)
    widened_worst = exit_unrealised if worst is None else min(worst, exit_unrealised)
    return widened_best, widened_worst



class PositionCloseDetector:
    """Emits a closed trade the moment a symbol's position reaches flat.

    A partial close is not a closed trade: the position is still open and its
    remaining lots still carry their own entry prices. Only reaching flat ends a
    round trip, which is what a later phase can score.

    A fill that reverses through flat closes the round trip and opens a new one
    at the same instant, so neither is lost.
    """

    def __init__(
        self,
        quantity_increment: float,
        now_ns=time.time_ns,
        remembered_fill_ids: int = 5000,
    ) -> None:
        # The venue's quantity step. Below one of these a book holds nothing any
        # order could sell, which is what `LotBook.is_flat_within` calls flat --
        # see that method for the 13 positions this was measured against. Not
        # defaulted: a detector that guessed its own flatness bound would decide
        # when a trade is over from a number nobody chose (RL-061).
        if quantity_increment <= 0:
            raise ValueError(
                "a quantity increment of zero gives no bound on an unsellable "
                "residue, and a book that can never reach flat never closes a trade"
            )
        self._quantity_increment = float(quantity_increment)
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], LotBook] = {}
        self._direction: dict[tuple[str, str], str] = {}
        self._realised: dict[tuple[str, str], float] = {}
        self._fees: dict[tuple[str, str], float] = {}
        self._entered_quantity: dict[tuple[str, str], Decimal] = {}
        self._entry_cost: dict[tuple[str, str], float] = {}
        # Notional entered at each leverage, so the position's own leverage is the
        # quantity-weighted one rather than whichever fill happened to be last.
        # Recorded because what a position ties up is its notional over this
        # number, and nothing between the fill and the operator's board carried it:
        # every open position read as committing its full notional, which is what
        # made a 100 USDT ceiling look breached by 4.5x on 2026-08-28.
        self._notional_at_leverage: dict[tuple[str, str], float] = {}
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
            "notional_at_leverage": {
                book_key_text(k): v for k, v in self._notional_at_leverage.items()
            },
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
        # Absent on a checkpoint written before leverage was recorded. Left absent
        # rather than filled with 1.0: unlevered and unknown are different claims,
        # and the second must reach the board as NOT MEASURED (Rule 8).
        self._notional_at_leverage = {
            book_key_of(k): float(v)
            for k, v in (state.get("notional_at_leverage") or {}).items()
        }
        self._opened_at = {book_key_of(k): int(v) for k, v in (state.get("opened_at") or {}).items()}
        self._excursion = {
            book_key_of(k): (v[0], v[1]) for k, v in (state.get("excursion") or {}).items()
        }
        # The window is this process's setting, not the one the checkpoint was
        # written under: an operator who narrowed it means it to apply now.
        self._seen_fills = RecentFillIds(
            self._remembered_fill_ids, state.get("seen_fills") or ()
        )
        self._trades_left_unfinished_by_an_older_build = tuple(
            self._close_residues_left_by_an_older_build()
        )
        self.standing.open_symbols = self._count_open_books()
        return self.standing.open_symbols

    def take_trades_recovered_at_restore(self) -> tuple:
        """The round trips a restored residue was still holding open. Drained once.

        Published by the first tick rather than at restore, for the same reason
        the checkpoint is written after publishing: a trade nobody was handed is
        not a trade that closed.
        """
        recovered = getattr(self, "_trades_left_unfinished_by_an_older_build", ())
        self._trades_left_unfinished_by_an_older_build = ()
        return recovered

    def _close_residues_left_by_an_older_build(self):
        """Close every restored book whose remainder no order could ever sell.

        Written for the 13 books measured on 2026-08-28, all of them checkpointed
        before `LotBook.is_flat_within` existed. Their closing fills had already
        happened -- what was left was an arithmetic remainder between 4E-18 and
        3.03E-11 -- so the round trip is reconstructed from what was recorded at
        the time rather than from anything measured now:

          * quantity and entry price from `entered_quantity` and `entry_cost`,
            the same two fields `_close` uses;
          * realised from `realised`, which every partial close already added to;
          * the average exit backed out of those three, because for a long
            `realised = (exit - entry) * quantity` and the other two are known.
            Nothing is invented -- it is the exit those numbers imply.

        `closed_at_ns` is the only field nothing recorded, and it is stamped now:
        the position was nominally open until this build recognised the residue,
        and that is what the timestamp says. A reader treating holding time as
        market time should count `residues_closed_at_restore` first.
        """
        for key in list(self._books):
            book = self._books[key]
            if not book.lots or not book.is_flat_within(self._quantity_increment):
                continue
            direction = self._direction.get(key, FLAT)
            entered = self._entered_quantity.get(key, exact_quantity(0))
            if direction == FLAT or entered <= 0:
                # Nothing says which way it was held or how much went in, so
                # there is no round trip to reconstruct. The residue is still
                # dropped: it can never be sold and can never reach flat.
                self._drop_residue(key)
                continue
            quantity = float(entered)
            entry_price = self._entry_cost.get(key, 0.0) / quantity
            realised = self._realised.get(key, 0.0)
            moved = realised / quantity
            exit_price = entry_price + moved if direction == LONG else entry_price - moved
            best, worst = self._excursion.get(key, (None, None))
            best, worst = widen_excursion_with_exit(
                best, worst, entry_price, exit_price, quantity, direction
            )
            venue_id, symbol = key
            self.standing.residues_closed_at_restore += 1
            self.standing.trades_closed += 1
            trade = ClosedTrade(
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=entry_price,
                exit_price=exit_price,
                realised_pnl=realised,
                fees_paid=self._fees.get(key, 0.0),
                opened_at_ns=self._opened_at.get(key, 0),
                closed_at_ns=self._now_ns(),
                best_unrealised=best,
                worst_unrealised=worst,
            )
            self._drop_residue(key)
            yield trade

    def _drop_residue(self, key) -> None:
        """Forget a book that holds nothing sellable, and everything keyed to it."""
        self._books.pop(key, None)
        self._direction[key] = FLAT
        for kept in (
            self._realised, self._fees, self._entered_quantity,
            self._entry_cost, self._notional_at_leverage, self._opened_at, self._excursion,
        ):
            kept.pop(key, None)

    def _count_open_books(self) -> int:
        """How many symbols are actually held, by the same bound the close uses.

        Counted with `is_flat_within` rather than `if b.lots`, because a book
        holding an unsellable residue has lots and holds nothing: on 2026-08-28
        that gap had this counter reporting 20 open symbols while 5 were real.
        """
        return sum(
            1 for b in self._books.values()
            if b.lots and not b.is_flat_within(self._quantity_increment)
        )

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
                self._notional_at_leverage.pop(key, None)
            self._direction[key] = fill_direction
            self._entered_quantity[key] = self._entered_quantity.get(key, exact_quantity(0)) + filled
            entered_notional = float(filled) * fill.price
            self._entry_cost[key] = self._entry_cost.get(key, 0.0) + entered_notional
            self._notional_at_leverage[key] = (
                self._notional_at_leverage.get(key, 0.0)
                + entered_notional / leverage_behind(fill)
            )
            book.add(Lot(filled, fill.price, fill.filled_at_ns, fill.fee))
            self.standing.open_symbols = self._count_open_books()
            return None

        # What the closing fill actually covers -- and the residue it would
        # otherwise leave. A fill a hair short of the book leaves a remainder no
        # order could ever sell, and taking it with the rest is what stops that
        # remainder holding the round trip open forever.
        held = book.total_quantity
        closing = min(filled, held)
        if closing < held and (held - closing) < exact_quantity(self._quantity_increment):
            self.standing.residues_absorbed_into_the_close += 1
            closing = held
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

        if not book.is_flat_within(self._quantity_increment):
            self.standing.partial_closes += 1
            return None

        trade = self._close(key, fill, direction)
        # What the fill asked for beyond the book. Only a reversal big enough to
        # be sold again opens a new position: an overshoot below one quantity
        # step is the same unsellable residue arriving from the other side, and
        # opening a position on it produced 7 of the 13 dust books measured on
        # 2026-08-28 -- `binance-usdm|BTCUSDC` was opened by one, holding 4E-18
        # of BTC and reporting itself open ever since.
        remaining = filled - closing
        if 0 < remaining < exact_quantity(self._quantity_increment):
            self.standing.reversal_overshoots_too_small_to_open += 1
            remaining = exact_quantity(0)
        if remaining > 0:
            self.standing.reversals += 1
            self._direction[key] = fill_direction
            self._opened_at[key] = fill.filled_at_ns
            self._realised[key] = 0.0
            self._fees[key] = 0.0
            self._entered_quantity[key] = remaining
            reversed_notional = float(remaining) * fill.price
            self._entry_cost[key] = reversed_notional
            self._notional_at_leverage[key] = reversed_notional / leverage_behind(fill)
            book.add(Lot(remaining, fill.price, fill.filled_at_ns, 0.0))
        else:
            self._direction[key] = FLAT
        self.standing.open_symbols = self._count_open_books()
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
        best, worst = widen_excursion_with_exit(
            best, worst, entry_price, fill.price, float(entered), direction
        )
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
        "residues_absorbed_into_the_close": detector.standing.residues_absorbed_into_the_close,
        "reversal_overshoots_too_small_to_open": (
            detector.standing.reversal_overshoots_too_small_to_open
        ),
        "residues_closed_at_restore": detector.standing.residues_closed_at_restore,
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
        # Round trips a restored residue was still holding open, published once
        # on the first tick. Drained before the fills so they are handed to every
        # closed-trade reader in the order they actually ended.
        closed = list(detector.take_trades_recovered_at_restore())
        observed = len(closed)
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
        quantity_increment=context.number("order_quantity_increment"),
        remembered_fill_ids=int(context.number("remembered_fill_ids")),
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
