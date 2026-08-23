"""procedural-playbook: what to do, applied by rule and never searched.

The third memory tier, and the one whose design is a prohibition. The semantic
store is queried and the episodic store is recalled from; this one is **not
searched**, and that is deliberate:

A searchable playbook lets a decision go looking for a rule that permits what it
already wants. There is always a nearby precedent, and the search finds it -- so
a rule that can be searched for is a suggestion, and a system with suggestions
has no procedure at all.

So the playbook is applied: given the current conditions, the rules that match
fire, in a defined order, and nothing chooses among them.

- **Rules come from instructions that survived every gate.** Nothing is written
  here directly; the playbook is downstream of the whole hypothesis pipeline.
- **A retired instruction's rule is deactivated immediately.** A rule outliving
  its instruction is the system following a procedure whose justification it has
  already discarded.
- **Rules are ordered and the order is stated.** When two fire, the more specific
  applies -- a rule conditioned on a regime beats one that is not -- and ties go
  to the older rule, which has more evidence behind it.
- **Every application is counted.** A rule that has never fired is not a rule the
  system follows; it is one it has.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import PlaybookRule
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "procedural-playbook"

PART_DECLARATION = PartDeclaration(
    part_id="procedural-playbook",
    consumes=("opportunity-instruction", "retired-instruction"),
    produces=("playbook-rule", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

APPLIED = "applied"
NOTHING_MATCHED = "no-rule-matches-these-conditions"
DEACTIVATED = "deactivated"


@dataclass(frozen=True)
class Application:
    """Which rules fired for one set of conditions, in the order they apply."""

    state: str
    rules: tuple
    conditions: dict
    rules_considered: int
    reason: str
    applied_at_ns: int

    @property
    def anything_fired(self) -> bool:
        return self.state == APPLIED

    @property
    def governing_rule(self) -> PlaybookRule | None:
        return self.rules[0] if self.rules else None


@dataclass
class PlaybookStanding:
    rules_written: int = 0
    rules_deactivated: int = 0
    applications: int = 0
    applications_with_nothing_matching: int = 0
    rules_that_have_never_fired: int = 0
    by_rule: dict = field(default_factory=dict)


class ProceduralPlaybook:
    """Applies rules to conditions. There is no search method, by design."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._rules: dict[str, PlaybookRule] = {}
        self._conditions: dict[str, dict] = {}
        self._applied_counts: dict[str, int] = {}
        self._written_at: dict[str, int] = {}
        self.standing = PlaybookStanding()

    def write_from_instruction(self, instruction, then: str) -> PlaybookRule:
        """A rule, from an instruction that survived every gate.

        Nothing is written here directly: the playbook is downstream of the
        whole hypothesis pipeline, so a rule always has that pipeline behind it.
        """
        rule_id = f"rule:{instruction.instruction_id}"
        rule = PlaybookRule(
            rule_id=rule_id,
            when=(
                f"{instruction.measurement} {instruction.comparison} "
                f"{instruction.threshold:.6g}"
                + (f" in the {instruction.regime_tag} regime" if instruction.regime_tag else "")
            ),
            then=then,
            instruction_id=instruction.instruction_id,
            regime_tag=instruction.regime_tag,
            is_active=True,
            times_applied=0,
            reason=(
                f"written from {instruction.instruction_id}, which cleared every gate: "
                f"{instruction.reason}"
            ),
            written_at_ns=self._now_ns(),
        )
        self._rules[rule_id] = rule
        self._conditions[rule_id] = {
            "measurement": instruction.measurement,
            "comparison": instruction.comparison,
            "threshold": instruction.threshold,
            "regime": instruction.regime_tag,
        }
        self._written_at[rule_id] = rule.written_at_ns
        self.standing.rules_written += 1
        return rule

    def deactivate_for_instruction(self, instruction_id: str) -> tuple:
        """A retired instruction's rules stop immediately.

        A rule outliving its instruction is the system following a procedure
        whose justification it has already discarded.
        """
        deactivated = []
        for rule_id, rule in list(self._rules.items()):
            if rule.instruction_id != instruction_id or not rule.is_active:
                continue
            self._rules[rule_id] = PlaybookRule(
                rule_id=rule.rule_id, when=rule.when, then=rule.then,
                instruction_id=rule.instruction_id, regime_tag=rule.regime_tag,
                is_active=False, times_applied=rule.times_applied,
                reason=(
                    f"{rule.reason}. Deactivated because {instruction_id} was retired -- a rule "
                    f"outliving its instruction is a procedure whose justification has been "
                    f"discarded"
                ),
                written_at_ns=rule.written_at_ns,
            )
            deactivated.append(self._rules[rule_id])
            self.standing.rules_deactivated += 1
        return tuple(deactivated)

    def apply(self, conditions: dict, regime: str | None = None) -> Application:
        """Every active rule that matches, in the order they apply.

        There is no method that returns rules by similarity or by name: a rule
        that can be searched for is one a decision can go looking for to permit
        what it already wants.
        """
        self.standing.applications += 1
        matching = []

        for rule_id, rule in self._rules.items():
            if not rule.is_active:
                continue
            if not self._matches(rule_id, conditions, regime):
                continue
            matching.append(rule)

        if not matching:
            self.standing.applications_with_nothing_matching += 1
            return Application(
                state=NOTHING_MATCHED, rules=(), conditions=dict(conditions),
                rules_considered=len(self._rules),
                reason=(
                    f"none of {len(self._rules)} rule(s) matches these conditions. That is the "
                    f"procedure: nothing is chosen when nothing applies"
                ),
                applied_at_ns=self._now_ns(),
            )

        # The more specific rule applies; ties go to the older one, which has
        # more evidence behind it.
        matching.sort(
            key=lambda rule: (rule.regime_tag is None, self._written_at.get(rule.rule_id, 0))
        )

        fired = []
        for rule in matching:
            count = self._applied_counts.get(rule.rule_id, 0) + 1
            self._applied_counts[rule.rule_id] = count
            self.standing.by_rule[rule.rule_id] = count
            fired.append(
                PlaybookRule(
                    rule_id=rule.rule_id, when=rule.when, then=rule.then,
                    instruction_id=rule.instruction_id, regime_tag=rule.regime_tag,
                    is_active=rule.is_active, times_applied=count, reason=rule.reason,
                    written_at_ns=rule.written_at_ns,
                )
            )

        return Application(
            state=APPLIED, rules=tuple(fired), conditions=dict(conditions),
            rules_considered=len(self._rules),
            reason=(
                f"{len(fired)} rule(s) fired, most specific first: "
                + "; ".join(f"{rule.when} then {rule.then}" for rule in fired)
                + ". Applied rather than searched, because a rule that can be searched for is "
                "a suggestion"
            ),
            applied_at_ns=self._now_ns(),
        )

    def rules_never_fired(self) -> tuple:
        """A rule that has never fired is one the system has rather than follows."""
        never = tuple(
            rule for rule_id, rule in sorted(self._rules.items())
            if rule.is_active and self._applied_counts.get(rule_id, 0) == 0
        )
        self.standing.rules_that_have_never_fired = len(never)
        return never

    def _matches(self, rule_id: str, conditions: dict, regime: str | None) -> bool:
        condition = self._conditions.get(rule_id)
        if condition is None:
            return False
        if condition["regime"] is not None and condition["regime"] != regime:
            return False
        value = conditions.get(condition["measurement"])
        if value is None:
            return False
        if condition["comparison"] == "above":
            return value > condition["threshold"]
        return value < condition["threshold"]

    @property
    def active_rules(self) -> tuple:
        return tuple(rule for rule in self._rules.values() if rule.is_active)


def describe_playbook(playbook: ProceduralPlaybook) -> dict:
    never_fired = playbook.rules_never_fired()
    return {
        "part_id": PART_ID,
        "rules_written": playbook.standing.rules_written,
        "rules_active": len(playbook.active_rules),
        "rules_deactivated": playbook.standing.rules_deactivated,
        "applications": playbook.standing.applications,
        "applications_with_nothing_matching": (
            playbook.standing.applications_with_nothing_matching
        ),
        "rules_that_have_never_fired": len(never_fired),
        "by_rule": dict(sorted(playbook.standing.by_rule.items())),
        "is_searchable": False,
    }


def run_procedural_playbook(
    playbook: ProceduralPlaybook, control_socket, read_instructions, publish_rules,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_instructions(playbook)
        publish_rules(playbook.active_rules)

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

    Every written instruction becomes a rule whose consequence is the
    direction and expectation it names; a retirement deactivates the rules
    written from that instruction. Active rules go out once per health
    interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    instructions = Batch(read=context.bus.reader("opportunity-instruction"))
    retirements = Batch(read=context.bus.reader("retired-instruction"))
    publish_rules = context.bus.publisher_for("playbook-rule")
    playbook = ProceduralPlaybook()
    written: set[str] = set()
    last_publish = [float("-inf")]

    def read_instructions(_playbook) -> None:
        for instruction in instructions.payloads():
            if instruction.instruction_id in written:
                continue
            written.add(instruction.instruction_id)
            playbook.write_from_instruction(instruction, then=f"{instruction.direction} expecting {instruction.expectation}")
        for record in retirements.payloads():
            if record.is_retired:
                playbook.deactivate_for_instruction(record.instruction_id)

    def publish(items) -> None:
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_rules(kept)
            last_publish[0] = now

    return run_procedural_playbook(
        playbook=playbook,
        control_socket=context.control_socket,
        read_instructions=read_instructions,
        publish_rules=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
