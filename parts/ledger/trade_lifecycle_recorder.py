"""trade-lifecycle-recorder: every step from candidate to fill, in order.

This is the one journal that has to answer "why did this trade happen". A fill on
its own says what was bought; the chain that produced it -- the candidate that was
noticed, the intent formed from it, the bounds risk put on it, the order that was
sent -- is what makes the outcome attributable to a decision rather than to luck.

So it records the stages of one trade as a chain, not as five unrelated events.
Each entry carries the trade's own correlation id, and the recorder refuses a
stage that arrives out of order rather than journaling a lifecycle that never
happened in that sequence: a journal whose order is wrong is worse than a gap in
it, because a later phase reading it would learn the wrong causal story.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-lifecycle-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="trade-lifecycle-recorder",
    consumes=("entry-candidate", "trade-intent", "bounded-order", "order-request", "fill"),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# The stages of one trade, in the order they must occur. A candidate becomes an
# intent, risk bounds it, it is sent, it fills. Position in this tuple is the
# rule the recorder checks against -- there is no separate ordering table to fall
# out of step with the data types this part actually consumes.
LIFECYCLE_STAGES = ("entry-candidate", "trade-intent", "bounded-order", "order-request", "fill")

# A fill may arrive more than once for one order -- partial fills are ordinary --
# so it is the one stage that may repeat without being an ordering fault.
REPEATABLE_STAGES = ("fill",)


@dataclass
class LifecycleStanding:
    recorded: int = 0
    out_of_order: int = 0
    unknown_stages: int = 0
    trades_started: int = 0
    trades_filled: int = 0
    stage_counts: dict = field(default_factory=dict)
    last_refusal: str | None = None


class TradeLifecycleRecorder:
    """Journals each stage of a trade, keeping the chain in the order it happened."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._furthest_stage: dict[str, int] = {}
        # The distinct stages recorded per trade, in first-recorded order, kept
        # here rather than read back out of the journal: a journal with a sink
        # retains nothing, and this recorder's journal held 3.6 GiB in memory
        # when it did. Distinct on purpose -- at most five entries per trade,
        # where a list of every repeat would regrow the same leak one symbol's
        # stream of entry-candidates at a time.
        self._stages_of: dict[str, list[str]] = {}
        self.standing = LifecycleStanding()

    def record(self, stage: str, trade_id: str, payload: dict) -> JournalEntry | None:
        """Journal one stage of one trade, or refuse it and say why.

        `trade_id` correlates the stages of a single trade. Without it the
        journal would hold five streams of unrelated events and nothing could
        reconstruct which candidate became which fill.
        """
        if stage not in LIFECYCLE_STAGES:
            self.standing.unknown_stages += 1
            self.standing.last_refusal = f"{stage!r} is not a stage of a trade's lifecycle"
            return None

        position = LIFECYCLE_STAGES.index(stage)
        furthest = self._furthest_stage.get(trade_id)

        if furthest is None:
            if position != 0:
                # A trade first seen mid-lifecycle is still recorded: refusing it
                # would lose a real fill because this part started late. What is
                # refused is a stage going *backwards*, which cannot have happened.
                self._furthest_stage[trade_id] = position
                return self._append(stage, trade_id, payload, note="lifecycle joined in progress")
            self.standing.trades_started += 1
            self._furthest_stage[trade_id] = position
            return self._append(stage, trade_id, payload)

        if position < furthest and stage not in REPEATABLE_STAGES:
            self.standing.out_of_order += 1
            self.standing.last_refusal = (
                f"{trade_id}: {stage} arrived after {LIFECYCLE_STAGES[furthest]}, "
                f"which is backwards through the lifecycle"
            )
            return None

        self._furthest_stage[trade_id] = max(furthest, position)
        return self._append(stage, trade_id, payload)

    def _append(self, stage: str, trade_id: str, payload: dict, note: str | None = None) -> JournalEntry:
        entry = self._journal.append(
            kind=stage,
            part_id=PART_ID,
            payload={"trade_id": trade_id, **payload, **({"note": note} if note else {})},
        )
        self.standing.recorded += 1
        self.standing.stage_counts[stage] = self.standing.stage_counts.get(stage, 0) + 1
        stages = self._stages_of.setdefault(trade_id, [])
        if stage not in stages:
            stages.append(stage)
        if stage == "fill":
            self.standing.trades_filled += 1
        return entry

    def stages_recorded_for(self, trade_id: str) -> tuple[str, ...]:
        """The distinct stages of one trade this recorder has journalled, in order."""
        return tuple(self._stages_of.get(trade_id, ()))


def describe_lifecycle(recorder: TradeLifecycleRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "recorded": recorder.standing.recorded,
        "trades_started": recorder.standing.trades_started,
        "trades_filled": recorder.standing.trades_filled,
        "out_of_order_refused": recorder.standing.out_of_order,
        "unknown_stages_refused": recorder.standing.unknown_stages,
        "stage_counts": dict(recorder.standing.stage_counts),
        "last_refusal": recorder.standing.last_refusal,
    }


def run_trade_lifecycle_recorder(
    recorder: TradeLifecycleRecorder, control_socket, read_stages, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        entries = [
            entry
            for stage, trade_id, payload in read_stages()
            if (entry := recorder.record(stage, trade_id, payload)) is not None
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
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The five stages of a trade, journalled in order and correlated by one id. The
    id is the client order id wherever the order has been stamped, and the symbol
    before that: the stamper is what gives a trade its identity, and the stages
    before it can only be correlated by what they are about.

    The journal writes to a file as well as holding the chain in memory, because a
    ledger that only exists in a process is a ledger that a restart erases -- and
    this is the record of what the system decided, which is the one thing that
    cannot be re-derived from the tape.
    """
    import pathlib
    from dataclasses import asdict, is_dataclass

    from runtime.input_assembly import Batch
    from runtime.journal import Journal, journal_path_for, read_journal_tail

    stages = {
        stage: Batch(read=context.bus.reader(stage))
        for stage in LIFECYCLE_STAGES
        if stage in context.declaration.consumes
    }
    publish_entries = context.bus.publisher_for("journal-entry")

    # This recorder's own file, beside the base the settings name. One writer per
    # chain: two processes appending to one file interleave, and every entry then
    # points at whatever the other wrote last, which is no chain at all.
    journal_path = journal_path_for(
        pathlib.Path(str(context.setting("journal_path").value)).expanduser(),
        "trade-lifecycle-recorder",
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append_line(line: str) -> None:
        # Opened per append and flushed: a recorder is killed the same way every
        # part is, and a buffered ledger loses exactly the entries that were about
        # to matter.
        with open(journal_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def as_payload(item) -> dict:
        return asdict(item) if is_dataclass(item) else {"value": repr(item)}

    def trade_id_of(item, stage: str) -> str:
        """What correlates the stages of one trade.

        The client order id once one exists; before that, the symbol it is about.
        A journal keyed by nothing would be five streams of unrelated events, which
        is the failure this argument exists to prevent.
        """
        stamped = getattr(item, "client_order_id", None)
        if stamped:
            return str(stamped)
        return f"{getattr(item, 'venue_id', 'unknown')}:{getattr(item, 'symbol', stage)}"

    def read_stages():
        recorded = []
        for stage, batch in stages.items():
            for item in batch.payloads():
                recorded.append((stage, trade_id_of(item, stage), as_payload(item)))
        return tuple(recorded)

    return run_trade_lifecycle_recorder(
        recorder=TradeLifecycleRecorder(
            journal=Journal(
                append_line=append_line,
                # Picked up from what the file already holds, so a restarted
                # recorder continues one chain rather than starting a second.
                # A ledger whose chain restarts every time the process does
                # detects an edit inside a run and nothing about a whole run
                # deleted.
                continues_from=read_journal_tail(journal_path),
            )
        ),
        control_socket=context.control_socket,
        read_stages=read_stages,
        publish_entries=publish_entries,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
