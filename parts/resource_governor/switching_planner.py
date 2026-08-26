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

# How many consecutive plans a part must be absent from the metering before it
# is planned on. Not tunable and not a threshold: the metering sweeps once per
# part_usage_cadence_seconds and plans are made at the same cadence, so a part
# absent from two consecutive plans has been missed by at least one whole sweep
# -- where a part absent from one may simply not have been measured yet. The
# first plans after every spine start used to switch "on" whichever reserved
# parts the first half-drained sweep had not covered, and the actuator recorded
# the failed flips of parts that were running all along (2026-08-24, twice).
PLANS_ABSENT_BEFORE_START = 2

# Reasons a part is switched, in the order they beat each other. First match
# wins, so the list is the policy: a flapping part is held even if capacity
# would allow it, and a reservation floor beats a hog report.
OFF_REASONS = (
    "memory-exhaustion-imminent", "hog-under-contention", "io-starvation", "outside-duty-cycle",
    # A conservation plan says what to stop while the runway is short, in the
    # order that costs the least. It reached this part as a candidate to *start*
    # until 2026-08-25 -- the list of parts to stop, handed to the wrong half of
    # the plan -- and it crashed before it could act on that, because a
    # ConservationPlan is one object and the code iterated it as a list of ids.
    "conserving-a-short-runway",
)
ON_REASONS = ("reserved-floor", "admitted", "capacity-available")


def part_id_of(item) -> str:
    """The part an ask is about, whether it arrived as an id or as the ask itself."""
    return item if isinstance(item, str) else item.part_id


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
    # The plan objects themselves, not lists of part ids: a ReplacementPlan names
    # one part and its steps, and a ConservationPlan names what to stop.
    replacement_plan: object | None = None
    conservation_plan: object | None = None
    running_parts: tuple = ()
    current_hour: int | None = None


@dataclass
class PlannerStanding:
    plans: int = 0
    refusals: int = 0
    switched_on: int = 0
    switched_off: int = 0
    held: int = 0
    # Parts switched off by this planner that are waiting for the condition which
    # shed them to clear, and parts planned back on because it has. Counted apart
    # from switched_on because they answer the question the ratchet of 2026-08-25
    # could not: does anything this governor turns off ever come back.
    off_until_the_pressure_clears: int = 0
    restored: int = 0
    # Hog decisions this plan deliberately did not make, because one measurement
    # can only justify shedding one part before it is taken again.
    hogs_deferred: int = 0
    # What the last plan actually saw, so "why is that part still off" is
    # answerable from the health table instead of by reading this code. Each is
    # the reading itself, not a verdict about it.
    sheds_confirmed_gone: int = 0
    sheds_still_stopping: int = 0
    conservation_plan_names: int = 0
    hog_reports_read: int = 0
    conditions_that_have_passed: int = 0
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
        never_switched_off_priority_ceiling: int,
        plans_between_on_retries: int,
        now_ns=time.time_ns,
    ) -> None:
        self._memory_warning = memory_exhaustion_warning_seconds
        self._io_stall = io_stall_fraction
        self._never_off_ceiling = never_switched_off_priority_ceiling
        self._plans_between_on_retries = plans_between_on_retries
        self._now_ns = now_ns
        # Consecutive plans each candidate has been absent from the metering.
        self._plans_absent: dict[str, int] = {}
        # Every part this planner has itself seen in a metering sweep. A
        # reservation only reconciles a part that is in here: absent-but-known
        # is evidence of an off part, never-known is a part still starting.
        self._ever_seen_running: set[str] = set()
        # What this planner switched off, and the reason it gave. Kept so the
        # reason can be re-read on every plan: an off with no way back is not
        # governance, it is a ratchet. Measured 2026-08-25 20:05 to 21:08, the
        # cost of not keeping it: 152 switch-records, every one an off, every
        # one "hog-under-contention", switched_on 0 across 30,150 plans. Among
        # the 42 parts left off were order-book-reader, venue-quote-stream-reader
        # and tick-size-resolver, and the bot placed no order for the eight hours
        # that followed.
        self._switched_off: dict[str, str] = {}
        # Which of those the metering has since confirmed gone. A shed part must
        # be observed absent before it can be observed back: without that, the
        # sweep taken while it was still stopping reads as a part that returned.
        self._seen_gone: set[str] = set()
        # The plan number each part was last asked on at. An ask cannot be
        # judged to have failed until a part has had time to start and to be
        # metered, and until then re-asking produces a refusal rather than a
        # part.
        self._asked_on_at: dict[str, int] = {}
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
        if not inputs.usages:
            # Missing is not empty, for what runs exactly as for capacity. No
            # part-resource-usage reading means the metering is absent -- the
            # appetite meter off, or the parts outside their scopes -- not that
            # nothing is running. Planning against it reads every running part
            # as off: measured live 2026-08-24, the first plans this part ever
            # produced switched "on" all ten reserved parts, every one of them
            # already running, ten times a plan, and the actuator recorded 324
            # failed flips of parts that never stopped before the spine came down.
            self.standing.refusals += 1
            return SwitchPlan(
                (), (), "cannot plan: no part-resource-usage readings, so what is "
                "running is unknown rather than nothing", self._now_ns(),
            )

        decisions: list[SwitchDecision] = []
        held: list[str] = []
        reserved = {r.part_id for r in inputs.reservations if r.state == "honoured"}

        for part_id in inputs.running_parts:
            self._plans_absent.pop(part_id, None)
            self._ever_seen_running.add(part_id)
            if part_id in self._switched_off:
                # Shed, and still in the sweep. Which of the two things that can
                # mean is decided by _seen_gone and never by this reading alone:
                # a part takes about two seconds to stop and the metering it was
                # measured in is a second old, so the sweeps either side of an
                # off still carry it. Measured live 2026-08-26 04:35: reading
                # presence here as "it came back" counted 112 restorations
                # against 80 offs and 0 ons -- and worse, forgot every shed as
                # it was made, which is the return path defeating itself.
                if part_id in self._seen_gone:
                    del self._switched_off[part_id]
                    self._seen_gone.discard(part_id)
                    self._asked_on_at.pop(part_id, None)
                    self.standing.restored += 1
                else:
                    continue
            reason = self._off_reason(part_id, inputs, reserved)
            if reason is not None:
                decisions.append(SwitchDecision(part_id, TURN_OFF, reason, self._priority(part_id, inputs)))

        for part_id in self._switched_off:
            if part_id not in inputs.running_parts:
                # Observed gone. Only now can this part be observed back.
                self._seen_gone.add(part_id)

        decisions = self._one_hog_per_measurement(decisions, inputs)

        cleared = self._sheds_whose_condition_has_passed(inputs)
        self.standing.sheds_confirmed_gone = len(self._seen_gone)
        self.standing.sheds_still_stopping = len(self._switched_off) - len(self._seen_gone)
        self.standing.conditions_that_have_passed = len(cleared)
        self.standing.hog_reports_read = len(inputs.hog_reports)
        self.standing.conservation_plan_names = (
            len(inputs.conservation_plan.parts_to_stop) if inputs.conservation_plan is not None else -1
        )
        for part_id in self._candidates_to_start(inputs, cleared):
            if part_id in inputs.running_parts:
                continue
            asked_at = self._asked_on_at.get(part_id)
            if asked_at is not None and self.standing.plans - asked_at < self._plans_between_on_retries:
                # Asked for already, and not yet long enough ago to know whether
                # the ask worked. A part takes a second or two to start and
                # another sweep to be metered, so re-asking on the next plan is
                # how gate-actuator came to record 17 refusals reading
                # "PartAlreadyRunning" between 05:21 and 05:40 on 2026-08-26.
                held.append(part_id)
                continue
            self._plans_absent[part_id] = self._plans_absent.get(part_id, 0) + 1
            if self._plans_absent[part_id] < PLANS_ABSENT_BEFORE_START:
                # Absent from the metering is not yet off: the sweep may simply
                # not have reached it. Held until a whole sweep has missed it.
                held.append(part_id)
                continue
            hold = self._hold_reason(part_id, inputs)
            if hold is not None:
                held.append(part_id)
                continue
            decisions.append(
                SwitchDecision(
                    part_id, TURN_ON, self._on_reason(part_id, reserved, cleared), self._priority(part_id, inputs)
                )
            )

        # Off before on, and within each, by priority: freeing room before
        # claiming it is what keeps a plan from asking for what it just spent.
        decisions.sort(key=lambda d: (d.action != TURN_OFF, d.priority, d.part_id))
        for decision in decisions:
            self.standing.by_reason[decision.reason] = self.standing.by_reason.get(decision.reason, 0) + 1
            if decision.action == TURN_ON:
                self.standing.switched_on += 1
                self._asked_on_at[decision.part_id] = self.standing.plans
                # A shed part stays remembered until it is seen running, so an
                # on the actuator failed to flip is asked again rather than
                # quietly dropped -- and a part that goes on and off repeatedly
                # is held by its flap report, not by being forgotten.
                self._plans_absent[decision.part_id] = 0
            else:
                self.standing.switched_off += 1
                # Written after the sort, so what is remembered is what the plan
                # actually asks for rather than what it considered.
                self._switched_off[decision.part_id] = decision.reason
                self._seen_gone.discard(decision.part_id)
        self.standing.held += len(held)
        self.standing.off_until_the_pressure_clears = len(self._switched_off)
        return SwitchPlan(tuple(decisions), tuple(sorted(held)), None, self._now_ns())

    def _one_hog_per_measurement(self, decisions, inputs) -> list[SwitchDecision]:
        """Shed the worst hog, not every hog the same reading named.

        Fair share is one part's equal slice among the parts running, so at 327
        parts every part doing real work is over three times it the moment the
        machine is contended: the reading that named one hog named sixteen, and
        gate-actuator flipped them two seconds apart on that one measurement
        while the metering it came from was already a minute old (2026-08-25
        21:08:20 to 21:08:50). Shedding the worst one and re-measuring is what
        makes the next decision answer the machine as it is rather than as it
        was; the ones not shed are not forgiven, they are simply not decided yet.
        """
        hogs = [decision for decision in decisions if decision.reason == OFF_REASONS[1]]
        if len(hogs) <= 1:
            return decisions
        worst = max(hogs, key=lambda d: (self._times_fair_share(d.part_id, inputs), d.part_id))
        deferred = [decision for decision in hogs if decision.part_id != worst.part_id]
        self.standing.hogs_deferred += len(deferred)
        return [decision for decision in decisions if decision not in deferred]

    def _times_fair_share(self, part_id, inputs) -> float:
        """How far over its share the hog reports put this part, worst resource first."""
        return max(
            (report.times_fair_share for report in inputs.hog_reports if report.part_id == part_id),
            default=0.0,
        )

    def _sheds_whose_condition_has_passed(self, inputs) -> tuple[str, ...]:
        """Parts this planner switched off whose reason no longer reads true.

        The reason is re-evaluated, never remembered as a verdict. What is
        re-read is the machine-level condition behind it, because the part-level
        one is unobservable while the part is off: an off part publishes no
        usage, so no hog report can name it and no forecast can call it the
        fastest grower. The evidence is deliberately the same on both sides --
        a part is shed on a hog report and comes back when the reports stop,
        which is what hog-detector publishing only under contention already
        means.
        """
        cleared = []
        for part_id, reason in sorted(self._switched_off.items()):
            if part_id in inputs.running_parts or part_id not in self._seen_gone:
                # Still stopping is not yet off, and asking for a part back
                # while it is still letting go is how a flap is manufactured.
                continue
            if not self._off_condition_still_holds(part_id, reason, inputs):
                cleared.append(part_id)
        return tuple(cleared)

    def _off_condition_still_holds(self, part_id, reason, inputs) -> bool:
        if reason == OFF_REASONS[0]:
            forecast = inputs.memory_forecast
            return (
                forecast is not None
                and forecast.seconds_to_exhaustion is not None
                and forecast.seconds_to_exhaustion <= self._memory_warning
            )
        if reason == OFF_REASONS[1]:
            return bool(inputs.hog_reports)
        if reason == OFF_REASONS[2]:
            pressure = inputs.io_pressure
            return (
                pressure is not None
                and pressure.is_measured
                and pressure.full_stalled_10s is not None
                and pressure.full_stalled_10s >= self._io_stall
            )
        if reason == OFF_REASONS[3]:
            duty = inputs.duty_cycles.get(part_id)
            return (
                duty is not None
                and inputs.current_hour is not None
                and inputs.current_hour not in duty.allowed_hours
            )
        if reason == OFF_REASONS[4]:
            plan = inputs.conservation_plan
            return plan is not None and part_id in plan.parts_to_stop
        # A reason this planner cannot re-read is a reason it cannot say has
        # passed. Held off, and visible in off_until_the_pressure_clears.
        return True

    def _off_reason(self, part_id, inputs, reserved) -> str | None:
        if self._priority(part_id, inputs) <= self._never_off_ceiling:
            # The control path is not shed, whatever the pressure. A governor
            # that switches off its own instruments decides the next question
            # blind: on 2026-08-25 it shed memory-pressure-forecaster,
            # duty-cycle-planner, part-restart-budgeter, failing-part-detector
            # and probe-runner, and every level they publish then read as the
            # last value they managed to send. Importance rank is the axis this
            # block already loses parts by, so it is the axis this floor sits on.
            return None
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
        plan = inputs.conservation_plan
        if plan is not None and part_id in plan.parts_to_stop and part_id not in reserved:
            # Never a part the plan itself names as one that must keep running:
            # the plan is ordered so nothing protecting capital is in the list,
            # and a reserved part is the governor's own floor under that.
            return OFF_REASONS[4]
        return None

    def _candidates_to_start(self, inputs, cleared: tuple[str, ...]) -> tuple[str, ...]:
        """Who might be switched on: explicit asks, reserved parts that vanished,
        and the parts this planner shed whose reason has since passed.

        The three sources carry different evidence. An admitted part, a restart
        request, a replacement or a conservation plan is an explicit ask --
        something decided this part should run. A reservation is not an ask; it
        is a standing floor, and using it to start a part is reconciliation:
        "this part should be running and is not". That reading is only sound for
        a part this planner has itself seen running -- a reserved part it has
        never seen is indistinguishable from one still starting, and the first
        plans after every spine boot were switching "on" whichever reserved part
        was slowest to its first metering sweep (symbol-catalogue-reader,
        fetching two venues' catalogues, at 13:00:16 on 2026-08-24).

        The third is this planner reconsidering its own decision, and it is the
        only one that closes the loop: without it every off is permanent, which
        is what an unattended eight hours proved on 2026-08-25.
        """
        # Part ids, not the objects that name them: a restart request and an
        # admitted part each carry a part_id, and putting the objects themselves
        # in this list made the plan sort RestartRequests against each other --
        # which raises, because nothing says which of two requests is smaller.
        candidates = [part_id_of(item) for item in inputs.admitted_parts]
        candidates += [part_id_of(item) for item in inputs.restart_requests]
        candidates += [
            r.part_id
            for r in inputs.reservations
            if r.state == "honoured" and r.part_id in self._ever_seen_running
        ]
        if inputs.replacement_plan is not None:
            # The part being replaced is the one to start: a replacement plan is
            # how a faulty part is swapped without a gap in between.
            candidates.append(inputs.replacement_plan.part_id)
        candidates += list(cleared)
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

    def _on_reason(self, part_id, reserved, cleared) -> str:
        if part_id in reserved:
            return ON_REASONS[0]
        # A part this planner shed is not being admitted; the room it was shed
        # for is back. Naming that separately is what makes the return path
        # legible in the switch journal rather than looking like an admission.
        return ON_REASONS[2] if part_id in cleared else ON_REASONS[1]

    def _priority(self, part_id, inputs) -> int:
        return int(inputs.priorities.get(part_id, 50))


def describe_planning(planner: SwitchingPlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans": planner.standing.plans,
        "refusals": planner.standing.refusals,
        "switched_on": planner.standing.switched_on,
        "switched_off": planner.standing.switched_off,
        "restored": planner.standing.restored,
        "off_until_the_pressure_clears": planner.standing.off_until_the_pressure_clears,
        "hogs_deferred": planner.standing.hogs_deferred,
        "sheds_confirmed_gone": planner.standing.sheds_confirmed_gone,
        "sheds_still_stopping": planner.standing.sheds_still_stopping,
        "conservation_plan_names": planner.standing.conservation_plan_names,
        "hog_reports_read": planner.standing.hog_reports_read,
        "conditions_that_have_passed": planner.standing.conditions_that_have_passed,
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
    # Bounded, because LatestByKey holds a key forever without it and "what is
    # running is what is reporting its own usage" then includes every part this
    # governor has ever switched off. Measured 2026-08-26 05:11: all 214 shed
    # parts were still in running_parts, so none could be observed gone and none
    # could come back.
    usages = LatestByKey(
        read=context.bus.reader("part-resource-usage"),
        key_of=lambda payload: payload.part_id,
        maximum_age_seconds=context.number("part_usage_reading_maximum_age_seconds"),
    )
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
            replacement_plan=replacement_plan.value(),
            conservation_plan=conservation_plan.value(),
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
            never_switched_off_priority_ceiling=int(
                context.number("never_switched_off_priority_ceiling")
            ),
            # How long to wait before deciding an on did not take. Derived rather
            # than set: until a reading could have gone stale, "not in the
            # metering" and "not started" are the same observation.
            plans_between_on_retries=max(
                1,
                round(
                    context.number("part_usage_reading_maximum_age_seconds")
                    / context.number("switch_plan_cadence_seconds")
                ),
            ),
        ),
        control_socket=context.control_socket,
        read_inputs=read_inputs,
        publish_plan=lambda plan: publish_plan([plan]),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        # Woken by fourteen input streams, this planned thirteen times a second
        # while the metering it reads updates once a second -- 2,802 plans in
        # four minutes, 218 of them lost in gate-actuator's receive buffer
        # (2026-08-24). A plan between two metering sweeps is computed from the
        # same numbers as the last, so the cadence is held as a tick floor:
        # inputs queue, nothing is lost, and the switch stays answerable.
        tick_floor_seconds=max(
            context.tick_floor_seconds, context.number("switch_plan_cadence_seconds")
        ),
        emit_health=context.emit_health,
    )
