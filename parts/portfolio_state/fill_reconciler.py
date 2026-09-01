"""fill-reconciler: the held position from fills, checked against the venue's own."""

from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field

from runtime.durable_state import (
    CheckpointSchedule,
    DurableStateStore,
    restore_and_arm_checkpoint,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, Position

# One checkpoint per part per component; this part keeps exactly one thing.
CHECKPOINT_COMPONENT = "held-positions"

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


def position_key_text(key: tuple[str, str]) -> str:
    """One position's key as one string, for a JSON object that has only strings.

    The same separator the lot books and the resting exits use, so the position
    checkpoints written by three different parts read alike.
    """
    return f"{key[0]}|{key[1]}"


def position_key_of(text: str) -> tuple[str, str]:
    venue_id, _, symbol = text.partition("|")
    return venue_id, symbol


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
    # Not counters: what happened to the checkpoint at start. A reconciler that
    # came back holding nothing and one whose checkpoint could not be read are
    # different facts, and only the second is a fault (Rule 8).
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


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

    def read_checkpoint_state(self) -> dict:
        """What is held, to carry into the next process.

        This part starts every other part that watches an open trade -- its own
        docstring says so -- and it held its positions in memory alone until
        2026-08-26. So every restart began with nothing held, and since a position
        is only learned from a *fill*, a position already open when the process
        started was invisible for the rest of that process's life.

        Measured on the live spine at 11:47 on 2026-08-26, with 20 positions open
        and restored by the two parts that did checkpoint:

            fill-reconciler          received nothing, published no position
            peak-excursion-tracker   positions_tracked 0, prices_without_cost_basis
                                     162,960 -- so the excursion on the board was
                                     frozen at whatever it last was, and 16 of 20
                                     rows contradicted their own live P&L
            stop-order-manager       0 of 20 positions had a stop resting
            exposure-limiter         positions_seen 0, total_exposure 0
            margin-liquidation-watch positions_watched 0

        `seen_fills` rides along so a fill replayed across a restart is still
        recognised as one already applied; without it a restart could double a
        position. The venue quantities do not: they are what the venue said, and
        the venue is asked again rather than remembered.
        """
        return {
            "positions": {
                position_key_text(key): {
                    "venue_id": held.venue_id,
                    "symbol": held.symbol,
                    "quantity": held.quantity,
                    "average_entry_price": held.average_entry_price,
                    "realised_pnl": held.realised_pnl,
                    "fees_paid": held.fees_paid,
                    "opened_at_ns": held.opened_at_ns,
                    "updated_at_ns": held.updated_at_ns,
                    # What it was opened at. Without this a restored position has
                    # no leverage anywhere -- `leverage-selector` answers while an
                    # intent is being formed and never again -- and a position
                    # whose leverage nobody knows has no computable liquidation
                    # price, which stops new risk on its symbol.
                    "leverage": held.leverage,
                }
                for key, held in self._positions.items()
            },
            "seen_fills": sorted(self._seen_fills),
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Rebuild what is held. Returns how many symbols came back."""
        self._positions = {
            position_key_of(text): Position(
                venue_id=str(held["venue_id"]),
                symbol=str(held["symbol"]),
                quantity=float(held["quantity"]),
                average_entry_price=float(held["average_entry_price"]),
                realised_pnl=float(held["realised_pnl"]),
                fees_paid=float(held["fees_paid"]),
                opened_at_ns=int(held["opened_at_ns"]),
                updated_at_ns=int(held["updated_at_ns"]),
                # Absent in a checkpoint written before positions carried it, and
                # absent is None: a position restored as 1x would be handed a
                # liquidation price computed from a leverage nobody recorded.
                leverage=(
                    float(held["leverage"]) if held.get("leverage") is not None else None
                ),
            )
            for text, held in (state.get("positions") or {}).items()
        }
        self._seen_fills = set(state.get("seen_fills") or ())
        self.standing.symbols = len(self._positions)
        return self.standing.symbols

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
                # Carried off the fill, which is the only place it survives the
                # decision that chose it.
                leverage=getattr(fill, "leverage", None),
            )
        else:
            position = self._apply(held, fill)
        self._positions[key] = position
        self.standing.symbols = len(self._positions)
        return position

    def _apply(self, held: Position, fill) -> Position:
        signed = fill.signed_quantity
        new_quantity = held.quantity + signed
        # A reopen starts a new round trip: its realised P&L and fees begin at
        # zero and this fill's own, never at what the last round trip on this
        # symbol finished with. `opened_at_ns` and `leverage` already special-
        # case `held.quantity == 0` below for the same reason; these two did
        # not, so `bull-position-invalidation-watcher` and
        # `bear-position-invalidation-watcher` scored a close's win/loss off
        # `position.realised_pnl > 0` -- the symbol's lifetime total across
        # every round trip this process had made, not this one's result.
        realised = 0.0 if held.quantity == 0 else held.realised_pnl
        fees_paid = fill.fee if held.quantity == 0 else held.fees_paid + fill.fee
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
            fees_paid=fees_paid,
            opened_at_ns=held.opened_at_ns if held.quantity != 0 else fill.filled_at_ns,
            updated_at_ns=fill.filled_at_ns,
            # A position reopened from flat takes the new fill's leverage; one
            # being added to or reduced keeps what it was opened at, because that
            # is the leverage its margin was posted at.
            leverage=(
                getattr(fill, "leverage", None) if held.quantity == 0 else held.leverage
            ),
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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
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
        # After the publish, never before: a checkpoint written first would record
        # a position no consumer had been told about yet. Only on a tick that
        # applied a fill -- the positions are republished every tick regardless,
        # and rewriting the file at that rate would record a book that had not
        # moved.
        if fills and write_checkpoint is not None:
            write_checkpoint(reconciler.standing.fills_applied)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_reconciliation(reconciler),
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

    # What is held, carried across a restart. This part is where a fill becomes a
    # position, so a position it has forgotten is a position nothing downstream
    # can see -- and it learns a position only from a *fill*, which for one
    # already open will never arrive again. Beside the lot books under
    # position_state_root, because it is the same fact about the same position.
    #
    # Nothing extra is needed to tell the rest of the system: `tick` republishes
    # every held position on every tick, so the restored book reaches the
    # excursion tracker, the stop manager, the exposure limiter and the margin
    # watch on the first tick after start.
    reconciler = FillReconciler(
        # How far our quantity may sit from the venue's before it is a
        # divergence rather than rounding. Read from the increment the venue
        # itself publishes, because a tolerance smaller than one tradeable
        # step would report every position as diverged, and one larger than a
        # step would hide a genuinely missing fill.
        quantity_tolerance=context.number("order_quantity_increment"),
    )
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        # Every fill. A position changes tens of times an hour and losing one
        # costs the whole book its meaning -- the same reasoning the lot books
        # use, and this is the same fact about the same position.
        CheckpointSchedule(1),
        PART_ID,
        CHECKPOINT_COMPONENT,
        reconciler,
        {"quantity_tolerance": float(context.number("order_quantity_increment"))},
    )

    def read_fills_and_reports():
        venue_quantities = tuple(
            (report.venue_id, report.symbol, report.quantity) for report in reports.payloads()
        )
        return tuple(fills.payloads()), venue_quantities

    return run_fill_reconciler(
        reconciler=reconciler,
        control_socket=context.control_socket,
        read_fills_and_reports=read_fills_and_reports,
        publish_positions=publish_positions,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
