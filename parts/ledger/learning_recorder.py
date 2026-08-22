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
    ),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED_KINDS = (
    "research-finding", "forecast-accuracy", "ablation-scorecard",
    "skill-usefulness", "opportunity-instruction", "decision-rationale",
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

    def claims_of_kind(self, kind: str) -> tuple[JournalEntry, ...]:
        return tuple(
            entry
            for entry in self._journal.entries
            if entry.part_id == PART_ID and entry.kind == kind
        )


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
    )
