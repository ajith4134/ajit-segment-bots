"""watch-condition-compiler: turn a proven instruction into something testable everywhere.

RL-009's mechanism. A bot that learns something true about one symbol has learned
something the system can only use once; compiled into a condition, the same
finding is tested against every symbol in the universe on every tick. That is the
difference between a strategy and a system that keeps finding the same
opportunity wherever it appears.

A condition is a **declared comparison over named measurements**, never code.
Three reasons, and the third is the one that matters:

- It can be checked before it runs, so a malformed instruction fails at compile
  rather than mid-scan.
- It can be evaluated on any symbol without knowing what produced it.
- Nothing the system learns can ever execute. An instruction that compiled to
  code would be an autonomous system writing its own executable, and this project
  keeps that boundary where a person can see it.

**A retired instruction is decompiled immediately.** An instruction that stopped
working keeps producing candidates until its condition is removed, and those are
the worst candidates the system can produce -- they carry the authority of having
been proven.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "watch-condition-compiler"

PART_DECLARATION = PartDeclaration(
    part_id="watch-condition-compiler",
    consumes=("proven-instruction", "retired-instruction", "opportunity-instruction"),
    produces=("watch-condition", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The comparisons an instruction may express. A closed set, because anything
# outside it would need code to evaluate.
ABOVE = "above"
BELOW = "below"
CROSSES_ABOVE = "crosses-above"
CROSSES_BELOW = "crosses-below"
BETWEEN = "between"
COMPARISONS = (ABOVE, BELOW, CROSSES_ABOVE, CROSSES_BELOW, BETWEEN)

COMPILED = "compiled"
REFUSED_UNKNOWN_MEASUREMENT = "refused-unknown-measurement"
REFUSED_UNKNOWN_COMPARISON = "refused-unknown-comparison"
REFUSED_MALFORMED = "refused-malformed-threshold"


class ConditionRefused(ValueError):
    """An instruction could not be compiled into a testable condition."""


@dataclass(frozen=True)
class WatchCondition:
    """One testable claim, evaluable on any symbol without knowing its origin."""

    condition_id: str
    instruction_id: str
    measurement: str
    comparison: str
    threshold: float
    upper_threshold: float | None
    direction: str
    expectation: str
    horizon_seconds: float
    compiled_at_ns: int

    def evaluate(self, value: float, previous_value: float | None = None) -> bool:
        """Whether this condition holds for one measured value.

        `previous_value` is required by the crossing comparisons and only by
        them: a cross is a statement about two observations, and evaluating one
        without the other would silently become a level test.
        """
        if self.comparison == ABOVE:
            return value > self.threshold
        if self.comparison == BELOW:
            return value < self.threshold
        if self.comparison == BETWEEN:
            return self.threshold <= value <= (self.upper_threshold or self.threshold)
        if previous_value is None:
            return False
        if self.comparison == CROSSES_ABOVE:
            return previous_value <= self.threshold < value
        return previous_value >= self.threshold > value


@dataclass
class CompilerStanding:
    compiled: int = 0
    refused: int = 0
    retired: int = 0
    active_conditions: int = 0
    by_measurement: dict = field(default_factory=dict)
    last_refusal: str | None = None
    # Proven instructions that arrived carrying only their id: a ProvenInstruction
    # names the instruction and its runs, not the measurement, comparison and
    # threshold a condition is compiled from, which live on the
    # opportunity-instruction this part does not consume. Counted so the gap is
    # a number on the board; the fix is a blueprint edit (RL-062).
    proven_without_a_body: int = 0


class WatchConditionCompiler:
    """Compiles proven instructions into declared conditions, and retires them."""

    def __init__(self, known_measurements: tuple[str, ...], now_ns=time.time_ns) -> None:
        if not known_measurements:
            raise ValueError("a compiler with no known measurements can compile nothing")
        self._known = set(known_measurements)
        self._now_ns = now_ns
        self._conditions: dict[str, WatchCondition] = {}
        self.standing = CompilerStanding()

    def compile_instruction(
        self,
        instruction_id: str,
        measurement: str,
        comparison: str,
        threshold: float,
        direction: str,
        expectation: str,
        horizon_seconds: float,
        upper_threshold: float | None = None,
    ) -> WatchCondition:
        """Compile one instruction, refusing anything that cannot be evaluated."""
        if measurement not in self._known:
            self.standing.refused += 1
            self.standing.last_refusal = f"{instruction_id}: {measurement!r} is not measured anywhere"
            raise ConditionRefused(
                f"{measurement!r} is not a measurement this system produces; a condition over it "
                f"could never be evaluated and would silently never fire"
            )
        if comparison not in COMPARISONS:
            self.standing.refused += 1
            self.standing.last_refusal = f"{instruction_id}: {comparison!r} is not a comparison"
            raise ConditionRefused(
                f"{comparison!r} is not one of {COMPARISONS}; anything else would need code, "
                f"and nothing this system learns may execute"
            )
        if comparison == BETWEEN and (upper_threshold is None or upper_threshold < threshold):
            self.standing.refused += 1
            self.standing.last_refusal = f"{instruction_id}: a range needs an upper bound above its lower"
            raise ConditionRefused("a 'between' condition needs an upper threshold above its lower")

        condition = WatchCondition(
            condition_id=f"{instruction_id}:{measurement}:{comparison}",
            instruction_id=instruction_id,
            measurement=measurement,
            comparison=comparison,
            threshold=threshold,
            upper_threshold=upper_threshold,
            direction=direction,
            expectation=expectation,
            horizon_seconds=horizon_seconds,
            compiled_at_ns=self._now_ns(),
        )
        self._conditions[condition.condition_id] = condition
        self.standing.compiled += 1
        self.standing.active_conditions = len(self._conditions)
        self.standing.by_measurement[measurement] = (
            self.standing.by_measurement.get(measurement, 0) + 1
        )
        return condition

    def retire_instruction(self, instruction_id: str) -> int:
        """Remove every condition compiled from one instruction. Returns how many.

        Immediately, because a retired instruction's conditions keep producing
        candidates that carry the authority of having been proven.
        """
        removed = [
            condition_id
            for condition_id, condition in self._conditions.items()
            if condition.instruction_id == instruction_id
        ]
        for condition_id in removed:
            del self._conditions[condition_id]
        self.standing.retired += len(removed)
        self.standing.active_conditions = len(self._conditions)
        return len(removed)

    @property
    def conditions(self) -> tuple[WatchCondition, ...]:
        return tuple(self._conditions[key] for key in sorted(self._conditions))


def describe_conditions(compiler: WatchConditionCompiler) -> dict:
    return {
        "part_id": PART_ID,
        "compiled": compiler.standing.compiled,
        "refused": compiler.standing.refused,
        "retired": compiler.standing.retired,
        "active_conditions": compiler.standing.active_conditions,
        "by_measurement": dict(compiler.standing.by_measurement),
        "last_refusal": compiler.standing.last_refusal,
    }


def run_watch_condition_compiler(
    compiler: WatchConditionCompiler, control_socket, read_instructions, publish_conditions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        proven, retired = read_instructions()
        for instruction_id in retired:
            compiler.retire_instruction(instruction_id)
        for instruction in proven:
            try:
                compiler.compile_instruction(**instruction)
            except ConditionRefused:
                continue
        publish_conditions(compiler.conditions)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_conditions(compiler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Retirements are applied as they arrive. A proven instruction arrives
    carrying its id and its runs, not the measurement, comparison and
    threshold a condition is compiled from; those live on the
    opportunity-instruction, which this part consumes since 2026-08-25 and
    joins on the id both sides carry. A verdict whose instruction has not
    arrived is still counted as proven without a body and nothing is compiled
    from it -- a condition invented from an id would watch for nothing the
    instruction said. The known measurements are the shared vocabulary in
    runtime.sweep_measurements, which the sweeper computes.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.sweep_measurements import KNOWN_MEASUREMENTS

    proven = Batch(read=context.bus.reader("proven-instruction"))
    instructions = LatestByKey(
        read=context.bus.reader("opportunity-instruction"),
        key_of=lambda instruction: instruction.instruction_id,
    )
    retired = Batch(read=context.bus.reader("retired-instruction"))
    publish_conditions = context.bus.publisher_for("watch-condition")
    compiler = WatchConditionCompiler(known_measurements=KNOWN_MEASUREMENTS)

    def read_instructions():
        bodies = []
        body_by_id = instructions.mapping()
        for verdict in proven.payloads():
            # A verdict names the instruction it is about; the body is the
            # instruction itself, joined on the id both sides carry. Until
            # 2026-08-25 the body was read off the verdict through a getattr
            # default -- ProvenInstruction has never had one -- so every proven
            # instruction was counted as bodyless and no condition was ever
            # compiled from one.
            body = body_by_id.get(verdict.instruction_id)
            if body is None:
                compiler.standing.proven_without_a_body += 1
                continue
            bodies.append({
                "instruction_id": verdict.instruction_id,
                "measurement": body.measurement, "comparison": body.comparison,
                "threshold": body.threshold, "direction": body.direction,
                "expectation": body.expectation, "horizon_seconds": body.horizon_seconds,
            })
        return tuple(bodies), tuple(item.instruction_id for item in retired.payloads())

    def publish(conditions) -> None:
        if conditions:
            publish_conditions(tuple(conditions))

    return run_watch_condition_compiler(
        compiler=compiler,
        control_socket=context.control_socket,
        read_instructions=read_instructions,
        publish_conditions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
