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
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_records(actuator.apply(read_plan())),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
