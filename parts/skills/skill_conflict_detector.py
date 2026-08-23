"""skill-conflict-detector: two skills that cannot both be followed.

Sources disagree. A book saying to cut losses at 2% and one saying to give a
thesis room to breathe are both defensible and cannot both be applied to the same
trade -- and a system holding both will follow whichever was loaded, which means
its behaviour is decided by a keyword match.

So conflicts are found and named, and the naming is what makes them usable:

- **A direct contradiction**: two rules whose conditions overlap and whose
  actions oppose. The urgent case, because both fire.
- **A threshold disagreement**: the same rule with different numbers. Usually the
  more specific source is right about its own market, and the disagreement is
  really about scope.
- **A scope disagreement**: one source says always, another says only in a
  regime. The unconditional one is almost always the unexamined one.

**A conflict is not resolved by recency or by source quality.** A newer book is
not more right, and a famous author is not more right about this market. The
resolution is a backtest, and until there is one both skills are held and neither
is available.

**Agreement is recorded too.** Two independent sources offering the same rule is
the strongest evidence this system can get about a rule it did not derive
itself, and a detector that only reported conflicts would throw that away.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-conflict-detector"

PART_DECLARATION = PartDeclaration(
    part_id="skill-conflict-detector",
    consumes=("skill",),
    produces=("skill-conflict", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DIRECT_CONTRADICTION = "their-conditions-overlap-and-their-actions-oppose"
THRESHOLD_DISAGREEMENT = "the-same-rule-with-different-numbers"
SCOPE_DISAGREEMENT = "one-says-always-and-the-other-says-only-in-a-regime"
AGREEMENT = "two-independent-sources-offering-the-same-rule"


@dataclass(frozen=True)
class SkillRule:
    """One rule from one skill, in the shape a conflict can be found in."""

    skill_id: str
    section: str
    measurement: str
    comparison: str
    threshold: float
    action: str
    regime: str | None
    text: str


@dataclass(frozen=True)
class SkillConflict:
    """Two rules that cannot both be followed, or two that agree."""

    kind: str
    left: SkillRule
    right: SkillRule
    is_agreement: bool
    resolvable_by: str
    reason: str
    detected_at_ns: int

    @property
    def blocks_both(self) -> bool:
        """Until a backtest resolves it, both are held and neither is available."""
        return self.kind == DIRECT_CONTRADICTION


@dataclass
class DetectorStanding:
    rules_observed: int = 0
    checks: int = 0
    conflicts_found: int = 0
    agreements_found: int = 0
    by_kind: dict = field(default_factory=dict)
    skills_blocked: int = 0


class SkillConflictDetector:
    """Finds rules that cannot both be followed, and rules that independently agree."""

    def __init__(self, threshold_tolerance: float, now_ns=time.time_ns) -> None:
        if threshold_tolerance < 0:
            raise ValueError("the tolerance is a distance and cannot be negative")
        self._tolerance = threshold_tolerance
        self._now_ns = now_ns
        self._rules: list = []
        self.standing = DetectorStanding()

    def observe_rule(self, rule: SkillRule) -> None:
        self._rules.append(rule)
        self.standing.rules_observed = len(self._rules)

    def check(self) -> tuple:
        """Every conflict and every agreement between the rules held."""
        self.standing.checks += 1
        found = []

        for index, left in enumerate(self._rules):
            for right in self._rules[index + 1 :]:
                if left.skill_id == right.skill_id:
                    continue
                if left.measurement != right.measurement:
                    continue

                conflict = self._compare(left, right)
                if conflict is None:
                    continue
                found.append(conflict)
                self.standing.by_kind[conflict.kind] = (
                    self.standing.by_kind.get(conflict.kind, 0) + 1
                )
                if conflict.is_agreement:
                    self.standing.agreements_found += 1
                else:
                    self.standing.conflicts_found += 1
                if conflict.blocks_both:
                    self.standing.skills_blocked += 2

        return tuple(found)

    def _compare(self, left: SkillRule, right: SkillRule) -> SkillConflict | None:
        same_direction = left.comparison == right.comparison
        close_threshold = abs(left.threshold - right.threshold) <= self._tolerance
        same_action = left.action == right.action

        if same_direction and close_threshold and same_action:
            # The strongest evidence this system can get about a rule it did not
            # derive itself, and a detector reporting only conflicts throws it away.
            return self._conflict(
                AGREEMENT, left, right, True, "nothing to resolve",
                f"{left.skill_id} and {right.skill_id} independently offer the same rule: "
                f"{left.text}. Two independent sources agreeing is the strongest evidence "
                f"this system can get about a rule it did not derive itself",
            )

        if self._conditions_overlap(left, right) and not same_action:
            return self._conflict(
                DIRECT_CONTRADICTION, left, right, False, "a backtest",
                f"{left.skill_id} says {left.action} and {right.skill_id} says {right.action} "
                f"on overlapping conditions. Both fire, so which one the system follows is "
                f"decided by whichever was loaded -- a keyword match choosing the behaviour. "
                f"Neither is available until a backtest resolves it: a newer book is not more "
                f"right, and a famous author is not more right about this market",
            )

        if same_direction and same_action and not close_threshold:
            return self._conflict(
                THRESHOLD_DISAGREEMENT, left, right, False, "a backtest, or scoping each to "
                "its own market",
                f"the same rule with different numbers: {left.threshold:.4g} against "
                f"{right.threshold:.4g}. Usually the more specific source is right about its "
                f"own market, so this disagreement is really about scope",
            )

        if same_action and (left.regime is None) != (right.regime is None):
            unconditional = left if left.regime is None else right
            return self._conflict(
                SCOPE_DISAGREEMENT, left, right, False, "scoping the unconditional one",
                f"{unconditional.skill_id} states this unconditionally while the other scopes "
                f"it to a regime. The unconditional one is almost always the unexamined one",
            )

        return None

    def _conditions_overlap(self, left: SkillRule, right: SkillRule) -> bool:
        """Whether both rules can fire on the same observation."""
        if left.regime is not None and right.regime is not None and left.regime != right.regime:
            return False
        if left.comparison == right.comparison:
            return True
        above = left if left.comparison == "above" else right
        below = right if left.comparison == "above" else left
        return above.threshold < below.threshold

    def _conflict(self, kind, left, right, is_agreement, resolvable_by, reason) -> SkillConflict:
        return SkillConflict(
            kind=kind,
            left=left,
            right=right,
            is_agreement=is_agreement,
            resolvable_by=resolvable_by,
            reason=reason,
            detected_at_ns=self._now_ns(),
        )


def describe_conflicts(detector: SkillConflictDetector) -> dict:
    return {
        "part_id": PART_ID,
        "rules_observed": detector.standing.rules_observed,
        "checks": detector.standing.checks,
        "conflicts_found": detector.standing.conflicts_found,
        "agreements_found": detector.standing.agreements_found,
        "by_kind": dict(sorted(detector.standing.by_kind.items())),
        "skills_blocked_pending_a_backtest": detector.standing.skills_blocked,
        "resolves_by_recency_or_source_quality": False,
    }


def run_skill_conflict_detector(
    detector: SkillConflictDetector, control_socket, read_skills, publish_conflicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_skills(detector)
        publish_conflicts(detector.check())

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

    A skill's decision rules are read for the shape the detector compares:
    a measurement, above or below, a number, and an action -- the words
    before the comparison, the comparison, the first number after it, and
    the words after that. A rule that does not state a comparison and a
    number is not one the detector can check, and is passed over.
    """
    import re

    from runtime.input_assembly import Batch

    skills = Batch(read=context.bus.reader("skill"))
    publish_conflicts = context.bus.publisher_for("skill-conflict")
    detector = SkillConflictDetector(threshold_tolerance=context.number("contradiction_value_tolerance"))
    rule_shape = re.compile(r"^(?P<measurement>.+?)\s+(?P<comparison>above|below)\s+(?P<threshold>-?\d+(?:\.\d+)?)\s*%?\s*(?P<action>.*)$", re.IGNORECASE)
    seen: set[tuple[str, str]] = set()

    def rule_from(skill, section: str, text: str) -> SkillRule | None:
        match = rule_shape.match(text.strip())
        if match is None:
            return None
        regime = None
        action = match.group("action").strip() or "act"
        if " in the " in action and action.endswith(" regime"):
            action, _, regime = action.partition(" in the ")
            regime = regime[: -len(" regime")]
        return SkillRule(
            skill_id=skill.skill_id, section=section, measurement=match.group("measurement").strip().lower(),
            comparison=match.group("comparison").lower(), threshold=float(match.group("threshold")),
            action=action.strip().lower(), regime=regime, text=text,
        )

    def read_skills(_detector) -> None:
        for skill in skills.payloads():
            for text in skill.decision_rules:
                key = (skill.skill_id, str(text))
                if key in seen:
                    continue
                seen.add(key)
                section = next((name for name, content in skill.sections.items() if str(text) in str(content)), "rules")
                rule = rule_from(skill, section, str(text))
                if rule is not None:
                    detector.observe_rule(rule)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_conflicts(kept)

    return run_skill_conflict_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_skills=read_skills,
        publish_conflicts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
