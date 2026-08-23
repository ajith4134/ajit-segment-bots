"""self-modification-journal: written before the change, or the change does not happen.

A system that can change itself and cannot say what it changed is a system nobody can
debug, and after enough undocumented changes nobody can explain its current behaviour
either. This journal is the thing that stops that, and its single most important
property is ordering: **the record is written before the change takes effect.**

Written afterwards, a change that crashes the system leaves no trace of itself, and
the crash is investigated as a mystery. Written before, the last entry names the
change that broke it.

Four rules:

- **Append-only.** No entry is ever edited or removed. A journal that can be tidied is
  one where the embarrassing entry disappears, and the embarrassing entry is the one
  worth keeping.
- **Every entry records the before as well as the after.** Without the before there is
  no rollback, only a guess at what things used to be.
- **An entry names who authorised it** -- which envelope level, which policy decision.
  A change nobody authorised is a defect regardless of what it did.
- **Taking effect is recorded separately from the entry.** A change written and never
  applied is a different situation from one applied, and conflating them means a
  failed application looks like a successful one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import ModificationRecord
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "self-modification-journal"

PART_DECLARATION = PartDeclaration(
    part_id="self-modification-journal",
    consumes=("admitted-part", "policy-decision"),
    produces=("modification-record", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED = "recorded"
APPLIED = "applied"
NOT_AUTHORISED = "no-policy-decision-authorised-this"
NOT_RECORDED_FIRST = "this-change-took-effect-without-being-recorded-first"
NO_SUCH_RECORD = "no-such-record"
NOT_REVERSIBLE = "no-before-state-was-recorded-so-there-is-nothing-to-roll-back-to"


@dataclass(frozen=True)
class JournalOutcome:
    record_id: str | None
    state: str
    record: ModificationRecord | None
    reason: str
    at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.record is not None


@dataclass
class JournalStanding:
    records_written: int = 0
    changes_applied: int = 0
    unauthorised_refused: int = 0
    applied_without_a_record: int = 0
    irreversible_records: int = 0
    rollbacks: int = 0
    edits_attempted: int = 0
    deletions_attempted: int = 0


class SelfModificationJournal:
    """Append-only, written first, with the before state kept for every change."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._records: list = []
        self._by_id: dict[str, ModificationRecord] = {}
        self._authorisations: dict[str, str] = {}
        self._sequence = 0
        self.standing = JournalStanding()

    def observe_authorisation(self, subject: str, envelope_level: str) -> None:
        """A policy decision permitting a change. Without one, nothing is recorded."""
        self._authorisations[subject] = envelope_level

    def record(
        self, part_id: str, change: str, before: str | None, after: str,
        authorised_by: str,
    ) -> JournalOutcome:
        envelope_level = self._authorisations.get(authorised_by)
        if envelope_level is None:
            self.standing.unauthorised_refused += 1
            return self._outcome(
                None, NOT_AUTHORISED, None,
                f"no policy decision authorised {authorised_by}. A change nobody "
                f"authorised is a defect regardless of what it did",
            )

        self._sequence += 1
        record = ModificationRecord(
            record_id=f"modification-{self._sequence}",
            part_id=part_id,
            change=change,
            before=before,
            after=after,
            authorised_by=authorised_by,
            envelope_level=envelope_level,
            took_effect_at_ns=None,
            recorded_at_ns=self._now_ns(),
        )
        self._records.append(record)
        self._by_id[record.record_id] = record
        self.standing.records_written += 1
        if before is None:
            self.standing.irreversible_records += 1

        return self._outcome(
            record.record_id, RECORDED, record,
            f"{change} to {part_id} recorded before it takes effect. Written afterwards, "
            f"a change that crashes the system leaves no trace of itself"
            + (
                ". No before state was recorded, so this cannot be rolled back"
                if before is None
                else ""
            ),
        )

    def mark_applied(self, record_id: str) -> JournalOutcome:
        """Separate from recording: a change written and never applied is not the same."""
        record = self._by_id.get(record_id)
        if record is None:
            self.standing.applied_without_a_record += 1
            return self._outcome(
                record_id, NOT_RECORDED_FIRST, None,
                "this change took effect without being recorded first. The record exists "
                "so the last entry names the change that broke things",
            )

        applied = ModificationRecord(
            record_id=record.record_id, part_id=record.part_id, change=record.change,
            before=record.before, after=record.after, authorised_by=record.authorised_by,
            envelope_level=record.envelope_level, took_effect_at_ns=self._now_ns(),
            recorded_at_ns=record.recorded_at_ns,
        )
        # Append-only: the applied record is a new entry, the original stays.
        self._records.append(applied)
        self._by_id[record.record_id] = applied
        self.standing.changes_applied += 1
        return self._outcome(
            record.record_id, APPLIED, applied,
            f"{record.change} to {record.part_id} took effect "
            f"{(applied.took_effect_at_ns - record.recorded_at_ns) / 1e9:.3f}s after being "
            f"recorded",
        )

    def rollback_target(self, part_id: str) -> JournalOutcome:
        """What this part looked like before its last applied change."""
        for record in reversed(self._records):
            if record.part_id == part_id and record.took_effect_at_ns is not None:
                if not record.is_reversible:
                    return self._outcome(
                        record.record_id, NOT_REVERSIBLE, record,
                        "no before state was recorded, so there is only a guess at what "
                        "things used to be",
                    )
                self.standing.rollbacks += 1
                return self._outcome(
                    record.record_id, RECORDED, record,
                    f"the state before {record.change} is available for rollback",
                )
        return self._outcome(
            None, NO_SUCH_RECORD, None, f"no applied change is recorded for {part_id}",
        )

    def entries(self) -> tuple:
        return tuple(self._records)

    def history_for(self, part_id: str) -> tuple:
        return tuple(record for record in self._records if record.part_id == part_id)

    def _outcome(self, record_id, state, record, reason) -> JournalOutcome:
        return JournalOutcome(
            record_id=record_id, state=state, record=record, reason=reason,
            at_ns=self._now_ns(),
        )


def describe_journal(journal: SelfModificationJournal) -> dict:
    return {
        "part_id": PART_ID,
        "records_written": journal.standing.records_written,
        "changes_applied": journal.standing.changes_applied,
        "unauthorised_refused": journal.standing.unauthorised_refused,
        "applied_without_a_record": journal.standing.applied_without_a_record,
        "irreversible_records": journal.standing.irreversible_records,
        "rollbacks": journal.standing.rollbacks,
        "entries": len(journal.entries()),
        "can_edit_an_entry": False,
        "edits_attempted": journal.standing.edits_attempted,
        "can_delete_an_entry": False,
        "deletions_attempted": journal.standing.deletions_attempted,
        "records_after_the_change": False,
    }


def run_self_modification_journal(
    journal: SelfModificationJournal, control_socket, read_changes, publish_records,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_changes():
            outcome = journal.record(**job)
            if outcome.is_usable:
                publish_records(outcome.record)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
