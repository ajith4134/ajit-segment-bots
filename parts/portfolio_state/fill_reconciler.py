"""fill-reconciler: the held position from fills, checked against the venue's own."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, Position

PART_ID = "fill-reconciler"

PART_DECLARATION = PartDeclaration(
    part_id="fill-reconciler",
    consumes=("fill", "venue-position-report"),
    produces=("position", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

AGREED = "agreed"
DIVERGED = "diverged"
UNCHECKED = "unchecked"


@dataclass(frozen=True)
class Reconciliation:
    """Our position, the venue's, and whether they agree."""

    position: Position
    venue_quantity: float | None
    verdict: str
    difference: float | None
    reason: str
    observed_at_ns: int


@dataclass
class ReconcilerStanding:
    fills_applied: int = 0
    duplicates_ignored: int = 0
    checks: int = 0
    divergences: int = 0
    symbols: int = 0
    last_divergence: str | None = None


class FillReconciler:
    """Builds each position from fills, then compares it with what the venue says.

    The venue is authoritative and this part still does not silently adopt its
    number. A divergence means a fill was missed, duplicated, or invented, and
    overwriting our figure would erase the only evidence that happened -- so it
    is reported and the difference named.

    Idempotent on fill id: a venue re-sending a fill is ordinary, and counting it
    twice would put the position permanently wrong in a way no later fill fixes.
    """

    def __init__(self, quantity_tolerance: float, now_ns=time.time_ns) -> None:
        self._tolerance = quantity_tolerance
        self._now_ns = now_ns
        self._positions: dict[tuple[str, str], Position] = {}
        self._seen_fills: set[str] = set()
        self._venue_quantities: dict[tuple[str, str], float] = {}
        self.standing = ReconcilerStanding()

    def observe_fill(self, fill) -> Position:
        key = (fill.venue_id, fill.symbol)
        if fill.fill_id in self._seen_fills:
            self.standing.duplicates_ignored += 1
            return self._positions[key]
        self._seen_fills.add(fill.fill_id)
        self.standing.fills_applied += 1

        held = self._positions.get(key)
        if held is None:
            position = Position(
                venue_id=fill.venue_id,
                symbol=fill.symbol,
                quantity=fill.signed_quantity,
                average_entry_price=fill.price,
                realised_pnl=0.0,
                fees_paid=fill.fee,
                opened_at_ns=fill.filled_at_ns,
                updated_at_ns=fill.filled_at_ns,
            )
        else:
            position = self._apply(held, fill)
        self._positions[key] = position
        self.standing.symbols = len(self._positions)
        return position

    def _apply(self, held: Position, fill) -> Position:
        signed = fill.signed_quantity
        new_quantity = held.quantity + signed
        realised = held.realised_pnl
        average = held.average_entry_price

        increasing = held.quantity == 0 or (held.quantity > 0) == (signed > 0)
        if increasing:
            total = abs(held.quantity) + abs(signed)
            average = (
                (abs(held.quantity) * held.average_entry_price + abs(signed) * fill.price) / total
                if total
                else fill.price
            )
        else:
            closed = min(abs(held.quantity), abs(signed))
            gain = (fill.price - held.average_entry_price) * closed
            realised += gain if held.quantity > 0 else -gain
            if abs(signed) > abs(held.quantity):
                average = fill.price

        return Position(
            venue_id=held.venue_id,
            symbol=held.symbol,
            quantity=new_quantity,
            average_entry_price=average if new_quantity != 0 else 0.0,
            realised_pnl=realised,
            fees_paid=held.fees_paid + fill.fee,
            opened_at_ns=held.opened_at_ns if held.quantity != 0 else fill.filled_at_ns,
            updated_at_ns=fill.filled_at_ns,
        )

    def observe_venue_report(self, venue_id: str, symbol: str, quantity: float) -> None:
        self._venue_quantities[(venue_id, symbol)] = quantity

    def reconcile(self, venue_id: str, symbol: str) -> Reconciliation:
        key = (venue_id, symbol)
        position = self._positions.get(key)
        if position is None:
            position = Position(venue_id, symbol, 0.0, 0.0, 0.0, 0.0, self._now_ns(), self._now_ns())
        venue_quantity = self._venue_quantities.get(key)
        self.standing.checks += 1

        if venue_quantity is None:
            return Reconciliation(
                position, None, UNCHECKED, None,
                "the venue has reported no position for this symbol", self._now_ns(),
            )
        difference = position.quantity - venue_quantity
        if abs(difference) <= self._tolerance:
            return Reconciliation(
                position, venue_quantity, AGREED, difference,
                "our fills and the venue agree", self._now_ns(),
            )
        self.standing.divergences += 1
        self.standing.last_divergence = (
            f"{venue_id} {symbol}: ours {position.quantity}, venue {venue_quantity}"
        )
        return Reconciliation(
            position, venue_quantity, DIVERGED, difference,
            f"ours {position.quantity} against the venue's {venue_quantity}", self._now_ns(),
        )

    def reconcile_all(self) -> tuple[Reconciliation, ...]:
        keys = sorted(set(self._positions) | set(self._venue_quantities))
        return tuple(self.reconcile(venue, symbol) for venue, symbol in keys)


def describe_reconciliation(reconciler: FillReconciler) -> dict:
    return {
        "part_id": PART_ID,
        "fills_applied": reconciler.standing.fills_applied,
        "duplicates_ignored": reconciler.standing.duplicates_ignored,
        "checks": reconciler.standing.checks,
        "divergences": reconciler.standing.divergences,
        "symbols": reconciler.standing.symbols,
        "last_divergence": reconciler.standing.last_divergence,
    }


def run_fill_reconciler(
    reconciler: FillReconciler, control_socket, read_fills_and_reports, publish_positions,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        """Reconcile, then publish the positions -- not the reconciliations.

        This part declares that it produces `position`, and a `Reconciliation` is
        not one: it is a position plus a verdict about whether the venue agrees.
        Publishing the wrapper put a shape on the bus that no consumer of
        `position` could read, and every part downstream of a fill failed on the
        first one that arrived (found 2026-08-23 by running the chain as
        processes; the single-process test could not see it, because there the
        objects were passed by hand).

        The verdict is not lost. It is what `describe_reconciliation` reports and
        what `standing.divergences` counts, which is where a judgement about the
        data belongs -- on the part's own health, not inside the data.
        """
        fills, reports = read_fills_and_reports()
        for fill in fills:
            reconciler.observe_fill(fill)
        for venue_id, symbol, quantity in reports:
            reconciler.observe_venue_report(venue_id, symbol, quantity)
        publish_positions(tuple(
            reconciliation.position for reconciliation in reconciler.reconcile_all()
        ))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    This is where a fill becomes a position, and therefore where every part that
    watches an open trade gets something to watch. Nothing downstream of a fill --
    the excursion tracker, the stop manager, the invalidation watcher, the close
    detector -- has an input until this part is running.

    `venue-position-report` has no producer while the segment is on paper: there
    is no venue holding a paper position to ask. So every reconciliation is
    reported UNCHECKED, which is the honest state and is not the same as agreed.
    The comparison switches itself on the moment `venue-position-reader` runs.
    """
    from runtime.input_assembly import Batch

    fills = Batch(read=context.bus.reader("fill"))
    reports = Batch(read=context.bus.reader("venue-position-report"))
    publish_positions = context.bus.publisher_for("position")

    def read_fills_and_reports():
        venue_quantities = tuple(
            (report.venue_id, report.symbol, report.quantity) for report in reports.payloads()
        )
        return tuple(fills.payloads()), venue_quantities

    return run_fill_reconciler(
        reconciler=FillReconciler(
            # How far our quantity may sit from the venue's before it is a
            # divergence rather than rounding. Read from the increment the venue
            # itself publishes, because a tolerance smaller than one tradeable
            # step would report every position as diverged, and one larger than a
            # step would hide a genuinely missing fill.
            quantity_tolerance=context.number("order_quantity_increment"),
        ),
        control_socket=context.control_socket,
        read_fills_and_reports=read_fills_and_reports,
        publish_positions=publish_positions,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
