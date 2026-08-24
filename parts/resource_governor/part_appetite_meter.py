"""part-appetite-meter: what each running part actually costs in CPU and RAM.

Read from the part's own cgroup, not from its RSS. A process's RSS does not
predict an OOM kill, because the page cache it dirtied is charged to the cgroup
and not to it -- which is exactly how phase 0's writers were killed.
"""

from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-appetite-meter"

PART_DECLARATION = PartDeclaration(
    part_id="part-appetite-meter",
    consumes=("part-health",),
    produces=("part-resource-usage", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class PartResourceUsage:
    """One part's measured cost, or an honest statement that it could not be read."""

    part_id: str
    cpu_seconds_per_second: float | None
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    measured_at_ns: int
    unreadable_reason: str | None = None

    @property
    def is_measured(self) -> bool:
        return self.unreadable_reason is None


@dataclass
class _Sample:
    cpu_microseconds: int
    at_monotonic: float


@dataclass
class MeterStanding:
    parts_measured: int = 0
    parts_unreadable: int = 0
    readings: int = 0
    unreadable: dict[str, str] = field(default_factory=dict)


class PartAppetiteMeter:
    """Reads cpu.stat and memory.current from each part's own cgroup.

    CPU is reported as seconds of CPU per second of wall clock, which is
    comparable across parts and across machines; a raw counter is not. The first
    reading of a part has no previous sample to difference against, so it reports
    None rather than a rate invented from one point.
    """

    def __init__(self, cgroup_root: pathlib.Path = CGROUP_ROOT, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._root = pathlib.Path(cgroup_root)
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._previous: dict[str, _Sample] = {}
        self.standing = MeterStanding()

    def measure(self, part_id: str, cgroup_directory: pathlib.Path) -> PartResourceUsage:
        self.standing.readings += 1
        directory = pathlib.Path(cgroup_directory)
        try:
            cpu_microseconds = self._read_cpu_microseconds(directory)
            memory_current = self._read_int(directory / "memory.current")
            memory_peak = self._read_int(directory / "memory.peak")
        except OSError as failure:
            self.standing.parts_unreadable += 1
            self.standing.unreadable[part_id] = f"{type(failure).__name__}: {failure}"
            return PartResourceUsage(
                part_id=part_id,
                cpu_seconds_per_second=None,
                memory_current_bytes=None,
                memory_peak_bytes=None,
                measured_at_ns=self._now_ns(),
                unreadable_reason=self.standing.unreadable[part_id],
            )

        now = self._monotonic()
        previous = self._previous.get(part_id)
        self._previous[part_id] = _Sample(cpu_microseconds, now)
        rate = None
        if previous is not None and now > previous.at_monotonic:
            rate = (
                (cpu_microseconds - previous.cpu_microseconds)
                / MICROSECONDS_PER_SECOND
                / (now - previous.at_monotonic)
            )
        self.standing.parts_measured += 1
        self.standing.unreadable.pop(part_id, None)
        return PartResourceUsage(
            part_id=part_id,
            cpu_seconds_per_second=rate,
            memory_current_bytes=memory_current,
            memory_peak_bytes=memory_peak,
            measured_at_ns=self._now_ns(),
        )

    def _read_cpu_microseconds(self, directory: pathlib.Path) -> int:
        for line in (directory / "cpu.stat").read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == "usage_usec":
                return int(value)
        raise OSError(f"{directory}/cpu.stat has no usage_usec line")

    def _read_int(self, path: pathlib.Path) -> int | None:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None


def describe_appetite(meter: PartAppetiteMeter) -> dict:
    return {
        "part_id": PART_ID,
        "readings": meter.standing.readings,
        "parts_measured": meter.standing.parts_measured,
        "parts_unreadable": meter.standing.parts_unreadable,
        "unreadable": dict(meter.standing.unreadable),
    }


def run_part_appetite_meter(
    meter: PartAppetiteMeter, control_socket, read_running_parts, publish_usage,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_usage(
            tuple(meter.measure(part_id, directory) for part_id, directory in read_running_parts())
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_appetite(meter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Which parts are running is answered twice over, and both answers are required:
    a part is a candidate because it published health -- the input this part
    declares -- and it is measured only if its scope directory still exists, which
    the kernel decides. Health alone would keep measuring a part that has since
    exited, and a staleness threshold to prevent that would be a number with no
    provenance. The scope disappearing is the measurement.

    The scope of a part is a sibling of this part's own, because the launcher places
    every part in `<part-id>.scope` under the same slice. That is a fact about the
    substrate this part runs on, not knowledge of another part (T-4): it never
    learns a part id from anywhere but the messages it was sent.
    """
    from runtime.hardware_facts import read_own_cgroup_directory
    from runtime.input_assembly import LatestByKey

    health = LatestByKey(read=context.bus.reader("part-health"), key_of=lambda report: report.part_id)
    publish_usage = context.bus.publisher_for("part-resource-usage")
    sibling_scopes = read_own_cgroup_directory().parent

    def read_running_parts():
        running = []
        for part_id in sorted(health.mapping()):
            scope = sibling_scopes / f"{part_id}.scope"
            if scope.is_dir():
                running.append((part_id, scope))
            else:
                health.forget(part_id)
        return tuple(running)

    return run_part_appetite_meter(
        meter=PartAppetiteMeter(),
        control_socket=context.control_socket,
        read_running_parts=read_running_parts,
        publish_usage=publish_usage,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
