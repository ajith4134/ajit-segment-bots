"""peak-excursion-tracker: the best and worst unrealised points a position reached (RL-042)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG

PART_ID = "peak-excursion-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="peak-excursion-tracker",
    consumes=("position", "market-data", "cost-basis"),
    produces=("peak-excursion", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class PeakExcursion:
    """How far a position went right and wrong before it was closed."""

    venue_id: str
    symbol: str
    best_unrealised: float
    worst_unrealised: float
    best_price: float
    worst_price: float
    current_unrealised: float
    samples: int
    observed_at_ns: int


@dataclass
class ExcursionStanding:
    prices_seen: int = 0
    positions_tracked: int = 0
    positions_closed: int = 0
    without_cost_basis: int = 0


class PeakExcursionTracker:
    """Marks each open position to market and keeps its extremes.

    The extremes are the point (RL-042): a trade that closed flat after being 5%
    up and a trade that never moved look identical in realised profit, and only
    one of them was a missed exit. Nothing else records that difference.

    A position with no cost basis is counted, not estimated: an excursion
    measured from a guessed entry is a number that reads exactly like a real one.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._basis: dict[tuple[str, str], tuple[float, float, str]] = {}
        self._extremes: dict[tuple[str, str], list[float]] = {}
        self._samples: dict[tuple[str, str], int] = {}
        self.standing = ExcursionStanding()

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        if position.is_flat:
            self._basis.pop(key, None)
            self._extremes.pop(key, None)
            self._samples.pop(key, None)
            self.standing.positions_closed += 1
        else:
            self._basis[key] = (
                position.average_entry_price,
                abs(position.quantity),
                position.direction,
            )
        self.standing.positions_tracked = len(self._basis)

    def observe_price(self, venue_id: str, symbol: str, price: float) -> PeakExcursion | None:
        self.standing.prices_seen += 1
        key = (venue_id, symbol)
        basis = self._basis.get(key)
        if basis is None:
            self.standing.without_cost_basis += 1
            return None

        entry, quantity, direction = basis
        unrealised = (price - entry) * quantity * (1 if direction == LONG else -1)
        extremes = self._extremes.setdefault(key, [unrealised, unrealised, price, price])
        if unrealised > extremes[0]:
            extremes[0], extremes[2] = unrealised, price
        if unrealised < extremes[1]:
            extremes[1], extremes[3] = unrealised, price
        self._samples[key] = self._samples.get(key, 0) + 1

        return PeakExcursion(
            venue_id=venue_id,
            symbol=symbol,
            best_unrealised=extremes[0],
            worst_unrealised=extremes[1],
            best_price=extremes[2],
            worst_price=extremes[3],
            current_unrealised=unrealised,
            samples=self._samples[key],
            observed_at_ns=self._now_ns(),
        )

    def read(self, venue_id: str, symbol: str) -> PeakExcursion | None:
        key = (venue_id, symbol)
        extremes = self._extremes.get(key)
        if extremes is None:
            return None
        return PeakExcursion(
            venue_id, symbol, extremes[0], extremes[1], extremes[2], extremes[3],
            extremes[0], self._samples.get(key, 0), self._now_ns(),
        )


def describe_excursions(tracker: PeakExcursionTracker) -> dict:
    return {
        "part_id": PART_ID,
        "prices_seen": tracker.standing.prices_seen,
        "positions_tracked": tracker.standing.positions_tracked,
        "positions_closed": tracker.standing.positions_closed,
        "prices_without_cost_basis": tracker.standing.without_cost_basis,
    }


def run_peak_excursion_tracker(
    tracker: PeakExcursionTracker, control_socket, read_positions_and_prices, publish_excursion,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        positions, prices = read_positions_and_prices()
        for position in positions:
            tracker.observe_position(position)
        for venue_id, symbol, price in prices:
            excursion = tracker.observe_price(venue_id, symbol, price)
            if excursion is not None:
                publish_excursion(excursion)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    How far each open position has gone in favour and against, measured against
    live prices as they arrive (RL-071). This is the input that makes a closed
    trade readable afterwards: without it, a trade that ran to +3% and closed at
    -1% and a trade that went straight to -1% are the same row.
    """
    from runtime.input_assembly import Batch

    positions = Batch(read=context.bus.reader("position"))
    trades = Batch(read=context.bus.reader("market-data"))
    bases = Batch(read=context.bus.reader("cost-basis"))
    publish_excursion = context.bus.publisher_for("peak-excursion")

    def read_positions_and_prices():
        # `cost-basis` is declared and drained, and this part measures against the
        # position's own average entry price rather than against it. The two agree
        # for a position built from fills this system made; they diverge for one
        # partly closed, where the lot book knows which lots are left and the
        # position's average does not. Using it here is a change to what an
        # excursion means, which is a decision for the part that owns the meaning
        # -- so the input is read and named, not silently half-applied.
        bases.payloads()
        prices = tuple(
            (trade.venue_id, trade.symbol, trade.price) for trade in trades.payloads()
        )
        return tuple(positions.payloads()), prices

    return run_peak_excursion_tracker(
        tracker=PeakExcursionTracker(),
        control_socket=context.control_socket,
        read_positions_and_prices=read_positions_and_prices,
        publish_excursion=publish_excursion,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
