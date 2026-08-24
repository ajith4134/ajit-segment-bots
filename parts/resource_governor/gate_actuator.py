"""gate-actuator: flip the gates a switch plan names, one at a time, recording each.

The only part that touches a switch. It decides nothing -- the plan decides --
and it records every flip whether it worked or not, because a switch nobody
recorded is a state change nothing downstream can account for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "gate-actuator"

# What the launcher answers with when a switch actually happened. Anything else is
# a switch that was not made, and must not be recorded as one.
OUTCOME_FLIPPED_BY_THE_LAUNCHER = "flipped"


class SwitchWasNotMade(RuntimeError):
    """The launcher did not carry out a switch this part asked for."""

PART_DECLARATION = PartDeclaration(
    part_id="gate-actuator",
    consumes=("switch-plan",),
    produces=("switch-record", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FLIPPED = "flipped"
REFUSED = "refused"
FAILED = "failed"


@dataclass(frozen=True)
class SwitchRecord:
    part_id: str
    action: str
    outcome: str
    reason: str
    attempted_at_ns: int
    completed_at_ns: int | None


@dataclass
class ActuatorStanding:
    flipped: int = 0
    failed: int = 0
    refused: int = 0
    plans_refused: int = 0
    last_failure: str | None = None
    by_part: dict = field(default_factory=dict)


class GateActuator:
    """Applies one decision at a time, and never invents one.

    One at a time on purpose: switching several parts together means a machine
    that briefly holds both the old and new set, which is exactly the moment a
    memory-pressure plan was trying to avoid. Each flip is recorded before the
    next is attempted, so a crash mid-plan leaves a record of where it stopped.
    """

    def __init__(self, switch_part, now_ns=time.time_ns) -> None:
        self._switch_part = switch_part
        self._now_ns = now_ns
        self.standing = ActuatorStanding()

    def apply(self, plan) -> tuple[SwitchRecord, ...]:
        if not plan.is_plannable:
            self.standing.plans_refused += 1
            return ()

        records = []
        for decision in plan.decisions:
            attempted = self._now_ns()
            try:
                self._switch_part(decision.part_id, decision.action)
            except Exception as failure:
                self.standing.failed += 1
                self.standing.last_failure = f"{decision.part_id}: {type(failure).__name__}: {failure}"
                records.append(
                    SwitchRecord(
                        decision.part_id, decision.action, FAILED,
                        f"{type(failure).__name__}: {failure}", attempted, None,
                    )
                )
                continue
            self.standing.flipped += 1
            self.standing.by_part[decision.part_id] = self.standing.by_part.get(decision.part_id, 0) + 1
            records.append(
                SwitchRecord(
                    decision.part_id, decision.action, FLIPPED, decision.reason, attempted, self._now_ns()
                )
            )
        return tuple(records)


def describe_actuation(actuator: GateActuator) -> dict:
    return {
        "part_id": PART_ID,
        "flipped": actuator.standing.flipped,
        "failed": actuator.standing.failed,
        "plans_refused": actuator.standing.plans_refused,
        "last_failure": actuator.standing.last_failure,
        "by_part": dict(actuator.standing.by_part),
    }


def run_gate_actuator(
    actuator: GateActuator, control_socket, read_plan, publish_records,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        # No plan this tick is not an empty plan: an empty plan would be a
        # statement that nothing should change, and nobody made it.
        plan = read_plan()
        if plan is not None:
            publish_records(actuator.apply(plan))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_actuation(actuator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    This is the one part in the system that switches other parts, and T-2 is what
    makes that safe: it holds no descriptor onto any part's control socket, because
    it was never given one. What it was given is the launcher's request endpoint,
    handed only to the part the blueprint says turns a switch-plan into
    switch-records. Every other part is started with that address set to None.

    A plan is an event, not a level. Applying the last one again on every tick would
    re-switch parts that were already switched, so an empty plan -- not the previous
    one -- is what a tick with no new plan applies.
    """
    from runtime.input_assembly import Batch
    from runtime.switch_service import request_switch

    if not context.switch_endpoint:
        raise RuntimeError(
            f"'{context.part_id}' was started without a switch endpoint, so it cannot switch "
            f"anything. Only the part the blueprint names as the actuator is given one -- if that "
            f"is this part, the launcher was started without open_switch_service."
        )

    plans = Batch(read=context.bus.reader("switch-plan"))
    publish_records = context.bus.publisher_for("switch-record")
    request_timeout_seconds = context.number("switch_request_timeout")

    def switch_part(part_id: str, action: str) -> None:
        outcome = request_switch(
            address=context.switch_endpoint,
            part_id=part_id,
            action=action,
            reason=f"{context.part_id} acting on a switch plan",
            timeout_seconds=request_timeout_seconds,
        )
        if outcome.outcome != OUTCOME_FLIPPED_BY_THE_LAUNCHER:
            # Raised so apply() records it as a failed switch rather than a made one.
            # A switch the launcher refused is not a switch; recording it as one is
            # how a part that never stopped ends up shown as stopped.
            raise SwitchWasNotMade(f"{outcome.outcome}: {outcome.detail}")

    def read_plan():
        # The newest plan wins; an older one acted on after a newer one arrived
        # would switch parts the planner has since changed its mind about. None
        # when nothing arrived -- which, until 2026-08-23, built an empty plan
        # from a type this file never imported and ended the process on its
        # first tick, caught the first time the part was started in a test.
        applied = plans.payloads()
        return applied[-1] if applied else None

    return run_gate_actuator(
        actuator=GateActuator(switch_part=switch_part),
        control_socket=context.control_socket,
        read_plan=read_plan,
        publish_records=publish_records,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
