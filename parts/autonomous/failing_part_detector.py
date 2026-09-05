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
from runtime.level_publishing import describe_level_publishing
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
# The resource classes the perfect-run rule means something for. Its own
# reasoning is "in a system that talks to venues, zero errors over a long run
# means errors are being swallowed rather than not happening" -- which is a
# statement about parts that talk to something able to fail. A compute-bound
# part has nothing to swallow an error from, so zero errors is what working
# looks like, not evidence of anything. Applied to all three classes the rule
# was true of 229 parts permanently: 16,877 of the 48,084 escalations on
# 2026-09-04 were this fault restated about parts that cannot have the problem
# it describes, and a real fault had to be found inside that.
CAN_SWALLOW_AN_ERROR = ("io-bound", "bandwidth-bound")

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
        # What each part's work is made of, as its own declaration states it.
        # Absent until that part's first health arrives, and a part whose class
        # is unknown is not judged by the perfect-run rule: guessing would put
        # back exactly the false positives the rule's scope exists to remove.
        self._resource_class: dict[str, str] = {}
        self._crashed: dict[str, str] = {}
        self._first_seen: dict[str, int] = {}
        self.standing = DetectorStanding()

    def observe_health(
        self, part_id: str, tick_seconds: float, produced: int, errors: int,
        output_digest: str | None = None, resource_class: str = "",
    ) -> None:
        if part_id not in self._ticks:
            self.standing.parts_watched += 1
            self._first_seen[part_id] = self._now_ns()
        self._ticks[part_id] = self._ticks.get(part_id, 0) + 1
        self._produced[part_id] = self._produced.get(part_id, 0) + produced
        self._errors[part_id] = self._errors.get(part_id, 0) + errors
        if resource_class:
            self._resource_class[part_id] = resource_class
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
                # Mean, not median: tried the median on 2026-09-05 on the theory
                # that parts pacing their work with `is_due()` have a bimodal tick
                # time a half-window mean swings on. Measured at matched maturity
                # it made no difference -- 10,569 faults at 1,182 ticks per part
                # with the mean, 11,123 at 1,157 with the median -- so the theory
                # is wrong and the mean stays. Why this fires on ~3% of all checks
                # is still unexplained, and it is now the whole of the warden's
                # remaining escalation volume.
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
        # means errors are being swallowed rather than not happening. Only asked of
        # a part that talks to something able to fail -- see CAN_SWALLOW_AN_ERROR.
        if (
            ticks >= self._perfect_run_ticks
            and self._errors.get(part_id, 0) == 0
            and self._resource_class.get(part_id) in CAN_SWALLOW_AN_ERROR
        ):
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


def verdict_of(faults) -> tuple:
    """What makes a fault a different fault, with the noticing left out.

    A PartFault carries three fields that restate when the detector last looked
    rather than what it found: `detected_at_ns`, `observations`, and a `detail`
    whose text is "5 tick(s) with no error of any kind" -- a number that climbs
    on every tick. Compared whole, two reports of one unchanging verdict are
    never equal, so nothing would ever be skipped and the storm would survive the
    fix while the skip counter claimed otherwise.

    What a reader acts on is which part, what is wrong with it, and how badly. A
    change in any of those is published at once; a fault that has merely been
    true for longer waits for the refresh interval, which is the correct reading
    of it -- "still suspect" is not news.
    """
    return tuple(
        (fault.part_id, fault.kind, fault.severity, fault.is_silent) for fault in faults
    )


def describe_detection(detector: FailingPartDetector, levels=None) -> dict:
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
        **(describe_level_publishing({"part-fault": levels}) if levels else {}),
    }


def run_failing_part_detector(
    detector: FailingPartDetector, control_socket, read_health, publish_faults,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    clear_fault=None,
    levels=None,
) -> int:
    """Judge the parts this tick heard from, and say only what changed.

    Two things this loop used to do on every tick, and the cost of each measured
    on the live spine at 10:14 on 2026-08-26:

    **It re-checked every part it had ever seen.** 327 parts against roughly 18
    ticks a second is 5,967 checks a second, and 5,966 of every 5,967 were of a
    part about which nothing new had arrived. A part's verdict is computed from
    its tick count, its durations and its error count, and all three move only
    when that part's health arrives -- so a part nobody heard from cannot have
    changed its mind, and checking it can only produce the answer it produced
    last time. Judging what arrived is the same verdict for a hundredth of the
    work.

    **It republished the verdict every time.** SUSPICIOUSLY_PERFECT is true of
    almost every part in this system and stays true -- errors=0 over a long run
    is the normal condition of a part that is working -- so the detector was
    putting 5,315 identical faults a second onto the bus. The warden escalated
    each one, the restart budgeter republished every budget on each, and the
    result was 89,747 messages a second and seven parts too starved of CPU to
    send the heartbeat that would have proved them alive.

    `clear_fault` is how a part that recovers stops being reported: without it
    the level publisher would go on holding that part's last fault as the thing
    it most recently said, and a part that faulted again identically inside one
    refresh interval would be silently skipped.
    """
    def tick() -> None:
        judged = set()
        for report in read_health():
            detector.observe_health(**report)
            judged.add(report["part_id"])
        for part_id in judged:
            outcome = detector.check(part_id)
            if outcome.is_usable:
                publish_faults(outcome.fault)
            elif clear_fault is not None:
                clear_fault(part_id)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_detection(detector, levels),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A health report carries the part's identity, the seconds since its previous
    tick, what its inputs lost, and any control frame it refused -- and nothing
    else. So the mapping states exactly what is measurable from here: the tick
    gap feeds the slowdown baseline; a refused frame or newly lost input counts
    as an error; the report itself is the only production visible, so a part
    that reports at all is producing one thing. That leaves getting-slower and
    suspiciously-perfect reachable from live wiring, stopped-producing and
    stuck-on-one-answer dormant until an input carries real output counts, and
    crashed dormant until something carries a crash -- dormant is a state, not
    a gap papered over with invented numbers.
    """
    from runtime.input_assembly import Batch

    from runtime.level_publishing import LevelPublisherByKey

    health = Batch(read=context.bus.reader("part-health"))
    # A fault is a level, not an event: "this part is currently suspect" is true
    # until it stops being true, and saying it again changes nothing downstream.
    # Keyed by part so one part changing its verdict does not restate every other
    # part's -- see runtime/level_publishing.py for what that cost when measured.
    fault_levels = LevelPublisherByKey(
        publish=context.bus.publisher_for("part-fault"),
        # Its own refresh interval: the keepalive is per key and this part watches
        # 314 of them, which measured 997 messages a second on 2026-09-04 -- 10.8%
        # of the spine -- with the change check working and skipping the 16% whose
        # verdict had genuinely not moved. It must stay inside
        # warden_escalation_forget_seconds, because this part clears a fault by
        # dropping the key rather than publishing an all-clear, so a gap longer
        # than that bound would read as a fault clearing and coming back.
        refresh_interval_seconds=context.number("part_fault_refresh_interval_seconds"),
        identity_of=verdict_of,
    )

    detector = FailingPartDetector(
        window=int(context.number("detector_window")),
        minimum_ticks=int(context.number("detector_minimum_ticks")),
        stuck_answer_ticks=int(context.number("detector_stuck_answer_ticks")),
        slowdown_ratio=context.number("detector_slowdown_ratio"),
        perfect_run_ticks=int(context.number("detector_perfect_run_ticks")),
    )
    loss_seen: dict[str, int] = {}

    def read_health():
        reports = []
        for report in health.payloads():
            lost_total = sum(count for _kind, count in report.input_loss)
            newly_lost = max(0, lost_total - loss_seen.get(report.part_id, 0))
            loss_seen[report.part_id] = max(
                lost_total, loss_seen.get(report.part_id, 0)
            )
            reports.append(
                {
                    "part_id": report.part_id,
                    "tick_seconds": report.staleness_seconds,
                    "produced": 1,
                    "errors": newly_lost
                    + (1 if report.refused_control_frame else 0),
                    "output_digest": None,
                    "resource_class": report.resource_class,
                }
            )
        return tuple(reports)

    return run_failing_part_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_health=read_health,
        publish_faults=lambda fault: fault_levels.publish_level(fault.part_id, (fault,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        clear_fault=fault_levels.forget,
        levels=fault_levels,
    )
