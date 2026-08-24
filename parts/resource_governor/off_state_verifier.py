"""off-state-verifier: confirm a part switched off actually released its CPU and RAM.

T-3 says off means genuinely off. Nothing else in this block checks that the
switch did what it claimed, so without this a governor could switch parts off all
day while the machine stayed exactly as full.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "off-state-verifier"

PART_DECLARATION = PartDeclaration(
    part_id="off-state-verifier",
    consumes=("switch-record", "part-resource-usage"),
    produces=("part-fault", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RELEASED = "released"
STILL_HOLDING = "still-holding"
UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class PartFault:
    """A part that was switched off and did not let go."""

    part_id: str
    verdict: str
    memory_bytes_still_held: int | None
    cpu_still_used: float | None
    seconds_since_switch: float
    reason: str
    observed_at_ns: int


@dataclass
class VerifierStanding:
    verified: int = 0
    faults: int = 0
    unverifiable: int = 0
    pending: int = 0
    by_part: dict = field(default_factory=dict)


class OffStateVerifier:
    """Watches a switched-off part until it stops costing anything, or faults it.

    The grace period is the point: a part is SIGKILLed and its cgroup does not
    empty instantly, so checking immediately would fault every healthy switch.
    Checking never would make T-3 a claim nobody tests.
    """

    def __init__(
        self,
        grace_seconds: float,
        released_memory_bytes: int,
        released_cpu_seconds_per_second: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._grace = grace_seconds
        self._released_memory = released_memory_bytes
        self._released_cpu = released_cpu_seconds_per_second
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._switched_off: dict[str, float] = {}
        self.standing = VerifierStanding()

    def observe_switch_record(self, record) -> None:
        if record.action == "off" and record.outcome == "flipped":
            self._switched_off[record.part_id] = self._monotonic()
            self.standing.pending = len(self._switched_off)

    def verify(self, usages) -> tuple[PartFault, ...]:
        now = self._monotonic()
        by_part = {usage.part_id: usage for usage in usages}
        faults = []

        for part_id, switched_at in list(self._switched_off.items()):
            elapsed = now - switched_at
            if elapsed < self._grace:
                continue
            usage = by_part.get(part_id)
            if usage is None:
                # Gone from the usage report entirely is the strongest evidence
                # of release there is: the cgroup no longer exists.
                del self._switched_off[part_id]
                self.standing.verified += 1
                continue
            if not usage.is_measured:
                del self._switched_off[part_id]
                self.standing.unverifiable += 1
                faults.append(
                    self._fault(part_id, UNVERIFIABLE, None, None, elapsed, usage.unreadable_reason or "usage unreadable")
                )
                continue

            memory = usage.memory_current_bytes or 0
            cpu = usage.cpu_seconds_per_second or 0.0
            if memory <= self._released_memory and cpu <= self._released_cpu:
                del self._switched_off[part_id]
                self.standing.verified += 1
                continue

            del self._switched_off[part_id]
            self.standing.faults += 1
            self.standing.by_part[part_id] = self.standing.by_part.get(part_id, 0) + 1
            faults.append(
                self._fault(
                    part_id, STILL_HOLDING, memory, cpu, elapsed,
                    f"still holding {memory} bytes and {cpu:.3f} CPU {elapsed:.1f}s after being switched off",
                )
            )

        self.standing.pending = len(self._switched_off)
        return tuple(faults)

    def _fault(self, part_id, verdict, memory, cpu, elapsed, reason) -> PartFault:
        return PartFault(
            part_id=part_id,
            verdict=verdict,
            memory_bytes_still_held=memory,
            cpu_still_used=cpu,
            seconds_since_switch=elapsed,
            reason=reason,
            observed_at_ns=self._now_ns(),
        )


def describe_verification(verifier: OffStateVerifier) -> dict:
    return {
        "part_id": PART_ID,
        "verified": verifier.standing.verified,
        "faults": verifier.standing.faults,
        "unverifiable": verifier.standing.unverifiable,
        "pending": verifier.standing.pending,
        "by_part": dict(verifier.standing.by_part),
    }


def run_off_state_verifier(
    verifier: OffStateVerifier, control_socket, read_records_and_usage, publish_faults,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        records, usages = read_records_and_usage()
        for record in records:
            verifier.observe_switch_record(record)
        publish_faults(verifier.verify(usages))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_verification(verifier),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A switch record is an event -- a part was switched, once -- and resource usage
    is a level, the current reading per part. Verifying is what happens between the
    two: the record starts a clock, and the usage read after the grace period says
    whether the part let go.

    The grace period is the measured one. With all 321 parts switched off at once,
    942 MB of 1988 MB had come back after two seconds and all of it after twelve, so
    a verifier reading immediately would report a leak that is not there -- and a
    fault nobody can act on is worse than no measurement (Rule 8).
    """
    from runtime.input_assembly import Batch, LatestByKey

    records = Batch(read=context.bus.reader("switch-record"))
    usages = LatestByKey(
        read=context.bus.reader("part-resource-usage"), key_of=lambda usage: usage.part_id
    )
    publish_faults = context.bus.publisher_for("part-fault")

    def read_records_and_usage():
        return records.payloads(), usages.values()

    return run_off_state_verifier(
        verifier=OffStateVerifier(
            grace_seconds=context.number("off_state_verify_delay"),
            released_memory_bytes=int(context.number("released_memory_bytes")),
            released_cpu_seconds_per_second=context.number("released_cpu_seconds_per_second"),
        ),
        control_socket=context.control_socket,
        read_records_and_usage=read_records_and_usage,
        publish_faults=publish_faults,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
