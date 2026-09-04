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
    consumes=("position", "closed-trade", "peak-excursion", "stop-adjustment"),
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
TRAILED = "stop-adjustment"


@dataclass
class PositionRecorderStanding:
    recorded: int = 0
    opened: int = 0
    changed: int = 0
    closed: int = 0
    unchanged_skipped: int = 0
    closed_trades: int = 0
    excursions: int = 0
    excursions_unchanged_skipped: int = 0
    stop_adjustments: int = 0
    stop_adjustments_held_skipped: int = 0


class PositionRecorder:
    """Journals a position each time the quantity held actually changes."""

    def __init__(self, journal: Journal, excursion_move_fraction: float) -> None:
        if not excursion_move_fraction > 0:
            raise ValueError(
                "excursion_move_fraction is the price move an extreme must make before it is "
                f"journalled again, as a fraction, and must be positive; got "
                f"{excursion_move_fraction!r}. Zero journals every twitch of the peak, which "
                f"wrote 8.59 million entries in two live days."
            )
        self._journal = journal
        self._excursion_move_fraction = excursion_move_fraction
        self._held: dict[tuple[str, str], float] = {}
        self._peaks: dict[tuple[str, str], tuple[float, float]] = {}
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
            # The next position on this symbol starts its own extremes; holding
            # the closed one's would swallow a first excursion that matched it.
            self._peaks.pop(key, None)
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

    def record_excursion(self, excursion) -> JournalEntry | None:
        """Journal how far an open position travelled, best and worst (RL-042).

        Only when either extreme has moved by `excursion_move_fraction` of its
        last journalled price. Skipping exact repeats was tried first (2026-08-24
        morning) and did not hold: an extreme is set by the market's newest best
        print, so on a trending symbol it moves on nearly every trade, and the
        journal took 8.59 million entries -- 4.2 GB, 99.9% of the file -- in two
        days anyway. This journal exists so a restarted spine recovers an open
        position's extremes; a peak recovered a fraction of a percent shy of the
        true one is the same recovery, and the per-price path stays the tape's.
        The extreme that ends the trade is exact regardless: the closed-trade
        entry carries the final best and worst.
        """
        key = (excursion.venue_id, excursion.symbol)
        previous = self._peaks.get(key)
        if previous is not None:
            previous_best, previous_worst = previous
            best_moved = abs(excursion.best_price - previous_best) >= abs(previous_best) * self._excursion_move_fraction
            worst_moved = abs(excursion.worst_price - previous_worst) >= abs(previous_worst) * self._excursion_move_fraction
            if not best_moved and not worst_moved:
                self.standing.excursions_unchanged_skipped += 1
                return None
        self._peaks[key] = (excursion.best_price, excursion.worst_price)
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

    def record_stop_adjustment(self, adjustment) -> JournalEntry | None:
        """Journal a lock's stop once it actually moves -- to break-even or trailed further.

        `stop-adjustment` is one wire carrying two shapes (documented in
        parts/paper_live_trading/stop_order_manager.py's own read_adjustment,
        after that ambiguity once dropped 34 real trails on the live spine):
        `exit-order-chainer`'s initial exits, named by `exit_side`/`stop_price`,
        and `profit-lock`'s trailing lock, named by `new_stop`/`direction`. Only
        the lock's shape belongs in this journal -- the initial exit is already
        covered by the fill it was chained from. `getattr` rather than
        `isinstance` for the same reason `read_adjustment` uses it: this part
        does not import either producer's type (T-4).

        `did_move` false is profit-lock saying nothing moved, which is not a
        change to record here either. Without this, the board's trailing
        column had no journalled record of a lock ever forming to read -- it
        could only ever show "not built" (found live 2026-08-30), because
        `stop-adjustment` was never on any recorder's consumes.
        """
        if getattr(adjustment, "exit_side", None) is not None:
            return None  # exit-order-chainer's shape; not this journal's concern
        new_stop = getattr(adjustment, "new_stop", None)
        if new_stop is None:
            return None  # unreadable; not this journal's job to guess at a shape
        if not getattr(adjustment, "did_move", False):
            self.standing.stop_adjustments_held_skipped += 1
            return None
        self.standing.stop_adjustments += 1
        return self._append(
            TRAILED,
            {
                "venue_id": adjustment.venue_id,
                "symbol": adjustment.symbol,
                "direction": adjustment.direction,
                "entry_price": adjustment.entry_price,
                "current_price": adjustment.current_price,
                "previous_stop": adjustment.previous_stop,
                "new_stop": new_stop,
                "outcome": adjustment.outcome,
                "gain_fraction": adjustment.gain_fraction,
                "locked_fraction": adjustment.locked_fraction,
                "adjusted_at_ns": adjustment.adjusted_at_ns,
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
        "excursions_unchanged_skipped": recorder.standing.excursions_unchanged_skipped,
        "stop_adjustments": recorder.standing.stop_adjustments,
        "stop_adjustments_held_skipped": recorder.standing.stop_adjustments_held_skipped,
    }


def run_position_recorder(
    recorder: PositionRecorder, control_socket, read_events, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        positions, closed_trades, excursions, stop_adjustments = read_events()
        entries = [
            entry for position in positions if (entry := recorder.record_position(position))
        ]
        entries += [recorder.record_closed_trade(trade) for trade in closed_trades]
        entries += [
            entry
            for excursion in excursions
            if (entry := recorder.record_excursion(excursion))
        ]
        entries += [
            entry
            for adjustment in stop_adjustments
            if (entry := recorder.record_stop_adjustment(adjustment))
        ]
        publish_entries(tuple(entries))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_positions(recorder),
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
    from runtime.journal import (
        JOURNAL_SEGMENT_BYTES_SETTING,
        Journal,
        RollingJournalSink,
        journal_path_for,
        read_journal_tail,
    )

    positions = Batch(read=context.bus.reader("position"))
    closed_trades = Batch(read=context.bus.reader("closed-trade"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    stop_adjustments = Batch(read=context.bus.reader("stop-adjustment"))
    publish_entries = context.bus.publisher_for("journal-entry")

    # This recorder's own file, beside the base the settings name. One writer per
    # chain: two processes appending to one file interleave, and every entry then
    # points at whatever the other wrote last, which is no chain at all.
    journal_path = journal_path_for(
        pathlib.Path(str(context.setting("journal_path").value)).expanduser(),
        "position-recorder",
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    # Rotation lives in the sink, not here: a segment at its bound is renamed to
    # carry the sequence it ended at and a new one starts, with the chain running
    # on into it. Nothing is deleted -- the journal is the only account of what the
    # system did (2026-09-04, when five journals held 38 GB with no trade placed).
    # Opened per append and flushed inside the sink: a recorder is killed the way
    # every part is, and a buffered ledger loses exactly the entries that were
    # about to matter.
    tail = read_journal_tail(journal_path)
    sink = RollingJournalSink(
        journal_path,
        maximum_bytes=int(context.number(JOURNAL_SEGMENT_BYTES_SETTING)),
        continues_from_sequence=tail.sequence if tail else 0,
    )
    append_line = sink.append_line

    def read_events():
        return (
            tuple(positions.payloads()),
            tuple(closed_trades.payloads()),
            tuple(excursions.payloads()),
            tuple(stop_adjustments.payloads()),
        )

    return run_position_recorder(
        recorder=PositionRecorder(
            journal=Journal(
                append_line=append_line,
                continues_from=tail,
            ),
            excursion_move_fraction=context.number("excursion_journal_move_fraction"),
        ),
        control_socket=context.control_socket,
        read_events=read_events,
        publish_entries=publish_entries,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
