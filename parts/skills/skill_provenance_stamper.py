"""skill-provenance-stamper: which source every rule in a skill came from.

A skill is a distillation, and a distillation loses the connection between what
it says and where each piece came from. That connection is what makes a skill
correctable: when a source turns out to be wrong, everything derived from it has
to be found, and without a stamp there is no way to find it.

- **Per rule, not per skill.** A skill composed from three sources has three
  provenances, and stamping the skill with one of them makes the other two
  uncorrectable.
- **The span, not just the reference.** "This came from that book" is not enough
  to check; the passage is.
- **A composed skill inherits every parent's provenance.** Composition is where
  provenance is most often lost, because the composed skill looks like a new
  thing.
- **A rule with no traceable source is stamped as untraceable.** Left unstamped
  it is indistinguishable from one nobody has got round to stamping, and only one
  of those needs fixing.

**Invalidating a source finds every rule that rests on it.** That is the whole
purpose: the stamp runs both ways, so a retracted paper takes its rules with it
rather than leaving them in the system looking like everyone else's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-provenance-stamper"

PART_DECLARATION = PartDeclaration(
    part_id="skill-provenance-stamper",
    consumes=("skill", "source-document"),
    produces=("skill-provenance", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

STAMPED = "stamped"
UNTRACEABLE = "no-span-of-any-source-holds-this"
INVALIDATED = "the-source-it-rests-on-was-retracted"


@dataclass(frozen=True)
class RuleProvenance:
    """One rule and the passage it came from."""

    skill_id: str
    rule: str
    state: str
    source_reference: str | None
    span: str | None
    inherited_from: tuple
    reason: str
    stamped_at_ns: int

    @property
    def can_be_checked(self) -> bool:
        return self.span is not None

    @property
    def is_invalidated(self) -> bool:
        return self.state == INVALIDATED


@dataclass
class StamperStanding:
    rules_stamped: int = 0
    untraceable: int = 0
    invalidated: int = 0
    composed_skills_stamped: int = 0
    sources_retracted: int = 0
    by_source: dict = field(default_factory=dict)


class SkillProvenanceStamper:
    """Stamps every rule with the passage it came from, and finds them when a source falls."""

    def __init__(self, minimum_matching_words: int, now_ns=time.time_ns) -> None:
        if minimum_matching_words < 2:
            raise ValueError(
                "one shared word matches anything, and a stamp that matches anything is not a "
                "stamp"
            )
        self._minimum_words = minimum_matching_words
        self._now_ns = now_ns
        self._documents: dict[str, str] = {}
        self._stamps: dict[tuple[str, str], RuleProvenance] = {}
        self._retracted: set[str] = set()
        self.standing = StamperStanding()

    def observe_document(self, source_reference: str, content: str) -> None:
        self._documents[source_reference] = content

    def span_for(self, rule: str) -> tuple:
        """The passage of a source that holds this rule, or nothing."""
        words = [word.lower() for word in rule.split() if len(word) > 4]
        if len(words) < self._minimum_words:
            return None, None
        for reference, content in sorted(self._documents.items()):
            lowered = content.lower()
            found = [word for word in words if word in lowered]
            if len(found) < self._minimum_words:
                continue
            positions = [lowered.find(word) for word in found if lowered.find(word) >= 0]
            if not positions:
                continue
            start = max(0, min(positions) - 40)
            return reference, content[start : start + 200]
        return None, None

    def stamp(self, skill_id: str, rule: str, inherited_from=()) -> RuleProvenance:
        """One rule, stamped with its passage. Per rule, never per skill."""
        self.standing.rules_stamped += 1
        inherited_from = tuple(inherited_from)
        if inherited_from:
            # Composition is where provenance is most often lost, because the
            # composed skill looks like a new thing.
            self.standing.composed_skills_stamped += 1

        reference, span = self.span_for(rule)

        if reference is None:
            # Stamped as untraceable rather than left unstamped: unstamped is
            # indistinguishable from not-yet-stamped, and only one needs fixing.
            self.standing.untraceable += 1
            provenance = self._provenance(
                skill_id, rule, UNTRACEABLE, None, None, inherited_from,
                "no span of any held source contains this rule. Stamped as untraceable rather "
                "than left unstamped, because unstamped is indistinguishable from not yet "
                "stamped and only one of those needs fixing",
            )
        elif reference in self._retracted:
            self.standing.invalidated += 1
            provenance = self._provenance(
                skill_id, rule, INVALIDATED, reference, span, inherited_from,
                f"{reference} has been retracted, so this rule goes with it",
            )
        else:
            self.standing.by_source[reference] = self.standing.by_source.get(reference, 0) + 1
            provenance = self._provenance(
                skill_id, rule, STAMPED, reference, span, inherited_from,
                f"from {reference}: \"{span[:80].strip()}...\". The span rather than just the "
                f"reference, because 'this came from that book' is not enough to check"
                + (
                    f". Inherited from {', '.join(inherited_from)} as well"
                    if inherited_from
                    else ""
                ),
            )

        self._stamps[(skill_id, rule)] = provenance
        return provenance

    def stamp_skill(self, skill, inherited_from=()) -> tuple:
        """Every rule and anti-pattern in one skill, each with its own source."""
        return tuple(
            self.stamp(skill.skill_id, rule, inherited_from)
            for rule in tuple(skill.decision_rules) + tuple(skill.anti_patterns)
        )

    def retract_source(self, source_reference: str) -> tuple:
        """A retracted source takes its rules with it.

        The whole purpose: the stamp runs both ways, so a retracted paper's rules
        do not stay in the system looking like everyone else's.
        """
        self._retracted.add(source_reference)
        self.standing.sources_retracted += 1
        affected = []
        for key, provenance in list(self._stamps.items()):
            if provenance.source_reference != source_reference:
                continue
            self.standing.invalidated += 1
            self._stamps[key] = self._provenance(
                provenance.skill_id, provenance.rule, INVALIDATED,
                provenance.source_reference, provenance.span, provenance.inherited_from,
                f"{source_reference} has been retracted, so this rule goes with it rather than "
                f"staying in the system looking like everyone else's",
            )
            affected.append(self._stamps[key])
        return tuple(affected)

    def rules_from(self, source_reference: str) -> tuple:
        return tuple(
            provenance
            for provenance in sorted(self._stamps.values(), key=lambda entry: entry.rule)
            if provenance.source_reference == source_reference
        )

    def provenance_of(self, skill_id: str, rule: str) -> RuleProvenance | None:
        return self._stamps.get((skill_id, rule))

    def _provenance(
        self, skill_id, rule, state, reference, span, inherited_from, reason
    ) -> RuleProvenance:
        return RuleProvenance(
            skill_id=skill_id,
            rule=rule,
            state=state,
            source_reference=reference,
            span=span,
            inherited_from=inherited_from,
            reason=reason,
            stamped_at_ns=self._now_ns(),
        )


def describe_stamping(stamper: SkillProvenanceStamper) -> dict:
    return {
        "part_id": PART_ID,
        "rules_stamped": stamper.standing.rules_stamped,
        "untraceable": stamper.standing.untraceable,
        "invalidated": stamper.standing.invalidated,
        "composed_skills_stamped": stamper.standing.composed_skills_stamped,
        "sources_retracted": stamper.standing.sources_retracted,
        "by_source": dict(sorted(stamper.standing.by_source.items())),
        "stamps_per_skill_rather_than_per_rule": False,
    }


def run_skill_provenance_stamper(
    stamper: SkillProvenanceStamper, control_socket, read_skills, publish_provenance,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        provenance = []
        for skill, inherited in read_skills(stamper):
            provenance.extend(stamper.stamp_skill(skill, inherited))
        publish_provenance(tuple(provenance))

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    skills = Batch(read=context.bus.reader("skill"))
    documents = Batch(read=context.bus.reader("source-document"))
    publish_provenance = context.bus.publisher_for("skill-provenance")
    stamper = SkillProvenanceStamper(minimum_matching_words=int(context.number("skill_provenance_minimum_matching_words")))

    def read_skills(_stamper):
        for document in documents.payloads():
            stamper.observe_document(document.source_reference, str(document.content))
        return tuple((skill, ()) for skill in skills.payloads())

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_provenance(kept)

    return run_skill_provenance_stamper(
        stamper=stamper,
        control_socket=context.control_socket,
        read_skills=read_skills,
        publish_provenance=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
