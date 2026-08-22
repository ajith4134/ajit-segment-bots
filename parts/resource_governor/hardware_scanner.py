"""hardware-scanner: what the machine has, and what is free."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from runtime.hardware_facts import HardwareFacts, measure_hardware_facts
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hardware-scanner"

PART_DECLARATION = PartDeclaration(
    part_id="hardware-scanner",
    consumes=(),
    produces=("hardware-capacity", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The facts every planner needs. A reading missing one of these is published as
# incomplete rather than as a smaller machine: None means unmeasurable, and a
# planner is required to refuse rather than substitute (spec section 9).
REQUIRED_FACTS = ("physical_cores", "logical_cpus", "numa_nodes")


@dataclass(frozen=True)
class HardwareCapacity:
    """One reading, and whether it is complete enough to plan against."""

    facts: HardwareFacts
    unmeasurable: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.unmeasurable

    def as_dict(self) -> dict:
        return {**asdict(self.facts), "unmeasurable": list(self.unmeasurable)}


class HardwareScanner:
    """Wraps the substrate's own measurement and reports what it could not read."""

    def __init__(self, measure=measure_hardware_facts) -> None:
        self._measure = measure
        self.readings = 0
        self.last: HardwareCapacity | None = None

    def scan(self) -> HardwareCapacity:
        facts = self._measure()
        unmeasurable = tuple(name for name in REQUIRED_FACTS if getattr(facts, name) is None)
        self.readings += 1
        self.last = HardwareCapacity(facts=facts, unmeasurable=unmeasurable)
        return self.last


def describe_capacity(scanner: HardwareScanner) -> dict:
    return {
        "part_id": PART_ID,
        "readings": scanner.readings,
        "capacity": scanner.last.as_dict() if scanner.last else None,
        "is_complete": scanner.last.is_complete if scanner.last else None,
    }


def run_hardware_scanner(
    scanner: HardwareScanner, control_socket, publish_capacity,
    health_interval_seconds: float, emit_health,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_capacity(scanner.scan()),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
