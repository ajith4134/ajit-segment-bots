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

    def record_switch(self, switch_record) -> JournalEntry:
        """One gate flip, with its outcome -- a failed flip is as important as a good one."""
        self.standing.switches += 1
        return self._append(
            SWITCH_RECORD,
            {
                "part_id": switch_record.part_id,
                "action": switch_record.action,
                "outcome": switch_record.outcome,
                "reason": switch_record.reason,
                "attempted_at_ns": switch_record.attempted_at_ns,
                "completed_at_ns": switch_record.completed_at_ns,
            },
        )

    def record_policy_decision(self, policy: str, verdict: str, reason: str, subject: str) -> JournalEntry:
        """One ruling by a policy engine, with what it ruled on and why.

        The reason is required rather than optional: a policy log of verdicts
        without reasons cannot be audited, and an autonomous system's rulings are
        exactly the thing that has to be auditable after the fact.
        """
        self.standing.policy_decisions += 1
        return self._append(
            POLICY_DECISION,
            {"policy": policy, "verdict": verdict, "reason": reason, "subject": subject},
        )

    def record_modification(
        self, target: str, description: str, new_content: str | None, author: str
    ) -> JournalEntry:
        """One change the system made to itself, digested so it can be checked.

        The digest is of the content as written. A modification recorded without
        it is still journaled -- losing the record entirely would be worse -- but
        it is counted separately, because that entry cannot later be matched
        against what is actually on disk.
        """
        self.standing.modifications += 1
        digest = None
        if new_content is None:
            self.standing.modifications_without_content += 1
        else:
            digest = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        return self._append(
            MODIFICATION_RECORD,
            {
                "target": target,
                "description": description,
                "author": author,
                "content_digest": digest,
                "content_bytes": len(new_content.encode("utf-8")) if new_content else None,
            },
        )

    def record_knowledge_snapshot(self, snapshot_id: str, summary: str, item_count: int) -> JournalEntry:
        """A point the system's knowledge can be rolled back to."""
        self.standing.snapshots += 1
        return self._append(
            KNOWLEDGE_SNAPSHOT,
            {"snapshot_id": snapshot_id, "summary": summary, "item_count": item_count},
        )

    def record(self, kind: str, payload: dict) -> JournalEntry | None:
        """A generic path for a caller holding an already-shaped payload."""
        if kind not in RECORDED_KINDS:
            self.standing.unknown_kinds += 1
            self.standing.last_refusal = f"{kind!r} is not a control-plane record"
            return None
        return self._append(kind, payload)

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
    )
