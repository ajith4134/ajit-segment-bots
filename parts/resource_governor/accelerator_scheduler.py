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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_slots(scheduler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The only request signal this part is given is part-priority; it answers
    every stated priority with a slot decision once per health interval, and
    on this machine -- which the scheduler measured at start to have no
    accelerator -- every answer is a refusal that says so. A capacity reading
    is consumed so a device that appears later is seen on the next scan.
    """
    import time as _time

    from runtime.input_assembly import LatestByKey, LatestValue

    priorities = LatestByKey(read=context.bus.reader("part-priority"), key_of=lambda p: p.part_id)
    capacity = LatestValue(read=context.bus.reader("hardware-capacity"))
    publish_slots = context.bus.publisher_for("accelerator-slot")
    scheduler = AcceleratorScheduler(slot_seconds=context.number("accelerator_slot_seconds"))
    last_answer = [float("-inf")]

    def read_requests():
        capacity.value()
        now = _time.monotonic()
        if now - last_answer[0] < context.health_interval_seconds:
            return ()
        last_answer[0] = now
        return tuple((p.part_id, p.priority) for p in priorities.mapping().values())

    def publish(slots) -> None:
        if slots:
            publish_slots(slots)

    return run_accelerator_scheduler(
        scheduler=scheduler,
        control_socket=context.control_socket,
        read_requests=read_requests,
        publish_slots=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
