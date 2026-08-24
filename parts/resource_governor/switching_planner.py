"""switching-planner: which parts turn on or off next.

The one part that decides. Everything else in this block measures or reports;
this weighs fourteen inputs and produces a plan, and only gate-actuator acts on
it. That split is T-2: the control path is separate from the data path, and no
feature ever switches itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "switching-planner"

PART_DECLARATION = PartDeclaration(
    part_id="switching-planner",
    consumes=(
        "hardware-capacity", "part-resource-usage", "hog-report", "part-priority",
        "restart-request", "replacement-plan", "conservation-plan", "admitted-part",
        "io-pressure", "memory-forecast", "restart-budget", "duty-cycle",
        "flap-report", "resource-reservation",
    ),
    produces=("switch-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TURN_ON = "on"
TURN_OFF = "off"

# Reasons a part is switched, in the order they beat each other. First match
# wins, so the list is the policy: a flapping part is held even if capacity
# would allow it, and a reservation floor beats a hog report.
OFF_REASONS = ("memory-exhaustion-imminent", "hog-under-contention", "io-starvation", "outside-duty-cycle")
ON_REASONS = ("reserved-floor", "admitted", "capacity-available")


@dataclass(frozen=True)
class SwitchDecision:
    part_id: str
    action: str
    reason: str
    priority: int


@dataclass(frozen=True)
class SwitchPlan:
    """What to flip, in order, and what was deliberately left alone."""

    decisions: tuple[SwitchDecision, ...]
    held: tuple[str, ...]
    unplannable_reason: str | None
    planned_at_ns: int

    @property
    def is_plannable(self) -> bool:
        return self.unplannable_reason is None


@dataclass
class GovernorInputs:
    """Everything the planner is allowed to weigh. Missing is not the same as empty."""

    capacity: object | None = None
    usages: tuple = ()
    hog_reports: tuple = ()
    priorities: dict = field(default_factory=dict)
    restart_requests: tuple = ()
    restart_budgets: dict = field(default_factory=dict)
    flap_reports: dict = field(default_factory=dict)
    duty_cycles: dict = field(default_factory=dict)
    reservations: tuple = ()
    memory_forecast: object | None = None
    io_pressure: object | None = None
    admitted_parts: tuple = ()
    replacement_plan: tuple = ()
    conservation_plan: tuple = ()
    running_parts: tuple = ()
    current_hour: int | None = None


@dataclass
class PlannerStanding:
    plans: int = 0
    refusals: int = 0
    switched_on: int = 0
    switched_off: int = 0
    held: int = 0
    by_reason: dict = field(default_factory=dict)


class SwitchingPlanner:
    """Turns measured pressure into a switch plan, or refuses when it cannot see.

    Refusal matters more than the plan. With no capacity reading, "switch nothing
    off" and "switch everything off" are equally defensible and both are guesses;
    the planner says it cannot plan and the machine keeps running as it was,
    which is the only outcome that cannot make things worse.
    """

    def __init__(
        self,
        memory_exhaustion_warning_seconds: float,
        io_stall_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        self._memory_warning = memory_exhaustion_warning_seconds
        self._io_stall = io_stall_fraction
        self._now_ns = now_ns
        self.standing = PlannerStanding()

    def plan(self, inputs: GovernorInputs) -> SwitchPlan:
        self.standing.plans += 1
        if inputs.capacity is None or not inputs.capacity.is_complete:
            self.standing.refusals += 1
            missing = (
                "no hardware-capacity reading"
                if inputs.capacity is None
                else f"capacity is missing {', '.join(inputs.capacity.unmeasurable)}"
            )
            return SwitchPlan((), (), f"cannot plan: {missing}", self._now_ns())

        decisions: list[SwitchDecision] = []
        held: list[str] = []
        reserved = {r.part_id for r in inputs.reservations if r.state == "honoured"}

        for part_id in inputs.running_parts:
            reason = self._off_reason(part_id, inputs, reserved)
            if reason is not None:
                decisions.append(SwitchDecision(part_id, TURN_OFF, reason, self._priority(part_id, inputs)))

        for part_id in self._candidates_to_start(inputs):
            if part_id in inputs.running_parts:
                continue
            hold = self._hold_reason(part_id, inputs)
            if hold is not None:
                held.append(part_id)
                continue
            decisions.append(
                SwitchDecision(part_id, TURN_ON, self._on_reason(part_id, reserved), self._priority(part_id, inputs))
            )

        # Off before on, and within each, by priority: freeing room before
        # claiming it is what keeps a plan from asking for what it just spent.
        decisions.sort(key=lambda d: (d.action != TURN_OFF, d.priority, d.part_id))
        for decision in decisions:
            self.standing.by_reason[decision.reason] = self.standing.by_reason.get(decision.reason, 0) + 1
            if decision.action == TURN_ON:
                self.standing.switched_on += 1
            else:
                self.standing.switched_off += 1
        self.standing.held += len(held)
        return SwitchPlan(tuple(decisions), tuple(sorted(held)), None, self._now_ns())

    def _off_reason(self, part_id, inputs, reserved) -> str | None:
        forecast = inputs.memory_forecast
        if (
            forecast is not None
            and forecast.seconds_to_exhaustion is not None
            and forecast.seconds_to_exhaustion <= self._memory_warning
            and forecast.fastest_growing_part == part_id
            and part_id not in reserved
        ):
            return OFF_REASONS[0]
        if any(report.part_id == part_id and report.contended for report in inputs.hog_reports) and part_id not in reserved:
            return OFF_REASONS[1]
        pressure = inputs.io_pressure
        if (
            pressure is not None
            and pressure.is_measured
            and pressure.full_stalled_10s is not None
            and pressure.full_stalled_10s >= self._io_stall
            and any(r.part_id == part_id for r in inputs.hog_reports)
            and part_id not in reserved
        ):
            return OFF_REASONS[2]
        duty = inputs.duty_cycles.get(part_id)
        if duty is not None and inputs.current_hour is not None and inputs.current_hour not in duty.allowed_hours:
            return OFF_REASONS[3]
        return None

    def _candidates_to_start(self, inputs) -> tuple[str, ...]:
        candidates = list(inputs.admitted_parts) + list(inputs.restart_requests)
        candidates += [r.part_id for r in inputs.reservations if r.state == "honoured"]
        candidates += list(inputs.replacement_plan) + list(inputs.conservation_plan)
        seen, ordered = set(), []
        for part_id in candidates:
            if part_id not in seen:
                seen.add(part_id)
                ordered.append(part_id)
        return tuple(ordered)

    def _hold_reason(self, part_id, inputs) -> str | None:
        if part_id in inputs.flap_reports:
            return "flapping"
        budget = inputs.restart_budgets.get(part_id)
        if budget is not None and budget.verdict == "exhausted":
            return "restart budget spent"
        duty = inputs.duty_cycles.get(part_id)
        if duty is not None and inputs.current_hour is not None and inputs.current_hour not in duty.allowed_hours:
            return "outside its duty cycle"
        return None

    def _on_reason(self, part_id, reserved) -> str:
        return ON_REASONS[0] if part_id in reserved else ON_REASONS[1]

    def _priority(self, part_id, inputs) -> int:
        return int(inputs.priorities.get(part_id, 50))


def describe_planning(planner: SwitchingPlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans": planner.standing.plans,
        "refusals": planner.standing.refusals,
        "switched_on": planner.standing.switched_on,
        "switched_off": planner.standing.switched_off,
        "held": planner.standing.held,
        "by_reason": dict(planner.standing.by_reason),
    }


def run_switching_planner(
    planner: SwitchingPlanner, control_socket, read_inputs, publish_plan,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_plan(planner.plan(read_inputs())),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_planning(planner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Fourteen declared inputs, and the assembly below is where each one is stated to
    be a level or an event -- a distinction the bus cannot make for a part and the
    part must not leave implicit. Capacity, pressure and forecasts are levels: the
    machine still has the cores it had when nothing new arrived. Restart requests
    are events: acting on the same request every tick would restart a part forever.

    Missing is deliberately not empty. A level nobody has published yet reads as
    None, and `plan` refuses to plan without a complete capacity reading rather
    than planning against a zero -- which is the refusal this part exists for.
    """
    import time as clock
    from datetime import UTC, datetime

    from runtime.input_assembly import Batch, LatestByKey, LatestValue

    def by_part(data_type: str) -> LatestByKey:
        return LatestByKey(read=context.bus.reader(data_type), key_of=lambda payload: payload.part_id)

    capacity = LatestValue(read=context.bus.reader("hardware-capacity"))
    usages = by_part("part-resource-usage")
    priorities = by_part("part-priority")
    restart_budgets = by_part("restart-budget")
    flap_reports = by_part("flap-report")
    duty_cycles = by_part("duty-cycle")
    reservations = by_part("resource-reservation")
    memory_forecast = LatestValue(read=context.bus.reader("memory-forecast"))
    io_pressure = LatestValue(read=context.bus.reader("io-pressure"))
    replacement_plan = LatestValue(read=context.bus.reader("replacement-plan"))
    conservation_plan = LatestValue(read=context.bus.reader("conservation-plan"))
    hog_reports = Batch(read=context.bus.reader("hog-report"))
    restart_requests = Batch(read=context.bus.reader("restart-request"))
    admitted_parts = Batch(read=context.bus.reader("admitted-part"))

    publish_plan = context.bus.publisher_for("switch-plan")

    def read_inputs() -> GovernorInputs:
        usage_by_part = usages.mapping()
        return GovernorInputs(
            capacity=capacity.value(),
            usages=tuple(usage_by_part.values()),
            hog_reports=hog_reports.payloads(),
            priorities={part_id: entry.priority for part_id, entry in priorities.mapping().items()},
            restart_requests=restart_requests.payloads(),
            restart_budgets=restart_budgets.mapping(),
            flap_reports=flap_reports.mapping(),
            duty_cycles=duty_cycles.mapping(),
            reservations=reservations.values(),
            memory_forecast=memory_forecast.value(),
            io_pressure=io_pressure.value(),
            admitted_parts=admitted_parts.payloads(),
            replacement_plan=replacement_plan.value() or (),
            conservation_plan=conservation_plan.value() or (),
            # What is running is what is reporting its own usage. The planner never
            # asks the launcher: a part that asked the substrate who else exists
            # would know the circuit, which is exactly what T-4 forbids.
            running_parts=tuple(sorted(usage_by_part)),
            current_hour=datetime.now(UTC).hour,
        )

    return run_switching_planner(
        planner=SwitchingPlanner(
            memory_exhaustion_warning_seconds=context.number("memory_exhaustion_warning"),
            io_stall_fraction=context.number("io_stall_fraction"),
        ),
        control_socket=context.control_socket,
        read_inputs=read_inputs,
        publish_plan=lambda plan: publish_plan([plan]),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
