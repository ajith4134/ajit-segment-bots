"""hog-detector: a part taking far more than its share while another waits."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hog-detector"

PART_DECLARATION = PartDeclaration(
    part_id="hog-detector",
    consumes=("part-resource-usage", "hardware-capacity"),
    produces=("hog-report", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CPU = "cpu"
MEMORY = "memory"


@dataclass(frozen=True)
class HogReport:
    """One part named as taking more than its share, and by how much."""

    part_id: str
    resource: str
    share: float
    fair_share: float
    times_fair_share: float
    contended: bool
    observed_at_ns: int


@dataclass
class HogStanding:
    reports: int = 0
    parts_seen: int = 0
    uncontended_skips: int = 0
    unmeasured_parts: int = 0
    by_part: dict[str, int] = field(default_factory=dict)


class HogDetector:
    """Names a part over its fair share, but only while the machine is contended.

    Contention is the whole point: a part using nine tenths of an idle machine is
    using what nobody wanted. Naming it would train the governor to switch off
    work that costs nothing, and the switch itself is never free.

    Fair share is per running part rather than a fixed number, so it falls as
    more parts run and no threshold has to be retuned.
    """

    def __init__(
        self,
        hog_multiple_of_fair_share: float,
        cpu_contention_fraction: float,
        memory_contention_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        self._multiple = hog_multiple_of_fair_share
        self._cpu_contention = cpu_contention_fraction
        self._memory_contention = memory_contention_fraction
        self._now_ns = now_ns
        self.standing = HogStanding()

    def detect(self, usages, capacity) -> tuple[HogReport, ...]:
        measured = [usage for usage in usages if usage.is_measured]
        self.standing.unmeasured_parts += len(usages) - len(measured)
        self.standing.parts_seen = len(measured)
        if not measured:
            return ()

        reports = []
        reports.extend(self._detect_cpu(measured, capacity))
        reports.extend(self._detect_memory(measured, capacity))
        for report in reports:
            self.standing.reports += 1
            self.standing.by_part[report.part_id] = self.standing.by_part.get(report.part_id, 0) + 1
        return tuple(reports)

    def _detect_cpu(self, usages, capacity) -> list[HogReport]:
        cores = capacity.facts.logical_cpus
        rates = [(u, u.cpu_seconds_per_second) for u in usages if u.cpu_seconds_per_second is not None]
        if cores is None or not rates:
            return []
        used = sum(rate for _, rate in rates)
        contended = used / cores >= self._cpu_contention
        if not contended:
            self.standing.uncontended_skips += 1
            return []
        fair = used / len(rates)
        return [
            self._report(usage, CPU, rate, fair, contended)
            for usage, rate in rates
            if fair > 0 and rate >= fair * self._multiple
        ]

    def _detect_memory(self, usages, capacity) -> list[HogReport]:
        total = capacity.facts.total_ram_bytes
        sized = [(u, u.memory_current_bytes) for u in usages if u.memory_current_bytes is not None]
        if not total or not sized:
            return []
        used = sum(size for _, size in sized)
        contended = used / total >= self._memory_contention
        if not contended:
            self.standing.uncontended_skips += 1
            return []
        fair = used / len(sized)
        return [
            self._report(usage, MEMORY, float(size), fair, contended)
            for usage, size in sized
            if fair > 0 and size >= fair * self._multiple
        ]

    def _report(self, usage, resource, share, fair, contended) -> HogReport:
        return HogReport(
            part_id=usage.part_id,
            resource=resource,
            share=share,
            fair_share=fair,
            times_fair_share=share / fair,
            contended=contended,
            observed_at_ns=self._now_ns(),
        )


def describe_hogs(detector: HogDetector) -> dict:
    return {
        "part_id": PART_ID,
        "reports": detector.standing.reports,
        "parts_seen": detector.standing.parts_seen,
        "uncontended_skips": detector.standing.uncontended_skips,
        "unmeasured_parts": detector.standing.unmeasured_parts,
        "by_part": dict(detector.standing.by_part),
    }


def run_hog_detector(
    detector: HogDetector, control_socket, read_usage_and_capacity, publish_reports,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        usages, capacity = read_usage_and_capacity()
        publish_reports(detector.detect(usages, capacity))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Usage is the latest per part from part-appetite-meter; capacity the latest
    from hardware-scanner. Without a capacity reading there is no fair share to
    measure against, and no report is made rather than one against a guess.
    """
    from runtime.input_assembly import LatestByKey, LatestValue

    usages = LatestByKey(read=context.bus.reader("part-resource-usage"), key_of=lambda u: u.part_id)
    capacity = LatestValue(read=context.bus.reader("hardware-capacity"))
    publish_reports = context.bus.publisher_for("hog-report")
    detector = HogDetector(
        hog_multiple_of_fair_share=context.number("hog_multiple_of_fair_share"),
        cpu_contention_fraction=context.number("cpu_contention_fraction"),
        memory_contention_fraction=context.number("memory_contention_fraction"),
    )

    def tick() -> None:
        current = capacity.value()
        seen = tuple(usages.mapping().values())
        if current is None or not seen:
            return
        reports = detector.detect(seen, current)
        if reports:
            publish_reports(reports)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
