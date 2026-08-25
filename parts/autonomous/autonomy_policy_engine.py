"""autonomy-policy-engine: is this particular action inside the envelope.

The envelope is a standing state; this is the per-action question. They are separate
because an envelope says what kind of thing is permitted and an action is a specific
thing with a size, a subject and a moment -- and folding the two together means every
caller re-derives the policy from raw state, which is how two callers come to disagree
about what is allowed.

Three rules the engine enforces that an envelope alone cannot:

- **Competence is per subject, not global.** A system may be entirely competent at
  BTCUSDT futures and have no history at all in a three-day-old listing. One global
  competence number permits the second because of the first, which is exactly
  backwards.
- **Size is checked against the envelope's ceiling *and* against what this subject
  has earned.** The ceiling is a hard limit; the earned size is usually much smaller,
  and using the ceiling as the default is how a new instrument gets a full-size
  position on its first trade.
- **Unknown subjects are refused, not defaulted.** An action on something with no
  competence record is not a small-competence action; it is an unmeasured one, and
  permitting it at any size is a guess about a thing nobody has measured.

Every decision states which rule decided it. A policy engine that returns only
allowed/denied trains its callers to work around it, because a refusal nobody can
explain looks like a bug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import (
    ACT_WITHIN_LIMITS, MODIFY_ITSELF, OBSERVE_ONLY, PROPOSE_ONLY, PolicyDecision,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "autonomy-policy-engine"

PART_DECLARATION = PartDeclaration(
    part_id="autonomy-policy-engine",
    consumes=("autonomy-envelope", "trade-intent", "proposed-part", "competence-map"),
    produces=("policy-decision", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ALLOWED = "allowed"
NO_ENVELOPE = "no-envelope-has-been-issued"
LEVEL_FORBIDS_IT = "the-envelope-level-does-not-permit-this-kind-of-action"
SUBJECT_IS_UNMEASURED = "nothing-is-known-about-this-subject"
ABOVE_THE_ENVELOPE_CEILING = "larger-than-the-envelope-permits"
ABOVE_WHAT_THIS_SUBJECT_HAS_EARNED = "larger-than-this-subject-has-earned"
UNKNOWN_ACTION = "not-an-action-this-engine-knows"

# The actions this engine can rule on, and the minimum level each needs.
OBSERVE = "observe"
PROPOSE_A_PART = "propose-a-part"
OPEN_A_POSITION = "open-a-position"
CLOSE_A_POSITION = "close-a-position"
CHANGE_A_SETTING = "change-a-setting"
ADMIT_A_PART = "admit-a-part"

LEVEL_REQUIRED = {
    OBSERVE: OBSERVE_ONLY,
    PROPOSE_A_PART: PROPOSE_ONLY,
    CLOSE_A_POSITION: PROPOSE_ONLY,
    OPEN_A_POSITION: ACT_WITHIN_LIMITS,
    CHANGE_A_SETTING: ACT_WITHIN_LIMITS,
    ADMIT_A_PART: MODIFY_ITSELF,
}

LEVEL_ORDER = (OBSERVE_ONLY, PROPOSE_ONLY, ACT_WITHIN_LIMITS, MODIFY_ITSELF)


@dataclass(frozen=True)
class PolicyOutcome:
    subject: str
    action: str
    state: str
    decision: PolicyDecision
    earned_ceiling: float | None
    deciding_rule: str
    reason: str
    decided_at_ns: int

    @property
    def is_allowed(self) -> bool:
        return self.state == ALLOWED


@dataclass
class PolicyStanding:
    decisions: int = 0
    allowed: int = 0
    refused_level: int = 0
    refused_unmeasured_subject: int = 0
    refused_envelope_ceiling: int = 0
    refused_earned_ceiling: int = 0
    refused_unknown_action: int = 0
    subjects_with_competence: int = 0


class AutonomyPolicyEngine:
    """Rules on one action at a time, per subject, and says which rule decided."""

    def __init__(
        self,
        earned_size_per_competence: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if earned_size_per_competence <= 0:
            raise ValueError(
                "earned size is derived from measured competence; at zero nothing is "
                "ever permitted and the envelope ceiling becomes the only limit"
            )
        if minimum_observations < 1:
            raise ValueError(
                "an unmeasured subject is not a low-competence subject, and permitting "
                "it at any size is a guess about a thing nobody has measured"
            )
        self._earned_per_competence = earned_size_per_competence
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        self._envelope = None
        self._competence: dict[str, tuple] = {}
        self.standing = PolicyStanding()

    def observe_envelope(self, envelope) -> None:
        self._envelope = envelope

    def observe_competence(self, subject: str, competence: float, observations: int) -> None:
        """Per subject: competence at BTCUSDT says nothing about a new listing."""
        if subject not in self._competence:
            self.standing.subjects_with_competence += 1
        self._competence[subject] = (competence, observations)

    def earned_ceiling(self, subject: str) -> float | None:
        entry = self._competence.get(subject)
        if entry is None:
            return None
        competence, observations = entry
        if observations < self._minimum_observations:
            return None
        return competence * self._earned_per_competence

    def decide(self, subject: str, action: str, size: float = 0.0) -> PolicyOutcome:
        self.standing.decisions += 1

        if action not in LEVEL_REQUIRED:
            self.standing.refused_unknown_action += 1
            return self._outcome(
                subject, action, UNKNOWN_ACTION, None, UNKNOWN_ACTION,
                f"{action!r} is not an action this engine knows. An unknown action is "
                f"refused rather than allowed by omission",
            )

        if self._envelope is None:
            return self._outcome(
                subject, action, NO_ENVELOPE, None, NO_ENVELOPE,
                "no envelope has been issued, so nothing is permitted",
            )

        needed = LEVEL_REQUIRED[action]
        if LEVEL_ORDER.index(self._envelope.level) < LEVEL_ORDER.index(needed):
            self.standing.refused_level += 1
            return self._outcome(
                subject, action, LEVEL_FORBIDS_IT, None, LEVEL_FORBIDS_IT,
                f"{action} needs {needed} and the envelope is at {self._envelope.level}",
            )

        if action not in (OPEN_A_POSITION,):
            self.standing.allowed += 1
            return self._outcome(
                subject, action, ALLOWED, None, "level",
                f"{action} is permitted at {self._envelope.level}",
            )

        earned = self.earned_ceiling(subject)
        if earned is None:
            self.standing.refused_unmeasured_subject += 1
            return self._outcome(
                subject, action, SUBJECT_IS_UNMEASURED, None, SUBJECT_IS_UNMEASURED,
                f"nothing is measured about {subject}. That is not a low-competence "
                f"subject, it is an unmeasured one, and permitting it at any size is a "
                f"guess",
            )

        if size > self._envelope.maximum_notional:
            self.standing.refused_envelope_ceiling += 1
            return self._outcome(
                subject, action, ABOVE_THE_ENVELOPE_CEILING, earned,
                ABOVE_THE_ENVELOPE_CEILING,
                f"{size:,.2f} exceeds the envelope's {self._envelope.maximum_notional:,.2f} "
                f"ceiling",
            )

        if size > earned:
            self.standing.refused_earned_ceiling += 1
            return self._outcome(
                subject, action, ABOVE_WHAT_THIS_SUBJECT_HAS_EARNED, earned,
                ABOVE_WHAT_THIS_SUBJECT_HAS_EARNED,
                f"{size:,.2f} exceeds the {earned:,.2f} this subject has earned. The "
                f"envelope ceiling is a hard limit, not a default -- using it as one is "
                f"how a new instrument gets a full-size position on its first trade",
            )

        self.standing.allowed += 1
        return self._outcome(
            subject, action, ALLOWED, earned, "earned-size",
            f"{size:,.2f} is inside both the envelope's "
            f"{self._envelope.maximum_notional:,.2f} and the {earned:,.2f} this subject "
            f"has earned",
        )

    def _outcome(
        self, subject, action, state, earned, deciding_rule, reason,
    ) -> PolicyOutcome:
        return PolicyOutcome(
            subject=subject, action=action, state=state,
            decision=PolicyDecision(
                subject=subject, action=action, is_allowed=state == ALLOWED,
                envelope_level=self._envelope.level if self._envelope else "none",
                reason=reason, decided_at_ns=self._now_ns(),
            ),
            earned_ceiling=earned, deciding_rule=deciding_rule, reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_policy(engine: AutonomyPolicyEngine) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": engine.standing.decisions,
        "allowed": engine.standing.allowed,
        "refused_by_level": engine.standing.refused_level,
        "refused_unmeasured_subject": engine.standing.refused_unmeasured_subject,
        "refused_envelope_ceiling": engine.standing.refused_envelope_ceiling,
        "refused_earned_ceiling": engine.standing.refused_earned_ceiling,
        "refused_unknown_action": engine.standing.refused_unknown_action,
        "subjects_with_competence": engine.standing.subjects_with_competence,
        "known_actions": sorted(LEVEL_REQUIRED),
        "uses_one_global_competence": False,
        "returns_a_bare_yes_or_no": False,
    }


def run_autonomy_policy_engine(
    engine: AutonomyPolicyEngine, control_socket, read_requests, publish_decisions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for subject, action, size in read_requests(engine):
            publish_decisions(engine.decide(subject, action, size).decision)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_policy(engine),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A trade intent carries no notional by design -- sizing belongs to parts
    that know the account -- so an intent is ruled on at size zero: the level
    and competence checks bind here, and the size checks bind where sizes are
    decided, against the envelope this engine's decisions carry. Competence
    entries whose value is unmeasured are passed over rather than counted as
    zero: an unmeasured subject is refused by the engine's own rule, not by an
    invented number.
    """
    from runtime.input_assembly import Batch, LatestValue
    from runtime.trade_intent import ADD_TO, OPEN, STAND_ASIDE

    envelopes = LatestValue(read=context.bus.reader("autonomy-envelope"))
    intents = Batch(read=context.bus.reader("trade-intent"))
    proposals = Batch(read=context.bus.reader("proposed-part"))
    competences = Batch(read=context.bus.reader("competence-map"))
    publish_decisions = context.bus.publisher_for("policy-decision")

    engine = AutonomyPolicyEngine(
        earned_size_per_competence=context.number("policy_earned_size_per_competence"),
        minimum_observations=int(context.number("policy_minimum_observations")),
    )

    def read_requests(_engine):
        envelope = envelopes.value()
        if envelope is not None:
            engine.observe_envelope(envelope)
        for mapped in competences.payloads():
            # A competence-map is the whole map; its entries are the per-symbol
            # records. Read as one entry until 2026-08-25, it crashed this part on
            # the first map that arrived -- the same defect, on the same wire, that
            # opinion-arbiter hit the same hour.
            for entry in mapped.entries:
                if entry.competence is not None:
                    engine.observe_competence(entry.symbol, entry.competence, entry.trades)
        requests = []
        for intent in intents.payloads():
            if intent.action == STAND_ASIDE:
                continue
            action = (
                OPEN_A_POSITION
                if intent.action in (OPEN, ADD_TO)
                else CLOSE_A_POSITION
            )
            requests.append((intent.symbol, action, 0.0))
        for proposal in proposals.payloads():
            requests.append((proposal.part_id, ADMIT_A_PART, 0.0))
        return tuple(requests)

    return run_autonomy_policy_engine(
        engine=engine,
        control_socket=context.control_socket,
        read_requests=read_requests,
        publish_decisions=lambda decision: publish_decisions((decision,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
