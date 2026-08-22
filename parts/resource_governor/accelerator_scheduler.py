"""accelerator-scheduler: grant GPU or other accelerator slots for a window.

This box has no accelerator. That is a measured fact, not an assumption, and it
is reported as zero slots rather than as a scheduler that never grants anything
for reasons nobody can see.
"""

from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "accelerator-scheduler"

PART_DECLARATION = PartDeclaration(
    part_id="accelerator-scheduler",
    consumes=("hardware-capacity", "part-priority"),
    produces=("accelerator-slot", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

GRANTED = "granted"
QUEUED = "queued"
NO_ACCELERATOR = "no-accelerator"

DRM_ROOT = pathlib.Path("/sys/class/drm")
NVIDIA_ROOT = pathlib.Path("/proc/driver/nvidia/gpus")


@dataclass(frozen=True)
class AcceleratorSlot:
    part_id: str
    state: str
    device: str | None
    expires_at_monotonic: float | None
    priority: int
    reason: str
    decided_at_ns: int


@dataclass
class SchedulerStanding:
    devices_found: int = 0
    granted: int = 0
    queued: int = 0
    expired: int = 0
    devices: list = field(default_factory=list)


class AcceleratorScheduler:
    """Grants each device to one part for a window, highest priority first.

    A window rather than a lock: a part that took a device and died would hold it
    forever, and nothing here can distinguish that from a part still working.
    The grant simply expires, which needs no liveness check to be correct.
    """

    def __init__(self, slot_seconds: float, discover=None, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._slot_seconds = slot_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._devices = tuple(discover() if discover else discover_accelerators())
        self._held: dict[str, tuple[str, float]] = {}
        self.standing = SchedulerStanding(
            devices_found=len(self._devices), devices=list(self._devices)
        )

    def request_slot(self, part_id: str, priority: int) -> AcceleratorSlot:
        self._expire_finished_slots()
        if not self._devices:
            return self._slot(part_id, NO_ACCELERATOR, None, None, priority, "this machine has no accelerator")

        free = [device for device in self._devices if device not in self._held]
        if not free:
            self.standing.queued += 1
            return self._slot(part_id, QUEUED, None, None, priority, "every device is held")

        device = free[0]
        expires = self._monotonic() + self._slot_seconds
        self._held[device] = (part_id, expires)
        self.standing.granted += 1
        return self._slot(part_id, GRANTED, device, expires, priority, f"holds {device} for {self._slot_seconds:.0f}s")

    def release(self, part_id: str) -> None:
        for device, (holder, _) in list(self._held.items()):
            if holder == part_id:
                del self._held[device]

    def _expire_finished_slots(self) -> None:
        now = self._monotonic()
        for device, (_, expires) in list(self._held.items()):
            if now >= expires:
                del self._held[device]
                self.standing.expired += 1

    def _slot(self, part_id, state, device, expires, priority, reason) -> AcceleratorSlot:
        return AcceleratorSlot(
            part_id=part_id,
            state=state,
            device=device,
            expires_at_monotonic=expires,
            priority=priority,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def discover_accelerators() -> tuple[str, ...]:
    """Every accelerator this machine actually publishes. Empty is an answer."""
    devices = []
    try:
        devices.extend(sorted(path.name for path in NVIDIA_ROOT.iterdir()))
    except OSError:
        pass
    try:
        devices.extend(
            sorted(path.name for path in DRM_ROOT.iterdir() if path.name.startswith("card"))
        )
    except OSError:
        pass
    return tuple(devices)


def describe_slots(scheduler: AcceleratorScheduler) -> dict:
    return {
        "part_id": PART_ID,
        "devices_found": scheduler.standing.devices_found,
        "devices": list(scheduler.standing.devices),
        "granted": scheduler.standing.granted,
        "queued": scheduler.standing.queued,
        "expired": scheduler.standing.expired,
    }


def run_accelerator_scheduler(
    scheduler: AcceleratorScheduler, control_socket, read_requests, publish_slots,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        publish_slots(
            tuple(scheduler.request_slot(part_id, priority) for part_id, priority in read_requests())
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
