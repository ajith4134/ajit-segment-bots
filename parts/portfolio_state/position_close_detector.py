"""position-close-detector: a closed trade when a position goes flat.

Closing fills match the oldest open lots first, so a partial close resolves
against what was actually bought first rather than against an average that hides
which parcel was sold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, FLAT, LONG, SHORT, ClosedTrade, Lot, LotBook

PART_ID = "position-close-detector"

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


class PositionCloseDetector:
    """Emits a closed trade the moment a symbol's position reaches flat.

    A partial close is not a closed trade: the position is still open and its
    remaining lots still carry their own entry prices. Only reaching flat ends a
    round trip, which is what a later phase can score.

    A fill that reverses through flat closes the round trip and opens a new one
    at the same instant, so neither is lost.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], LotBook] = {}
        self._direction: dict[tuple[str, str], str] = {}
        self._realised: dict[tuple[str, str], float] = {}
        self._fees: dict[tuple[str, str], float] = {}
        self._opened_at: dict[tuple[str, str], int] = {}
        self._excursion: dict[tuple[str, str], tuple[float, float]] = {}
        self._seen_fills: set[str] = set()
        self.standing = DetectorStanding()

    def observe_excursion(self, venue_id: str, symbol: str, best: float, worst: float) -> None:
        self._excursion[(venue_id, symbol)] = (best, worst)

    def observe_fill(self, fill) -> ClosedTrade | None:
        if fill.fill_id in self._seen_fills:
            return None
        self._seen_fills.add(fill.fill_id)
        self.standing.fills_seen += 1

        key = (fill.venue_id, fill.symbol)
        book = self._books.setdefault(key, LotBook())
        direction = self._direction.get(key, FLAT)
        fill_direction = LONG if fill.side == BUY else SHORT
        self._fees[key] = self._fees.get(key, 0.0) + fill.fee

        if direction in (FLAT, fill_direction):
            if direction == FLAT:
                self._opened_at[key] = fill.filled_at_ns
                self._realised[key] = 0.0
            self._direction[key] = fill_direction
            book.add(Lot(fill.quantity, fill.price, fill.filled_at_ns, fill.fee))
            self.standing.open_symbols = sum(1 for b in self._books.values() if b.lots)
            return None

        closing = min(fill.quantity, book.total_quantity)
        taken = book.take(closing)
        gain = sum(
            (fill.price - lot.price) * used if direction == LONG else (lot.price - fill.price) * used
            for lot, used in taken
        )
        self._realised[key] = self._realised.get(key, 0.0) + gain

        if book.total_quantity > 0:
            self.standing.partial_closes += 1
            return None

        trade = self._close(key, fill, direction, closing)
        remaining = fill.quantity - closing
        if remaining > 0:
            self.standing.reversals += 1
            self._direction[key] = fill_direction
            self._opened_at[key] = fill.filled_at_ns
            self._realised[key] = 0.0
            self._fees[key] = 0.0
            book.add(Lot(remaining, fill.price, fill.filled_at_ns, 0.0))
        else:
            self._direction[key] = FLAT
        self.standing.open_symbols = sum(1 for b in self._books.values() if b.lots)
        return trade

    def _close(self, key, fill, direction, closing_quantity) -> ClosedTrade:
        best, worst = self._excursion.get(key, (None, None))
        self.standing.trades_closed += 1
        entry_price = fill.price - (self._realised[key] / closing_quantity) * (1 if direction == LONG else -1)
        return ClosedTrade(
            venue_id=fill.venue_id,
            symbol=fill.symbol,
            direction=direction,
            quantity=closing_quantity,
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
    }


def run_position_close_detector(
    detector: PositionCloseDetector, control_socket, read_fills, publish_closed_trade,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        """One batch of closed trades per tick, for the same reason.

        A publisher takes an iterable; a single ClosedTrade is not one.
        """
        closed = []
        for fill in read_fills():
            trade = detector.observe_fill(fill)
            if trade is not None:
                closed.append(trade)
        publish_closed_trade(tuple(closed))

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

    detector = PositionCloseDetector()
    return run_position_close_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_fills=read_fills,
        publish_closed_trade=publish_closed_trade,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
