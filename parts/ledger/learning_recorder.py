"""learning-recorder: what was learned, researched, forecast and scored.

The journal the learning loop is graded against. Everything it records is a claim
the system made about itself -- a finding, a forecast, a score, a rationale -- and
the only thing that makes those worth anything later is that they were written
down *before* the outcome was known.

So this recorder stamps every entry with what was knowable at the time and
refuses to accept a claim backdated to look prescient. A forecast that can be
edited after the fact is not a forecast, and a learning loop grading itself on
edited forecasts learns nothing except that it is always right.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "learning-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="learning-recorder",
    consumes=(
        "research-finding", "forecast-accuracy", "ablation-scorecard",
        "skill-usefulness", "opportunity-instruction", "decision-rationale",
        "pnl-attribution",
    ),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED_KINDS = (
    "research-finding", "forecast-accuracy", "ablation-scorecard",
    "skill-usefulness", "opportunity-instruction", "decision-rationale",
    # Added 2026-08-25. An attribution is what was scored, and it is the one
    # conclusion this recorder's own block exists to produce. Until then it lived
    # on the bus only, for as long as the producing process lived: the attribution
    # of a real closed trade, residual share 0.32, was computed and then vanished
    # (docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md).
    "pnl-attribution",
)

# Claims that are only meaningful if they were made before their subject
# resolved. These carry a `subject_resolved_at_ns` and are refused when the claim
# arrives stamped later than the thing it claims to be about.
FORWARD_LOOKING_KINDS = ("forecast-accuracy", "opportunity-instruction", "decision-rationale")


@dataclass
class LearningStanding:
    recorded: int = 0
    unknown_kinds: int = 0
    backdated_refused: int = 0
    by_kind: dict = field(default_factory=dict)
    last_refusal: str | None = None


class LearningRecorder:
    """Journals the system's claims about itself, with the time they were made."""

    def __init__(self, journal: Journal, now_ns=None) -> None:
        import time

        self._journal = journal
        self._now_ns = now_ns or time.time_ns
        self.standing = LearningStanding()

    def record(self, kind: str, payload: dict, subject_resolved_at_ns: int | None = None) -> JournalEntry | None:
        """Journal one claim, refusing a forward-looking one that arrives late.

        `subject_resolved_at_ns` is when the thing being claimed about became
        known -- when the forecast's window closed, when the trade the rationale
        justifies was filled. A claim recorded after that is not evidence about
        foresight, and letting it in would quietly corrupt every accuracy score
        computed from this journal.
        """
        if kind not in RECORDED_KINDS:
            self.standing.unknown_kinds += 1
            self.standing.last_refusal = f"{kind!r} is not a learning record"
            return None

        recorded_at = self._now_ns()
        if kind in FORWARD_LOOKING_KINDS and subject_resolved_at_ns is not None:
            if recorded_at > subject_resolved_at_ns:
                self.standing.backdated_refused += 1
                self.standing.last_refusal = (
                    f"{kind} arrived {(recorded_at - subject_resolved_at_ns) / 1e9:.1f}s after its "
                    f"subject resolved; a claim made after the fact is not a forecast"
                )
                return None

        entry = self._journal.append(
            kind=kind,
            part_id=PART_ID,
            payload={
                **payload,
                "claimed_at_ns": recorded_at,
                "subject_resolved_at_ns": subject_resolved_at_ns,
                "is_forward_looking": kind in FORWARD_LOOKING_KINDS,
            },
        )
        self.standing.recorded += 1
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1
        return entry


def describe_learning(recorder: LearningRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "recorded": recorder.standing.recorded,
        "unknown_kinds_refused": recorder.standing.unknown_kinds,
        "backdated_refused": recorder.standing.backdated_refused,
        "by_kind": dict(recorder.standing.by_kind),
        "last_refusal": recorder.standing.last_refusal,
        "records": list(RECORDED_KINDS),
        "forward_looking": list(FORWARD_LOOKING_KINDS),
    }


def run_learning_recorder(
    recorder: LearningRecorder, control_socket, read_claims, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        entries = [
            entry
            for kind, payload, resolved_at in read_claims()
            if (entry := recorder.record(kind, payload, resolved_at)) is not None
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
        read_standing=lambda: describe_learning(recorder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Seven kinds of claim, each journalled under its type name with the claim's
    fields as payload. The forward-looking kinds carry the time their subject
    resolved where the type states one; a claim stamped after its subject
    resolved is refused by the recorder, which is what it is for.
    """
    from dataclasses import asdict, is_dataclass

    from runtime.input_assembly import Batch
    import pathlib as _pathlib

    from runtime.journal import (
        JOURNAL_SEGMENT_BYTES_SETTING,
        Journal,
        RollingJournalSink,
        journal_path_for,
        read_journal_tail,
    )

    journal_path = journal_path_for(
        _pathlib.Path(str(context.setting("journal_path").value)).expanduser(), PART_ID
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

    journal = Journal(append_line=append_line, continues_from=tail)

    sources = {kind: Batch(read=context.bus.reader(kind)) for kind in RECORDED_KINDS}
    publish_entries = context.bus.publisher_for("journal-entry")

    def as_payload(item) -> dict:
        return asdict(item) if is_dataclass(item) else {"value": repr(item)}

    def resolved_at(item):
        return getattr(item, "subject_resolved_at_ns", None)

    def read_claims():
        return tuple(
            (kind, as_payload(item), resolved_at(item))
            for kind, source in sources.items() for item in source.payloads()
        )

    def publish(entries) -> None:
        if entries:
            publish_entries(entries)

    return run_learning_recorder(
        recorder=LearningRecorder(journal=journal),
        control_socket=context.control_socket,
        read_claims=read_claims,
        publish_entries=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
