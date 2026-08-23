"""failing-part-detector: which parts are broken, including the quiet ones.

A crashed part announces itself. The dangerous failures are the ones that keep
reporting healthy:

- **A part that has stopped producing** while its heartbeat continues. The loop is
  alive and the work is not, which is what a `while True` around a swallowed
  exception looks like from outside.
- **A part returning the same answer every time.** An estimator stuck on its prior,
  a reader replaying a cached response, a classifier whose input stopped changing.
  The output is well-formed and constant, and nothing downstream can tell.
- **A part getting slower every tick.** Usually an unbounded structure being walked;
  it works perfectly right up until it does not, and by then it has taken the CPU
  with it.
- **A part that is too healthy.** Zero errors over a long run in a system that talks
  to venues means the errors are being swallowed, not that they stopped happening.

Detection is per part and against that part's own history, never against a global
threshold: a part that ticks once an hour and one that ticks a thousand times a
second are both healthy, and one number cannot describe them.

Every fault names what would clear it, because a fault that cannot be cleared is a
permanent red light that people learn to ignore.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.autonomy_types import PartFault
from runtime.rolling_statistics import RollingWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "failing-part-detector"

PART_DECLARATION = PartDeclaration(
    part_id="failing-part-detector",
    consumes=("part-health",),
    produces=("part-fault", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HEALTHY = "healthy"
NOT_ENOUGH_HISTORY = "not-enough-history-to-judge-this-part"

# The failure kinds. Each is silent from the outside except the first.
CRASHED = "crashed"
STOPPED_PRODUCING = "alive-but-producing-nothing"
STUCK_ON_ONE_ANSWER = "producing-the-same-answer-every-time"
GETTING_SLOWER = "taking-longer-every-tick"
SUSPICIOUSLY_PERFECT = "reporting-no-error-at-all-over-a-long-run"

FAULT_KINDS = (
    CRASHED, STOPPED_PRODUCING, STUCK_ON_ONE_ANSWER, GETTING_SLOWER,
    SUSPICIOUSLY_PERFECT,
)

FATAL = "fatal"
STALLED = "stalled"
DEGRADED = "degraded"
SUSPECT = "suspect"


@dataclass(frozen=True)
class DetectionOutcome:
    part_id: str
    state: str
    fault: PartFault | None
    ticks_seen: int
    reason: str
    detected_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.fault is not None


@dataclass
class DetectorStanding:
    parts_watched: int = 0
    checks: int = 0
    faults_found: int = 0
    by_kind: dict = field(default_factory=dict)
    silent_faults_found: int = 0
    parts_with_too_little_history: int = 0


class FailingPartDetector:
    """Judges each part against its own history, and looks hardest at the quiet ones."""

    def __init__(
        self,
        window: int,
        minimum_ticks: int,
        stuck_answer_ticks: int,
        slowdown_ratio: float,
        perfect_run_ticks: int,
        now_ns=time.time_ns,
    ) -> None:
        if window < 3 or minimum_ticks < 3:
            raise ValueError(
                "a part is judged against its own history, and a history of two ticks "
                "describes nothing"
            )
        if stuck_answer_ticks < 2:
            raise ValueError("one repeated answer is not a stuck answer")
        if slowdown_ratio <= 1.0:
            raise ValueError(
                "the slowdown ratio is how many times its own baseline a part may take "
                "before it is getting slower"
            )
        if perfect_run_ticks < 2:
            raise ValueError(
                "zero errors over two ticks is a quiet moment, not swallowed errors"
            )
        self._window = window
        self._minimum_ticks = minimum_ticks
        self._stuck_answer_ticks = stuck_answer_ticks
        self._slowdown_ratio = slowdown_ratio
        self._perfect_run_ticks = perfect_run_ticks
        self._now_ns = now_ns
        self._durations: dict[str, RollingWindow] = {}
        self._outputs: dict[str, list] = {}
        self._produced: dict[str, int] = {}
        self._ticks: dict[str, int] = {}
        self._errors: dict[str, int] = {}
        self._crashed: dict[str, str] = {}
        self._first_seen: dict[str, int] = {}
        self.standing = DetectorStanding()

    def observe_health(
        self, part_id: str, tick_seconds: float, produced: int, errors: int,
        output_digest: str | None = None,
    ) -> None:
        if part_id not in self._ticks:
            self.standing.parts_watched += 1
            self._first_seen[part_id] = self._now_ns()
        self._ticks[part_id] = self._ticks.get(part_id, 0) + 1
        self._produced[part_id] = self._produced.get(part_id, 0) + produced
        self._errors[part_id] = self._errors.get(part_id, 0) + errors
        self._durations.setdefault(part_id, RollingWindow(self._window)).observe(tick_seconds)
        if output_digest is not None:
            digests = self._outputs.setdefault(part_id, [])
            digests.append(output_digest)
            if len(digests) > self._window:
                digests.pop(0)

    def observe_crash(self, part_id: str, detail: str) -> None:
        self._crashed[part_id] = detail

    def check(self, part_id: str) -> DetectionOutcome:
        self.standing.checks += 1
        ticks = self._ticks.get(part_id, 0)

        if part_id in self._crashed:
            return self._fault(
                part_id, CRASHED, FATAL, self._crashed[part_id], ticks, False,
                "restarting it, or replacing it if the restart does not hold",
            )

        if ticks < self._minimum_ticks:
            self.standing.parts_with_too_little_history += 1
            return self._outcome(
                part_id, NOT_ENOUGH_HISTORY, None, ticks,
                f"{ticks} tick(s) of history, below the {self._minimum_ticks} needed. "
                f"A part is judged against its own history, not a global threshold",
            )

        # Alive and producing nothing: the loop is running and the work is not.
        if self._produced.get(part_id, 0) == 0:
            return self._fault(
                part_id, STOPPED_PRODUCING, STALLED,
                f"{ticks} tick(s) and nothing produced", ticks, True,
                "the part producing anything at all",
            )

        digests = self._outputs.get(part_id, [])
        if (
            len(digests) >= self._stuck_answer_ticks
            and len(set(digests[-self._stuck_answer_ticks :])) == 1
        ):
            return self._fault(
                part_id, STUCK_ON_ONE_ANSWER, STALLED,
                f"the same answer for {self._stuck_answer_ticks} consecutive tick(s)",
                ticks, True,
                "the output changing, which usually means its input started changing again",
            )

        durations = self._durations.get(part_id)
        if durations is not None and durations.count >= self._minimum_ticks:
            recent = list(durations.values)[-max(self._minimum_ticks // 2, 2) :]
            earlier = list(durations.values)[: max(self._minimum_ticks // 2, 2)]
            if earlier and recent:
                baseline = statistics.mean(earlier)
                latest = statistics.mean(recent)
                if baseline > 0 and latest / baseline >= self._slowdown_ratio:
                    return self._fault(
                        part_id, GETTING_SLOWER, DEGRADED,
                        f"{latest / baseline:.1f}x its own baseline tick time", ticks,
                        True,
                        "the tick time returning to its baseline, usually after whatever "
                        "structure is growing is bounded",
                    )

        # Too healthy: in a system that talks to venues, zero errors over a long run
        # means errors are being swallowed rather than not happening.
        if ticks >= self._perfect_run_ticks and self._errors.get(part_id, 0) == 0:
            return self._fault(
                part_id, SUSPICIOUSLY_PERFECT, SUSPECT,
                f"{ticks} tick(s) with no error of any kind", ticks, True,
                "an error being reported, or a deliberate confirmation that this part "
                "genuinely cannot fail",
            )

        return self._outcome(
            part_id, HEALTHY, None, ticks,
            f"{ticks} tick(s), producing, varying, and not slowing against its own "
            f"baseline",
        )

    def _fault(self, part_id, kind, severity, detail, ticks, is_silent, cleared_by):
        self.standing.faults_found += 1
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1
        if is_silent:
            self.standing.silent_faults_found += 1
        fault = PartFault(
            part_id=part_id, kind=kind, detail=detail,
            first_seen_at_ns=self._first_seen.get(part_id, self._now_ns()),
            observations=ticks, is_silent=is_silent, severity=severity,
            reason=(
                f"{detail}. "
                + (
                    "This is silent from the outside: the part keeps reporting healthy. "
                    if is_silent
                    else ""
                )
                + f"It would be cleared by {cleared_by}"
            ),
            detected_at_ns=self._now_ns(),
        )
        return self._outcome(part_id, kind, fault, ticks, fault.reason)

    def _outcome(self, part_id, state, fault, ticks, reason) -> DetectionOutcome:
        return DetectionOutcome(
            part_id=part_id, state=state, fault=fault, ticks_seen=ticks, reason=reason,
            detected_at_ns=self._now_ns(),
        )


def describe_detection(detector: FailingPartDetector) -> dict:
    return {
        "part_id": PART_ID,
        "parts_watched": detector.standing.parts_watched,
        "checks": detector.standing.checks,
        "faults_found": detector.standing.faults_found,
        "by_kind": dict(detector.standing.by_kind),
        "silent_faults_found": detector.standing.silent_faults_found,
        "parts_with_too_little_history": (
            detector.standing.parts_with_too_little_history
        ),
        "fault_kinds": list(FAULT_KINDS),
        "uses_one_global_threshold": False,
        "raises_a_fault_that_nothing_can_clear": False,
    }


def run_failing_part_detector(
    detector: FailingPartDetector, control_socket, read_health, publish_faults,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for report in read_health():
            detector.observe_health(**report)
        for part_id in list(detector._ticks):
            outcome = detector.check(part_id)
            if outcome.is_usable:
                publish_faults(outcome.fault)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
