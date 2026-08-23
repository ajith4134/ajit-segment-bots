"""skill-composer: two skills that agree, made into one that is stronger than either.

The only part that creates a skill without a source. That makes it the one most
able to manufacture confidence, so what it may compose is narrow:

- **Only where two skills genuinely agree.** Independent sources reaching the
  same rule is the strongest evidence this system can have about a rule it did
  not derive, and a composed skill is how that evidence gets recorded as one
  thing rather than two.
- **Never where they conflict.** Composing a conflict produces a skill that
  contains a contradiction and looks like a resolution, which is worse than
  either input.
- **The composition inherits both provenances.** A composed rule that lost its
  parents' sources is uncorrectable, and composition is exactly where provenance
  is most often lost.
- **A composed skill is not more confident than its parents.** Two sources
  agreeing is stronger evidence than one; it is not proof, and a composer that
  raised confidence on composition would let the system agree with itself into
  certainty.

**A composed skill must still be backtested.** It is a new claim about what to
do, and inheriting its parents' backtests would be inheriting evidence for
different rules.

**Composition is recorded as a version**, so a composed skill that turns out
worse than its parents can be rolled back to them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import Skill
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-composer"

PART_DECLARATION = PartDeclaration(
    part_id="skill-composer",
    consumes=("skill", "knowledge-link"),
    produces=("skill", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

COMPOSED = "composed"
THEY_CONFLICT = "composing-a-conflict-produces-a-contradiction-that-looks-like-a-resolution"
THEY_DO_NOT_AGREE = "they-share-no-rule-so-there-is-nothing-to-compose"
ALREADY_COMPOSED = "these-two-have-already-been-composed"


@dataclass(frozen=True)
class Composition:
    """One composed skill, with what agreed and what each parent contributed alone."""

    skill: Skill | None
    state: str
    parents: tuple
    agreed_rules: tuple
    unique_to_left: tuple
    unique_to_right: tuple
    conflicts: tuple = ()
    inherited_sources: tuple = ()
    reason: str = ""
    composed_at_ns: int = 0

    @property
    def was_composed(self) -> bool:
        return self.state == COMPOSED


@dataclass
class ComposerStanding:
    attempts: int = 0
    composed: int = 0
    refused_conflicting: int = 0
    refused_no_agreement: int = 0
    refused_already_composed: int = 0
    rules_agreed: int = 0
    by_parent: dict = field(default_factory=dict)


class SkillComposer:
    """Composes only where skills agree, and inherits both provenances."""

    def __init__(self, minimum_agreed_rules: int, now_ns=time.time_ns) -> None:
        if minimum_agreed_rules < 1:
            raise ValueError(
                "composing two skills that agree on nothing produces a skill that is neither"
            )
        self._minimum_agreed = minimum_agreed_rules
        self._now_ns = now_ns
        self._conflicts: dict[tuple[str, str], tuple] = {}
        self._composed: set[tuple[str, str]] = set()
        self.standing = ComposerStanding()

    def observe_conflict(self, left_skill_id: str, right_skill_id: str, description: str) -> None:
        key = tuple(sorted((left_skill_id, right_skill_id)))
        existing = self._conflicts.get(key, ())
        self._conflicts[key] = existing + (description,)

    def agreed_rules(self, left: Skill, right: Skill) -> tuple:
        """Rules both skills state. Matched on their words, not their exact text."""
        agreed = []
        for rule in left.decision_rules:
            fingerprint = self._fingerprint(rule)
            for other in right.decision_rules:
                if self._fingerprint(other) == fingerprint:
                    agreed.append(rule)
                    break
        return tuple(agreed)

    def compose(self, left: Skill, right: Skill) -> Composition:
        """Two skills into one, only where they agree."""
        self.standing.attempts += 1
        key = tuple(sorted((left.skill_id, right.skill_id)))

        if key in self._composed:
            self.standing.refused_already_composed += 1
            return self._composition(
                None, ALREADY_COMPOSED, (left.skill_id, right.skill_id), (), (), (), (), (),
                "these two have already been composed; composing again would produce a "
                "duplicate that looks like a third source",
            )

        conflicts = self._conflicts.get(key, ())
        if conflicts:
            # A skill containing a contradiction and looking like a resolution is
            # worse than either input.
            self.standing.refused_conflicting += 1
            return self._composition(
                None, THEY_CONFLICT, (left.skill_id, right.skill_id), (), (), (), conflicts, (),
                f"{len(conflicts)} conflict(s) between them: "
                + "; ".join(conflicts)
                + ". Composing a conflict produces a skill that contains a contradiction and "
                "looks like a resolution",
            )

        agreed = self.agreed_rules(left, right)
        if len(agreed) < self._minimum_agreed:
            self.standing.refused_no_agreement += 1
            return self._composition(
                None, THEY_DO_NOT_AGREE, (left.skill_id, right.skill_id), agreed, (), (), (), (),
                f"they share {len(agreed)} rule(s) of the {self._minimum_agreed} needed, so "
                f"there is nothing to compose",
            )

        agreed_fingerprints = {self._fingerprint(rule) for rule in agreed}
        unique_left = tuple(
            rule for rule in left.decision_rules
            if self._fingerprint(rule) not in agreed_fingerprints
        )
        unique_right = tuple(
            rule for rule in right.decision_rules
            if self._fingerprint(rule) not in agreed_fingerprints
        )

        sections = dict(left.sections)
        for name, content in right.sections.items():
            sections[name] = (
                f"{sections[name]}\n{content}" if name in sections else content
            )
        # Recorded explicitly, so what agreed can be told from what one source
        # said alone.
        sections["agreed-by-both-sources"] = "\n".join(agreed)

        # Inherited: a composed rule that lost its parents' sources is
        # uncorrectable, and composition is where provenance is most often lost.
        inherited = tuple(
            reference for reference in (left.source_reference, right.source_reference)
            if reference
        )

        composed = Skill(
            skill_id=f"composed:{key[0]}+{key[1]}",
            title=f"{left.title} and {right.title}, where they agree",
            sections=sections,
            frameworks=tuple(sorted(set(left.frameworks) | set(right.frameworks))),
            decision_rules=tuple(agreed) + unique_left + unique_right,
            anti_patterns=tuple(sorted(set(left.anti_patterns) | set(right.anti_patterns))),
            source_reference="; ".join(inherited),
            version="1",
            distilled_at_ns=self._now_ns(),
        )

        self._composed.add(key)
        self.standing.composed += 1
        self.standing.rules_agreed += len(agreed)
        for parent in key:
            self.standing.by_parent[parent] = self.standing.by_parent.get(parent, 0) + 1

        return self._composition(
            composed, COMPOSED, (left.skill_id, right.skill_id), agreed, unique_left,
            unique_right, (), inherited,
            f"{len(agreed)} rule(s) stated by both, which is the strongest evidence this "
            f"system can have about a rule it did not derive itself; {len(unique_left)} and "
            f"{len(unique_right)} stated by one each. Both provenances inherited. It is no "
            f"more confident than its parents -- two sources agreeing is stronger evidence "
            f"than one and is not proof -- and it must be backtested itself, because "
            f"inheriting their backtests would be inheriting evidence for different rules",
        )

    def _fingerprint(self, rule: str) -> str:
        words = sorted(word.lower().strip(".,;:") for word in rule.split() if len(word) > 4)
        return " ".join(words)

    def _composition(
        self, skill, state, parents, agreed, unique_left, unique_right,
        conflicts, inherited, reason,
    ) -> Composition:
        return Composition(
            skill=skill,
            state=state,
            parents=parents,
            agreed_rules=agreed,
            unique_to_left=unique_left,
            unique_to_right=unique_right,
            conflicts=conflicts,
            inherited_sources=inherited,
            reason=reason,
            composed_at_ns=self._now_ns(),
        )


def describe_composition(composer: SkillComposer) -> dict:
    return {
        "part_id": PART_ID,
        "attempts": composer.standing.attempts,
        "composed": composer.standing.composed,
        "refused_conflicting": composer.standing.refused_conflicting,
        "refused_no_agreement": composer.standing.refused_no_agreement,
        "refused_already_composed": composer.standing.refused_already_composed,
        "rules_agreed": composer.standing.rules_agreed,
        "by_parent": dict(sorted(composer.standing.by_parent.items())),
        "raises_confidence_on_composition": False,
        "inherits_parent_backtests": False,
    }


def run_skill_composer(
    composer: SkillComposer, control_socket, read_skills, publish_skills,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        skills = []
        for left, right in read_skills(composer):
            composition = composer.compose(left, right)
            if composition.skill is not None:
                skills.append(composition.skill)
        publish_skills(tuple(skills))

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

    Every new skill is offered for composition with every skill already
    held that shares a rule with it; a knowledge link of the contradicting
    kind between two skills is a conflict the composer refuses across. A
    composed skill comes back on the same channel and is held like any
    other, which the composer's own already-composed record bounds.
    """
    from runtime.input_assembly import Batch

    skills = Batch(read=context.bus.reader("skill"))
    links = Batch(read=context.bus.reader("knowledge-link"))
    publish_skills = context.bus.publisher_for("skill")
    composer = SkillComposer(minimum_agreed_rules=int(context.number("skill_minimum_agreed_rules")))
    held: dict[str, object] = {}

    def read_skills(_composer):
        for link in links.payloads():
            if link.kind == "contradicts":
                composer.observe_conflict(str(link.left), str(link.right), link.reason)
        pairs = []
        for skill in skills.payloads():
            for other in held.values():
                if other.skill_id != skill.skill_id and composer.agreed_rules(other, skill):
                    pairs.append((other, skill))
            held[skill.skill_id] = skill
        return tuple(pairs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_skills(kept)

    return run_skill_composer(
        composer=composer,
        control_socket=context.control_socket,
        read_skills=read_skills,
        publish_skills=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
