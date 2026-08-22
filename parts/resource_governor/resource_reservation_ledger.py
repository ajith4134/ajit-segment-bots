"""resource-reservation-ledger: a guaranteed floor for parts that must never starve."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "resource-reservation-ledger"

PART_DECLARATION = PartDeclaration(
    part_id="resource-reservation-ledger",
    consumes=("part-priority", "hardware-capacity"),
    produces=("resource-reservation", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HONOURED = "honoured"
OVERCOMMITTED = "overcommitted"


@dataclass(frozen=True)
class ResourceReservation:
    part_id: str
    cpu_cores: float
    memory_bytes: int
    priority: int
    state: str
    reason: str
    reserved_at_ns: int


@dataclass
class LedgerStanding:
    reservations: int = 0
    overcommitted: int = 0
    cpu_reserved: float = 0.0
    memory_reserved: int = 0
    refused: dict = field(default_factory=dict)


class ResourceReservationLedger:
    """Holds a floor per part, and refuses to promise more than the machine has.

    Refusal is by priority, lowest rank first, so an overcommitted machine loses
    its least important guarantee rather than an arbitrary one. A reservation
    that cannot be honoured is marked overcommitted and kept visible instead of
    being dropped -- a floor that silently vanished is worse than one that was
    never granted, because the part still believes it has one.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._requested: dict[str, tuple[float, int, int]] = {}
        self.standing = LedgerStanding()

    def reserve(self, part_id: str, cpu_cores: float, memory_bytes: int, priority: int) -> None:
        self._requested[part_id] = (cpu_cores, memory_bytes, priority)

    def release(self, part_id: str) -> None:
        self._requested.pop(part_id, None)

    def settle(self, capacity) -> tuple[ResourceReservation, ...]:
        cores = capacity.facts.logical_cpus
        memory = capacity.facts.total_ram_bytes
        if cores is None or not memory:
            return tuple(
                self._reservation(part_id, *values, OVERCOMMITTED, "machine capacity unmeasurable")
                for part_id, values in sorted(self._requested.items())
            )

        by_priority = sorted(self._requested.items(), key=lambda item: (item[1][2], item[0]))
        cpu_left, memory_left = float(cores), int(memory)
        settled = []
        self.standing.overcommitted = 0
        self.standing.refused = {}
        for part_id, (cpu, ram, priority) in by_priority:
            if cpu <= cpu_left and ram <= memory_left:
                cpu_left -= cpu
                memory_left -= ram
                settled.append(self._reservation(part_id, cpu, ram, priority, HONOURED, "floor held"))
            else:
                self.standing.overcommitted += 1
                self.standing.refused[part_id] = "machine has no room left at this rank"
                settled.append(
                    self._reservation(
                        part_id, cpu, ram, priority, OVERCOMMITTED,
                        f"needs {cpu} cores and {ram} bytes; {cpu_left:.2f} cores and {memory_left} bytes remain",
                    )
                )
        self.standing.reservations = len(settled)
        self.standing.cpu_reserved = float(cores) - cpu_left
        self.standing.memory_reserved = int(memory) - memory_left
        return tuple(settled)

    def _reservation(self, part_id, cpu, ram, priority, state, reason) -> ResourceReservation:
        return ResourceReservation(
            part_id=part_id,
            cpu_cores=cpu,
            memory_bytes=ram,
            priority=priority,
            state=state,
            reason=reason,
            reserved_at_ns=self._now_ns(),
        )


def describe_reservations(ledger: ResourceReservationLedger) -> dict:
    return {
        "part_id": PART_ID,
        "reservations": ledger.standing.reservations,
        "overcommitted": ledger.standing.overcommitted,
        "cpu_reserved": ledger.standing.cpu_reserved,
        "memory_reserved": ledger.standing.memory_reserved,
        "refused": dict(ledger.standing.refused),
    }


def run_resource_reservation_ledger(
    ledger: ResourceReservationLedger, control_socket, read_requests_and_capacity, publish_reservations,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        requests, capacity = read_requests_and_capacity()
        for part_id, cpu, memory, priority in requests:
            ledger.reserve(part_id, cpu, memory, priority)
        publish_reservations(ledger.settle(capacity))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
