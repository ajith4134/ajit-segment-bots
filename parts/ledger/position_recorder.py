"""position-recorder: positions as they open, change and close.

The journal of what was actually held, which is a different question from what
was ordered. `trade-lifecycle-recorder` answers "what did we decide"; this
answers "what did we own, and when", and the two disagree exactly when something
went wrong -- a fill that never became a position, a position that changed with no
fill behind it.

It journals a change rather than a state. A recorder that wrote the position on
every tick would fill the journal with a thousand identical entries a minute and
bury the moments that mattered; one that wrote only on close would lose the path
the position took to get there. So it writes when the held quantity actually
moves, and says by how much.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "position-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="position-recorder",
    consumes=("position", "closed-trade", "peak-excursion"),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

OPENED = "position-opened"
CHANGED = "position-changed"
CLOSED = "position-closed"
TRADE_CLOSED = "closed-trade"
EXCURSION = "peak-excursion"


@dataclass
class PositionRecorderStanding:
    recorded: int = 0
    opened: int = 0
    changed: int = 0
    closed: int = 0
    unchanged_skipped: int = 0
    closed_trades: int = 0
    excursions: int = 0


class PositionRecorder:
    """Journals a position each time the quantity held actually changes."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._held: dict[tuple[str, str], float] = {}
        self.standing = PositionRecorderStanding()

    def record_position(self, position) -> JournalEntry | None:
        """Journal this position if it differs from what was last journalled."""
        key = (position.venue_id, position.symbol)
        previous = self._held.get(key)

        if previous is not None and previous == position.quantity:
            self.standing.unchanged_skipped += 1
            return None

        if previous is None or previous == 0:
            kind = OPENED
            self.standing.opened += 1
        elif position.is_flat:
            kind = CLOSED
            self.standing.closed += 1
        else:
            kind = CHANGED
            self.standing.changed += 1

        self._held[key] = position.quantity
        return self._append(
            kind,
            {
                "venue_id": position.venue_id,
                "symbol": position.symbol,
                "quantity": position.quantity,
                "previous_quantity": previous,
                "direction": position.direction,
                "average_entry_price": position.average_entry_price,
                "realised_pnl": position.realised_pnl,
                "fees_paid": position.fees_paid,
                "updated_at_ns": position.updated_at_ns,
            },
        )

    def record_closed_trade(self, trade) -> JournalEntry:
        """Journal a completed round trip, with its excursion if one was tracked.

        Separate from the close of a position: a position closes when quantity
        reaches zero, and a closed trade is the round trip that produced it,
        carrying entry, exit and how far it travelled between them.
        """
        self.standing.closed_trades += 1
        return self._append(
            TRADE_CLOSED,
            {
                "venue_id": trade.venue_id,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "realised_pnl": trade.realised_pnl,
                "fees_paid": trade.fees_paid,
                "holding_seconds": trade.holding_seconds,
                "best_unrealised": trade.best_unrealised,
                "worst_unrealised": trade.worst_unrealised,
                "opened_at_ns": trade.opened_at_ns,
                "closed_at_ns": trade.closed_at_ns,
            },
        )

    def record_excursion(self, excursion) -> JournalEntry:
        """Journal how far an open position travelled, best and worst (RL-042)."""
        self.standing.excursions += 1
        return self._append(
            EXCURSION,
            {
                "venue_id": excursion.venue_id,
                "symbol": excursion.symbol,
                "best_unrealised": excursion.best_unrealised,
                "worst_unrealised": excursion.worst_unrealised,
                "best_price": excursion.best_price,
                "worst_price": excursion.worst_price,
                "samples": excursion.samples,
            },
        )

    def _append(self, kind: str, payload: dict) -> JournalEntry:
        self.standing.recorded += 1
        return self._journal.append(kind=kind, part_id=PART_ID, payload=payload)


def describe_positions(recorder: PositionRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "recorded": recorder.standing.recorded,
        "opened": recorder.standing.opened,
        "changed": recorder.standing.changed,
        "closed": recorder.standing.closed,
        "unchanged_skipped": recorder.standing.unchanged_skipped,
        "closed_trades": recorder.standing.closed_trades,
        "excursions": recorder.standing.excursions,
    }


def run_position_recorder(
    recorder: PositionRecorder, control_socket, read_events, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        positions, closed_trades, excursions = read_events()
        entries = [
            entry for position in positions if (entry := recorder.record_position(position))
        ]
        entries += [recorder.record_closed_trade(trade) for trade in closed_trades]
        entries += [recorder.record_excursion(excursion) for excursion in excursions]
        publish_entries(tuple(entries))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The record of what was actually held, what it became, and how far it went
    while it was open. `trade-lifecycle-recorder` journals the decision that
    opened a trade; this journals the position that decision produced and the
    closed trade it ended as, which is the half a reader needs to check one
    against the other.

    It writes into the same journal file, continuing the same hash chain, because
    two files would be two chains and a reader could not tell whether an entry
    missing from one was deleted or simply belonged to the other.
    """
    import pathlib

    from runtime.input_assembly import Batch
    from runtime.journal import Journal, journal_path_for, read_journal_tail

    positions = Batch(read=context.bus.reader("position"))
    closed_trades = Batch(read=context.bus.reader("closed-trade"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    publish_entries = context.bus.publisher_for("journal-entry")

    # This recorder's own file, beside the base the settings name. One writer per
    # chain: two processes appending to one file interleave, and every entry then
    # points at whatever the other wrote last, which is no chain at all.
    journal_path = journal_path_for(
        pathlib.Path(str(context.setting("journal_path").value)).expanduser(),
        "position-recorder",
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append_line(line: str) -> None:
        # Opened per append and flushed, for the same reason the lifecycle
        # recorder does it: a part is killed the way every part is killed, and a
        # buffered ledger loses exactly the entries that were about to matter.
        with open(journal_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def read_events():
        return (
            tuple(positions.payloads()),
            tuple(closed_trades.payloads()),
            tuple(excursions.payloads()),
        )

    return run_position_recorder(
        recorder=PositionRecorder(
            journal=Journal(
                append_line=append_line,
                continues_from=read_journal_tail(journal_path),
            )
        ),
        control_socket=context.control_socket,
        read_events=read_events,
        publish_entries=publish_entries,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
