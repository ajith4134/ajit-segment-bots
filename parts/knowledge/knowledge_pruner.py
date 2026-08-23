"""knowledge-pruner: removing what has stopped being worth keeping.

The only part allowed to delete. Everything else in the knowledge block lowers
confidence, records contradictions or supersedes -- deletion is concentrated here
because it is the one irreversible operation, and irreversible operations should
have exactly one place they can happen.

What earns removal, and what does not:

- **Useless, not merely old.** A tick size established two years ago and never
  rechecked is old and still correct. What earns removal is a fact nothing has
  read, whose confidence has decayed, and which contradicts something better
  sourced. Age alone is not evidence.
- **Superseded, with the supersession settled.** A fact whose replacement has
  itself been confirmed can go; one whose replacement is still contested cannot,
  because the contest may resolve the other way.
- **Derived from something invalidated.** A fact resting on a source found wrong
  is wrong, and this is the one case where removal is immediate.

**Nothing is pruned while anything still reads it.** A fact with recent reads is
in use whatever its confidence says, and removing it makes a live part fail for
a reason nobody will connect to this one.

**Every removal is recorded.** A pruner whose deletions leave no trace makes the
knowledge base's history unreconstructable, and the question after every surprise
is what the system used to believe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "knowledge-pruner"

PART_DECLARATION = PartDeclaration(
    part_id="knowledge-pruner",
    consumes=(
        "skill-usefulness", "semantic-fact", "instruction-scorecard", "fact-provenance",
        "knowledge-contradiction", "fact-confidence",
    ),
    produces=("stale-knowledge", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

KEPT = "kept"
PRUNED = "pruned"

USELESS = "nothing-reads-it-and-its-confidence-has-gone"
SUPERSEDED_AND_SETTLED = "its-replacement-has-been-confirmed"
RESTS_ON_SOMETHING_INVALIDATED = "an-ancestor-was-found-wrong"

STILL_IN_USE = "something-still-reads-it"
ONLY_OLD = "old-is-not-the-same-as-useless"
SUPERSESSION_UNSETTLED = "its-replacement-is-still-contested"


@dataclass(frozen=True)
class StaleKnowledge:
    """One piece of knowledge judged, and what happened to it."""

    knowledge_key: str
    kind: str
    state: str
    because: str
    confidence: float
    reads_recently: int
    age_seconds: float
    contradicted_by: str | None
    reason: str
    judged_at_ns: int

    @property
    def was_pruned(self) -> bool:
        return self.state == PRUNED


@dataclass
class PrunerStanding:
    judgements: int = 0
    pruned: int = 0
    kept: int = 0
    kept_because_in_use: int = 0
    kept_because_only_old: int = 0
    kept_because_supersession_unsettled: int = 0
    by_reason: dict = field(default_factory=dict)
    removals_recorded: int = 0


class KnowledgePruner:
    """The one place knowledge is deleted, and it records every deletion."""

    def __init__(
        self,
        confidence_floor: float,
        idle_seconds_before_useless: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= confidence_floor < 1.0:
            raise ValueError("the confidence floor is a fraction below one")
        if idle_seconds_before_useless <= 0:
            raise ValueError(
                "a fact with no idle period is never in use, and everything would be prunable"
            )
        self._confidence_floor = confidence_floor
        self._idle_seconds = idle_seconds_before_useless
        self._now_ns = now_ns
        self._reads: dict[str, list] = {}
        self._confidences: dict[str, float] = {}
        self._established: dict[str, int] = {}
        self._superseded_by: dict[str, str] = {}
        self._settled: set[str] = set()
        self._invalidated: set[str] = set()
        self._contradictions: dict[str, str] = {}
        self._removals: list = []
        self.standing = PrunerStanding()

    def observe_read(self, knowledge_key: str) -> None:
        """Something read this. A fact in use is in use whatever its confidence says."""
        self._reads.setdefault(knowledge_key, []).append(self._now_ns())

    def observe_confidence(self, knowledge_key: str, confidence: float) -> None:
        self._confidences[knowledge_key] = confidence

    def observe_established(self, knowledge_key: str, at_ns: int) -> None:
        self._established[knowledge_key] = at_ns

    def observe_supersession(self, knowledge_key: str, replacement_key: str, is_settled: bool) -> None:
        self._superseded_by[knowledge_key] = replacement_key
        if is_settled:
            self._settled.add(knowledge_key)
        else:
            self._settled.discard(knowledge_key)

    def observe_invalidated_ancestor(self, knowledge_key: str) -> None:
        self._invalidated.add(knowledge_key)

    def observe_contradiction(self, knowledge_key: str, better_sourced: str) -> None:
        self._contradictions[knowledge_key] = better_sourced

    def reads_since(self, knowledge_key: str, seconds: float) -> int:
        cutoff = self._now_ns() - int(seconds * 1e9)
        return sum(1 for at_ns in self._reads.get(knowledge_key, ()) if at_ns >= cutoff)

    def judge(self, knowledge_key: str, kind: str = "semantic-fact") -> StaleKnowledge:
        """One piece of knowledge, kept or pruned, with the reason recorded."""
        self.standing.judgements += 1
        confidence = self._confidences.get(knowledge_key, 1.0)
        reads = self.reads_since(knowledge_key, self._idle_seconds)
        established = self._established.get(knowledge_key, self._now_ns())
        age = (self._now_ns() - established) / 1e9
        contradicted_by = self._contradictions.get(knowledge_key)

        if knowledge_key in self._invalidated:
            # The one case where removal is immediate: a fact resting on a
            # source found wrong is wrong.
            return self._judgement(
                knowledge_key, kind, PRUNED, RESTS_ON_SOMETHING_INVALIDATED, confidence,
                reads, age, contradicted_by,
                "an ancestor of this was found to be wrong, so this is wrong. The only case "
                "where removal is immediate",
            )

        if reads > 0:
            # Removing it makes a live part fail for a reason nobody will
            # connect to this one.
            self.standing.kept_because_in_use += 1
            return self._judgement(
                knowledge_key, kind, KEPT, STILL_IN_USE, confidence, reads, age,
                contradicted_by,
                f"{reads} read(s) in the last {self._idle_seconds / 86400:.1f} day(s). A fact "
                f"in use is in use whatever its confidence says",
            )

        replacement = self._superseded_by.get(knowledge_key)
        if replacement is not None:
            if knowledge_key in self._settled:
                return self._judgement(
                    knowledge_key, kind, PRUNED, SUPERSEDED_AND_SETTLED, confidence, reads,
                    age, contradicted_by,
                    f"superseded by {replacement}, which has itself been confirmed",
                )
            self.standing.kept_because_supersession_unsettled += 1
            return self._judgement(
                knowledge_key, kind, KEPT, SUPERSESSION_UNSETTLED, confidence, reads, age,
                contradicted_by,
                f"superseded by {replacement}, but that supersession is still contested and "
                f"the contest may resolve the other way",
            )

        if confidence >= self._confidence_floor or contradicted_by is None:
            # Age alone is not evidence: a tick size established two years ago
            # and never rechecked is old and still correct.
            self.standing.kept_because_only_old += 1
            return self._judgement(
                knowledge_key, kind, KEPT, ONLY_OLD, confidence, reads, age, contradicted_by,
                f"confidence {confidence:.2f} and {age / 86400:.0f} day(s) old"
                + (
                    ", and nothing better sourced contradicts it. Old is not the same as "
                    "useless"
                    if contradicted_by is None
                    else f", above the {self._confidence_floor:.2f} floor"
                ),
            )

        return self._judgement(
            knowledge_key, kind, PRUNED, USELESS, confidence, reads, age, contradicted_by,
            f"nothing has read it in {self._idle_seconds / 86400:.1f} day(s), its confidence "
            f"has fallen to {confidence:.2f}, and {contradicted_by} contradicts it with a "
            f"better source. All three, because any one alone is not evidence of uselessness",
        )

    def prune(self, keys) -> tuple:
        judgements = tuple(self.judge(key) for key in keys)
        for judgement in judgements:
            if judgement.was_pruned:
                self.standing.pruned += 1
                self._record_removal(judgement)
            else:
                self.standing.kept += 1
            self.standing.by_reason[judgement.because] = (
                self.standing.by_reason.get(judgement.because, 0) + 1
            )
        return judgements

    @property
    def removals(self) -> tuple:
        """Every deletion, so the knowledge base's history stays reconstructable."""
        return tuple(self._removals)

    def _record_removal(self, judgement: StaleKnowledge) -> None:
        self._removals.append(
            {
                "knowledge_key": judgement.knowledge_key,
                "kind": judgement.kind,
                "because": judgement.because,
                "confidence_at_removal": judgement.confidence,
                "age_seconds": judgement.age_seconds,
                "removed_at_ns": judgement.judged_at_ns,
            }
        )
        self.standing.removals_recorded += 1

    def _judgement(
        self, knowledge_key, kind, state, because, confidence, reads, age, contradicted_by, reason
    ) -> StaleKnowledge:
        return StaleKnowledge(
            knowledge_key=knowledge_key,
            kind=kind,
            state=state,
            because=because,
            confidence=confidence,
            reads_recently=reads,
            age_seconds=age,
            contradicted_by=contradicted_by,
            reason=reason,
            judged_at_ns=self._now_ns(),
        )


def describe_pruning(pruner: KnowledgePruner) -> dict:
    return {
        "part_id": PART_ID,
        "judgements": pruner.standing.judgements,
        "pruned": pruner.standing.pruned,
        "kept": pruner.standing.kept,
        "kept_because_still_in_use": pruner.standing.kept_because_in_use,
        "kept_because_only_old": pruner.standing.kept_because_only_old,
        "kept_because_supersession_unsettled": (
            pruner.standing.kept_because_supersession_unsettled
        ),
        "by_reason": dict(sorted(pruner.standing.by_reason.items())),
        "removals_recorded": pruner.standing.removals_recorded,
        "age_alone_is_evidence": False,
    }


def run_knowledge_pruner(
    pruner: KnowledgePruner, control_socket, read_knowledge, publish_stale,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        keys = read_knowledge(pruner)
        publish_stale(pruner.prune(keys))

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

    A fact is established when stored, read when a skill that cites it is
    loaded into a decision or an instruction that uses it is scored, and
    its confidence follows the scheduler. A contradiction names the
    better-sourced side as the one with the higher confidence; an
    invalidated provenance marks the fact as resting on something wrong.
    Every key seen is judged once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    usefulness = Batch(read=context.bus.reader("skill-usefulness"))
    facts = Batch(read=context.bus.reader("semantic-fact"))
    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    provenance = Batch(read=context.bus.reader("fact-provenance"))
    contradictions = Batch(read=context.bus.reader("knowledge-contradiction"))
    confidences = Batch(read=context.bus.reader("fact-confidence"))
    publish_stale = context.bus.publisher_for("stale-knowledge")
    pruner = KnowledgePruner(
        confidence_floor=context.number("knowledge_confidence_floor"),
        idle_seconds_before_useless=context.number("knowledge_idle_seconds_before_useless"),
    )
    keys_seen: set[str] = set()
    last_judged = [float("-inf")]

    def read_knowledge(_pruner):
        for fact in facts.payloads():
            key = f"{fact.venue_id}:{fact.symbol}:{fact.key}"
            keys_seen.add(key)
            pruner.observe_established(key, int(fact.observed_at_ns))
            pruner.observe_confidence(key, float(fact.confidence.value))
            if fact.superseded_at_ns is not None:
                pruner.observe_supersession(key, key, True)
        for confidence in confidences.payloads():
            keys_seen.add(confidence.fact_key)
            pruner.observe_confidence(confidence.fact_key, float(confidence.confidence))
        for record in provenance.payloads():
            keys_seen.add(record.fact_key)
            if record.state == "an-ancestor-was-found-to-be-wrong":
                pruner.observe_invalidated_ancestor(record.fact_key)
        for contradiction in contradictions.payloads():
            better = contradiction.left if contradiction.left_confidence >= contradiction.right_confidence else contradiction.right
            worse = contradiction.right if better == contradiction.left else contradiction.left
            pruner.observe_contradiction(worse, better)
        for score in usefulness.payloads():
            if score.decisions_loaded_into > 0:
                pruner.observe_read(score.skill_id)
        for card in scorecards.payloads():
            pruner.observe_read(card.instruction_id)
        now = _time.monotonic()
        if now - last_judged[0] < context.health_interval_seconds:
            return ()
        last_judged[0] = now
        return tuple(sorted(keys_seen))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_stale(kept)

    return run_knowledge_pruner(
        pruner=pruner,
        control_socket=context.control_socket,
        read_knowledge=read_knowledge,
        publish_stale=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
