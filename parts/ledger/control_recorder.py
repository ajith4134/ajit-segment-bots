"""control-recorder: every gate flip, policy ruling and self-modification.

The journal of what the system did *to itself*, as distinct from what it did to
the market. It is the one an operator reads after something went wrong at three
in the morning, and the one an autonomous system's own changes have to survive in
whether or not anyone was watching.

Self-modification is the entry that matters most and is treated differently from
the rest: a modification record is journaled with the digest of what changed, so
a later reader can tell whether the change on disk is the change that was
recorded. Without that, a system that rewrites itself keeps a journal of what it
*intended* to do, which is exactly the account that fails when it matters.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "control-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="control-recorder",
    consumes=("switch-record", "policy-decision", "modification-record", "knowledge-snapshot"),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SWITCH_RECORD = "switch-record"
POLICY_DECISION = "policy-decision"
MODIFICATION_RECORD = "modification-record"
KNOWLEDGE_SNAPSHOT = "knowledge-snapshot"

RECORDED_KINDS = (SWITCH_RECORD, POLICY_DECISION, MODIFICATION_RECORD, KNOWLEDGE_SNAPSHOT)


@dataclass
class ControlStanding:
    recorded: int = 0
    unknown_kinds: int = 0
    switches: int = 0
    policy_decisions: int = 0
    modifications: int = 0
    snapshots: int = 0
    modifications_without_content: int = 0
    last_refusal: str | None = None


class ControlRecorder:
    """Journals control-plane events, digesting anything that changed the system."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self.standing = ControlStanding()

    # The content field of a `modification-record`: what was written, as written.
    # Named here rather than taken from a producer, because the digest is the whole
    # point of this kind and a recorder that could not find the content would
    # journal an account of intentions.
    MODIFICATION_CONTENT_FIELD = "after"

    COUNTER_FOR_KIND = {
        SWITCH_RECORD: "switches",
        POLICY_DECISION: "policy_decisions",
        MODIFICATION_RECORD: "modifications",
        KNOWLEDGE_SNAPSHOT: "snapshots",
    }

    def record(self, kind: str, payload: dict) -> JournalEntry | None:
        """The one way a control-plane event becomes an entry.

        Every field the producer carried is journaled as it arrived: a recorder
        that re-listed the fields it wanted would silently drop whatever a
        producer gained later, and the fields it dropped would be exactly the
        ones nobody thought to ask for.

        Two things happen here that a bare append does not do, and both were
        absent from every one of the 5,930 entries written before 2026-08-28:
        the kind is counted as its own kind, so a board can tell 3,691 policy
        rulings from 140 gate flips; and a modification is digested, so a later
        reader can check the change on disk against the change recorded.
        """
        if kind not in RECORDED_KINDS:
            self.standing.unknown_kinds += 1
            self.standing.last_refusal = f"{kind!r} is not a control-plane record"
            return None
        counter = self.COUNTER_FOR_KIND[kind]
        setattr(self.standing, counter, getattr(self.standing, counter) + 1)
        if kind == MODIFICATION_RECORD:
            payload = self._digested(payload)
        return self._append(kind, payload)

    def _digested(self, payload: dict) -> dict:
        """A modification record, with the digest of what was written added to it.

        Recorded without one when the content is absent -- losing the record
        entirely would be worse -- but counted separately, because that entry can
        never be matched against what is actually on disk.
        """
        content = payload.get(self.MODIFICATION_CONTENT_FIELD)
        if not isinstance(content, str):
            self.standing.modifications_without_content += 1
            return {**payload, "content_digest": None, "content_bytes": None}
        written = content.encode("utf-8")
        return {
            **payload,
            "content_digest": hashlib.sha256(written).hexdigest(),
            "content_bytes": len(written),
        }

    def _append(self, kind: str, payload: dict) -> JournalEntry:
        self.standing.recorded += 1
        return self._journal.append(kind=kind, part_id=PART_ID, payload=payload)

    def verify_modification(self, entry: JournalEntry, content_on_disk: str) -> bool:
        """Whether what is on disk now is what this entry says was written.

        The reason the digest is recorded at all: an account of self-modification
        that cannot be checked against reality is an account of intentions.
        """
        recorded = entry.payload.get("content_digest")
        if recorded is None:
            return False
        return recorded == hashlib.sha256(content_on_disk.encode("utf-8")).hexdigest()


def describe_control(recorder: ControlRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "recorded": recorder.standing.recorded,
        "switches": recorder.standing.switches,
        "policy_decisions": recorder.standing.policy_decisions,
        "modifications": recorder.standing.modifications,
        "modifications_without_content": recorder.standing.modifications_without_content,
        "knowledge_snapshots": recorder.standing.snapshots,
        "unknown_kinds_refused": recorder.standing.unknown_kinds,
        "last_refusal": recorder.standing.last_refusal,
    }


def run_control_recorder(
    recorder: ControlRecorder, control_socket, read_events, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        entries = [
            entry for kind, payload in read_events() if (entry := recorder.record(kind, payload))
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
        read_standing=lambda: describe_control(recorder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Its own journal file, one writer per chain, picked up from what the file
    already holds. Each control-plane event becomes one entry under the kind
    the recorder names it, with the event's own fields as the payload.
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

    sources = {
        SWITCH_RECORD: Batch(read=context.bus.reader("switch-record")),
        POLICY_DECISION: Batch(read=context.bus.reader("policy-decision")),
        MODIFICATION_RECORD: Batch(read=context.bus.reader("modification-record")),
        KNOWLEDGE_SNAPSHOT: Batch(read=context.bus.reader("knowledge-snapshot")),
    }
    publish_entries = context.bus.publisher_for("journal-entry")

    def as_payload(item) -> dict:
        return asdict(item) if is_dataclass(item) else {"value": repr(item)}

    def read_events():
        return tuple(
            (kind, as_payload(item)) for kind, source in sources.items() for item in source.payloads()
        )

    def publish(entries) -> None:
        if entries:
            publish_entries(entries)

    return run_control_recorder(
        recorder=ControlRecorder(journal=journal),
        control_socket=context.control_socket,
        read_events=read_events,
        publish_entries=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
