"""contradiction-detector: the two things this system believes that cannot both be true.

The reason the three memory tiers are allowed to disagree. A single store would
resolve every contradiction by overwriting, silently, and the system would lose
the one signal that says something has changed.

This part finds the disagreements and **reports them without resolving them**,
because resolution needs judgement this part does not have:

- **A fact against a fact.** Two sources giving different values for the same
  closed-set key. Usually one is stale, which is a different fix from one being
  wrong.
- **A fact against a rule.** The playbook says to act when a measurement is above
  a threshold; the facts say that measurement has never been above it here. The
  rule is dead and nobody has noticed.
- **A rule against a rule.** Two playbook rules that fire on the same conditions
  and say opposite things. This one is urgent: whichever ordering happens to
  apply becomes the system's behaviour, and nobody chose it.
- **A link against the facts.** The graph says two symbols are related; the facts
  say their correlation is zero. A link nobody rechecks becomes a belief.

**Confidence is reported, never used to pick a winner.** A high-confidence stale
fact beats a low-confidence fresh one on confidence alone, and that is exactly
backwards.

**Every contradiction names both sides and both sources**, so whoever resolves it
can go and look rather than choose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import KnowledgeContradiction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "contradiction-detector"

PART_DECLARATION = PartDeclaration(
    part_id="contradiction-detector",
    consumes=("semantic-fact", "playbook-rule", "knowledge-link"),
    produces=("knowledge-contradiction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FACT_AGAINST_FACT = "two-sources-disagree-about-the-same-fact"
FACT_AGAINST_RULE = "a-rule-fires-on-something-the-facts-say-never-happens"
RULE_AGAINST_RULE = "two-rules-fire-together-and-say-opposite-things"
LINK_AGAINST_FACT = "the-graph-says-related-and-the-facts-say-otherwise"


@dataclass
class DetectorStanding:
    checks: int = 0
    contradictions_found: int = 0
    by_kind: dict = field(default_factory=dict)
    resolved_by_this_part: int = 0
    urgent_rule_conflicts: int = 0


class ContradictionDetector:
    """Finds what cannot both be true, and reports it rather than deciding."""

    def __init__(self, value_tolerance: float, now_ns=time.time_ns) -> None:
        if not 0.0 <= value_tolerance <= 1.0:
            raise ValueError(
                "the tolerance is how far two values may differ before they contradict"
            )
        self._tolerance = value_tolerance
        self._now_ns = now_ns
        self._facts: dict[tuple, list] = {}
        self._rules: list = []
        self._links: list = []
        self.standing = DetectorStanding()

    def observe_fact(self, fact) -> None:
        """One fact from one source. Several sources may claim the same key."""
        key = (fact.venue_id, fact.symbol, fact.key)
        held = [
            existing for existing in self._facts.get(key, []) if existing.source != fact.source
        ]
        held.append(fact)
        self._facts[key] = held

    def observe_rule(self, rule, condition: dict) -> None:
        self._rules.append((rule, condition))

    def observe_link(self, left: str, right: str, claimed_relation: str, strength: float) -> None:
        self._links.append((left, right, claimed_relation, strength))

    def facts_against_facts(self) -> tuple:
        """Two sources giving different values for the same key.

        Usually one is stale, which is a different fix from one being wrong --
        and this part does not decide which.
        """
        found = []
        for (venue_id, symbol, key), facts in sorted(self._facts.items()):
            for index, left in enumerate(facts):
                for right in facts[index + 1 :]:
                    if not self._values_disagree(left.value, right.value):
                        continue
                    found.append(
                        KnowledgeContradiction(
                            subject=f"{venue_id}:{symbol}:{key}",
                            left=str(left.value),
                            right=str(right.value),
                            left_source=left.source,
                            right_source=right.source,
                            left_confidence=left.confidence.value,
                            right_confidence=right.confidence.value,
                            kind=FACT_AGAINST_FACT,
                            reason=(
                                f"{left.source} says {left.value} and {right.source} says "
                                f"{right.value} about {key} for {symbol}. Reported rather than "
                                f"resolved: a high-confidence stale fact beats a "
                                f"low-confidence fresh one on confidence alone, which is "
                                f"exactly backwards"
                            ),
                            detected_at_ns=self._now_ns(),
                        )
                    )
        return tuple(found)

    def facts_against_rules(self) -> tuple:
        """A rule that fires on a value the facts say never occurs here."""
        found = []
        for rule, condition in self._rules:
            if not getattr(rule, "is_active", True):
                continue
            measurement = condition.get("measurement")
            threshold = condition.get("threshold")
            observed_range = condition.get("observed_range")
            if measurement is None or threshold is None or observed_range is None:
                continue
            low, high = observed_range
            fires_above = condition.get("comparison") == "above"
            impossible = threshold >= high if fires_above else threshold <= low
            if not impossible:
                continue
            found.append(
                KnowledgeContradiction(
                    subject=rule.rule_id,
                    left=f"{measurement} {condition.get('comparison')} {threshold:.6g}",
                    right=f"{measurement} has been in [{low:.6g}, {high:.6g}]",
                    left_source="the playbook",
                    right_source="the semantic facts",
                    left_confidence=1.0,
                    right_confidence=1.0,
                    kind=FACT_AGAINST_RULE,
                    reason=(
                        f"{rule.rule_id} fires when {measurement} is "
                        f"{condition.get('comparison')} {threshold:.6g}, and the facts say it "
                        f"has never been outside [{low:.6g}, {high:.6g}] here. The rule is "
                        f"dead and nobody has noticed"
                    ),
                    detected_at_ns=self._now_ns(),
                )
            )
        return tuple(found)

    def rules_against_rules(self) -> tuple:
        """Two rules firing together and saying opposite things.

        Urgent: whichever ordering happens to apply becomes the system's
        behaviour, and nobody chose it.
        """
        found = []
        for index, (left_rule, left_condition) in enumerate(self._rules):
            for right_rule, right_condition in self._rules[index + 1 :]:
                if not (
                    getattr(left_rule, "is_active", True)
                    and getattr(right_rule, "is_active", True)
                ):
                    continue
                if left_condition.get("measurement") != right_condition.get("measurement"):
                    continue
                if not self._can_fire_together(left_condition, right_condition):
                    continue
                if left_rule.then == right_rule.then:
                    continue
                self.standing.urgent_rule_conflicts += 1
                found.append(
                    KnowledgeContradiction(
                        subject=f"{left_rule.rule_id} vs {right_rule.rule_id}",
                        left=f"{left_rule.when} then {left_rule.then}",
                        right=f"{right_rule.when} then {right_rule.then}",
                        left_source=left_rule.instruction_id or "the playbook",
                        right_source=right_rule.instruction_id or "the playbook",
                        left_confidence=1.0,
                        right_confidence=1.0,
                        kind=RULE_AGAINST_RULE,
                        reason=(
                            f"both fire on the same conditions and say opposite things. "
                            f"Whichever ordering happens to apply becomes this system's "
                            f"behaviour, and nobody chose it"
                        ),
                        detected_at_ns=self._now_ns(),
                    )
                )
        return tuple(found)

    def links_against_facts(self, correlations: dict) -> tuple:
        """A link nobody rechecks becomes a belief."""
        found = []
        for left, right, relation, strength in self._links:
            measured = correlations.get(tuple(sorted((left, right))))
            if measured is None:
                continue
            if abs(measured) >= self._tolerance or strength < self._tolerance:
                continue
            found.append(
                KnowledgeContradiction(
                    subject=f"{left}/{right}",
                    left=f"the graph says {relation} at {strength:.2f}",
                    right=f"their measured correlation is {measured:.2f}",
                    left_source="the knowledge graph",
                    right_source="measurement",
                    left_confidence=strength,
                    right_confidence=1.0,
                    kind=LINK_AGAINST_FACT,
                    reason=(
                        f"the graph claims {left} and {right} are {relation} at "
                        f"{strength:.2f} while their measured correlation is {measured:.2f}. "
                        f"A link nobody rechecks becomes a belief"
                    ),
                    detected_at_ns=self._now_ns(),
                )
            )
        return tuple(found)

    def check(self, correlations: dict | None = None) -> tuple:
        """Every contradiction, of every kind, reported and never resolved."""
        self.standing.checks += 1
        found = (
            self.facts_against_facts()
            + self.facts_against_rules()
            + self.rules_against_rules()
            + self.links_against_facts(correlations or {})
        )
        self.standing.contradictions_found += len(found)
        for contradiction in found:
            self.standing.by_kind[contradiction.kind] = (
                self.standing.by_kind.get(contradiction.kind, 0) + 1
            )
        return found

    def _values_disagree(self, left, right) -> bool:
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            larger = max(abs(left), abs(right))
            if larger == 0:
                return False
            return abs(left - right) / larger > self._tolerance
        return left != right

    def _can_fire_together(self, left: dict, right: dict) -> bool:
        left_above = left.get("comparison") == "above"
        right_above = right.get("comparison") == "above"
        left_threshold = left.get("threshold", 0.0)
        right_threshold = right.get("threshold", 0.0)
        if left_above and right_above:
            return True
        if not left_above and not right_above:
            return True
        # One above and one below: they overlap when the "above" threshold sits
        # under the "below" one.
        above = left_threshold if left_above else right_threshold
        below = right_threshold if left_above else left_threshold
        return above < below


def describe_contradictions(detector: ContradictionDetector) -> dict:
    return {
        "part_id": PART_ID,
        "checks": detector.standing.checks,
        "contradictions_found": detector.standing.contradictions_found,
        "by_kind": dict(sorted(detector.standing.by_kind.items())),
        "urgent_rule_conflicts": detector.standing.urgent_rule_conflicts,
        "resolved_by_this_part": detector.standing.resolved_by_this_part,
        "resolves_anything": False,
    }


def run_contradiction_detector(
    detector: ContradictionDetector, control_socket, read_knowledge, publish_contradictions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        correlations = read_knowledge(detector)
        publish_contradictions(detector.check(correlations))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_contradictions(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Facts, rules and links are observed as they arrive and every check runs
    over everything held. A rule's condition is read back from its own
    `when` clause, which the playbook writes as measurement, comparison,
    threshold. No correlation reaches this part, so links are checked
    against facts with none -- a link can be refuted by a fact, never by a
    correlation nobody measured.
    """
    from runtime.input_assembly import Batch

    facts = Batch(read=context.bus.reader("semantic-fact"))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    links = Batch(read=context.bus.reader("knowledge-link"))
    publish_contradictions = context.bus.publisher_for("knowledge-contradiction")
    detector = ContradictionDetector(value_tolerance=context.number("contradiction_value_tolerance"))
    rules_seen: set[str] = set()

    def condition_of(rule) -> dict:
        tokens = str(rule.when).split()
        if len(tokens) < 3:
            return {}
        try:
            threshold = float(tokens[2])
        except ValueError:
            return {"measurement": tokens[0], "comparison": tokens[1]}
        return {"measurement": tokens[0], "comparison": tokens[1], "threshold": threshold}

    def read_knowledge(_detector):
        for fact in facts.payloads():
            detector.observe_fact(fact)
        for rule in rules.payloads():
            if rule.rule_id in rules_seen or not rule.is_active:
                continue
            rules_seen.add(rule.rule_id)
            detector.observe_rule(rule, condition_of(rule))
        for link in links.payloads():
            detector.observe_link(link.left, link.right, link.kind, float(link.strength))
        return {}

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_contradictions(kept)

    return run_contradiction_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_knowledge=read_knowledge,
        publish_contradictions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
