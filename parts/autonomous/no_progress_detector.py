"""no-progress-detector: the system is running and nothing is happening.

Every part can be individually healthy while the system as a whole accomplishes
nothing. Feeds arrive, scanners scan, bots form opinions, and no trade is ever
placed -- because one filter in the middle rejects everything and reports success
doing it. Nothing in a per-part health view can see this, because nothing is broken.

So this part watches for progress rather than for health, and progress means an
outcome the system exists to produce: a decision reaching a conclusion, a trade
being placed, a lesson being written, a model being refit. It is deliberately
separate from the failing-part detector because the two look for opposite things --
one asks whether a part is doing its job, this one asks whether the jobs add up to
anything.

Three distinctions it holds:

- **Quiet is not stalled.** A system with nothing to trade in a flat market is
  working correctly. So the detector needs a reason to expect progress -- inputs
  arriving, opportunities detected -- before absence of output means anything.
- **The stall is located.** "Nothing is happening" is useless; "candidates are
  produced and intents are not" names the gap between two stages, which is where the
  filter that rejects everything lives.
- **A slow stall counts.** Output falling by an order of magnitude over hours is the
  same failure as output stopping, arriving gradually enough that nobody notices.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import PartFault
from runtime.rolling_statistics import RollingWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "no-progress-detector"

PART_DECLARATION = PartDeclaration(
    part_id="no-progress-detector",
    consumes=("part-health", "journal-entry"),
    produces=("part-fault", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROGRESSING = "progressing"
NOTHING_EXPECTED = "there-was-nothing-for-the-system-to-do"
STALLED_AT_A_STAGE = "one-stage-consumes-and-produces-nothing"
SLOWING = "output-is-falling-without-input-falling"
NOT_ENOUGH_HISTORY = "not-enough-history-to-judge-progress"

STALLED_SEVERITY = "stalled"
DEGRADED_SEVERITY = "degraded"


@dataclass(frozen=True)
class ProgressOutcome:
    state: str
    fault: PartFault | None
    stalled_stage: str | None
    inputs_seen: int
    outputs_seen: int
    reason: str
    checked_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.fault is not None


@dataclass
class ProgressStanding:
    checks: int = 0
    stalls_found: int = 0
    slowdowns_found: int = 0
    quiet_periods: int = 0
    stages_watched: int = 0
    by_stage: dict = field(default_factory=dict)


class NoProgressDetector:
    """Watches whether the stages add up to an outcome, and names where they stop."""

    def __init__(
        self,
        stages,
        window: int,
        minimum_inputs: int,
        slowdown_ratio: float,
        now_ns=time.time_ns,
    ) -> None:
        """`stages` is the ordered pipeline: each name consumes the one before it."""
        stages = tuple(stages)
        if len(stages) < 2:
            raise ValueError(
                "locating a stall needs at least two stages: 'nothing is happening' is "
                "not an actionable finding"
            )
        if window < 2:
            raise ValueError("a rate needs more than one observation")
        if minimum_inputs < 1:
            raise ValueError(
                "absence of output means nothing without a reason to expect it: a system "
                "with nothing to trade in a flat market is working correctly"
            )
        if slowdown_ratio <= 1.0:
            raise ValueError(
                "the ratio is how far output may fall against its own history before a "
                "gradual stall counts as one"
            )
        self._stages = stages
        self._window = window
        self._minimum_inputs = minimum_inputs
        self._slowdown_ratio = slowdown_ratio
        self._now_ns = now_ns
        self._counts: dict[str, int] = {stage: 0 for stage in stages}
        self._history: dict[str, RollingWindow] = {
            stage: RollingWindow(window) for stage in stages
        }
        self.standing = ProgressStanding()
        self.standing.stages_watched = len(stages)

    def observe(self, stage: str, count: int) -> None:
        if stage not in self._counts:
            raise ValueError(
                f"{stage!r} is not one of the declared stages. A stall is located between "
                f"named stages, not in an anonymous middle"
            )
        self._counts[stage] += count

    def roll_period(self) -> None:
        """Ends the measurement period: rates are per period, not since start."""
        for stage in self._stages:
            self._history[stage].observe(float(self._counts[stage]))
            self._counts[stage] = 0

    def check(self) -> ProgressOutcome:
        self.standing.checks += 1
        first_stage = self._stages[0]
        inputs = self._counts[first_stage]

        if inputs < self._minimum_inputs:
            self.standing.quiet_periods += 1
            return self._outcome(
                NOTHING_EXPECTED, None, None, inputs, self._counts[self._stages[-1]],
                f"{inputs} input(s) this period, below the {self._minimum_inputs} that "
                f"would make an absence of output meaningful. Quiet is not stalled",
            )

        # Locate the stall: the first stage that consumed something and produced
        # nothing. "Nothing is happening" is useless; naming the gap is not.
        for index in range(1, len(self._stages)):
            upstream = self._stages[index - 1]
            stage = self._stages[index]
            if self._counts[upstream] >= self._minimum_inputs and self._counts[stage] == 0:
                self.standing.stalls_found += 1
                self.standing.by_stage[stage] = self.standing.by_stage.get(stage, 0) + 1
                return self._fault(
                    STALLED_AT_A_STAGE, stage, STALLED_SEVERITY, inputs,
                    self._counts[self._stages[-1]],
                    f"{upstream} produced {self._counts[upstream]} and {stage} produced "
                    f"nothing. That names the gap where a filter is rejecting everything "
                    f"and reporting success doing it",
                )

        # A gradual fall is the same failure arriving slowly enough to be missed.
        last_stage = self._stages[-1]
        history = self._history[last_stage]
        baseline = history.mean(self._window)
        if baseline is not None and baseline > 0:
            latest = float(self._counts[last_stage])
            if latest * self._slowdown_ratio < baseline:
                self.standing.slowdowns_found += 1
                return self._fault(
                    SLOWING, last_stage, DEGRADED_SEVERITY, inputs, int(latest),
                    f"{last_stage} produced {latest:.0f} against a baseline of "
                    f"{baseline:.1f}, while inputs held. Output falling by an order of "
                    f"magnitude over hours is the same failure as output stopping",
                )

        return self._outcome(
            PROGRESSING, None, None, inputs, self._counts[self._stages[-1]],
            f"{inputs} input(s) reached {self._counts[self._stages[-1]]} output(s) "
            f"through {len(self._stages)} stage(s)",
        )

    def _fault(self, state, stage, severity, inputs, outputs, reason) -> ProgressOutcome:
        fault = PartFault(
            part_id=stage,
            kind=state,
            detail=reason,
            first_seen_at_ns=self._now_ns(),
            observations=self._history[stage].count,
            is_silent=True,
            severity=severity,
            reason=(
                f"{reason}. Nothing in a per-part health view can see this, because "
                f"nothing is broken"
            ),
            detected_at_ns=self._now_ns(),
        )
        return self._outcome(state, fault, stage, inputs, outputs, fault.reason)

    def _outcome(self, state, fault, stage, inputs, outputs, reason) -> ProgressOutcome:
        return ProgressOutcome(
            state=state, fault=fault, stalled_stage=stage, inputs_seen=inputs,
            outputs_seen=outputs, reason=reason, checked_at_ns=self._now_ns(),
        )


def describe_progress(detector: NoProgressDetector) -> dict:
    return {
        "part_id": PART_ID,
        "checks": detector.standing.checks,
        "stalls_found": detector.standing.stalls_found,
        "slowdowns_found": detector.standing.slowdowns_found,
        "quiet_periods": detector.standing.quiet_periods,
        "stages_watched": detector.standing.stages_watched,
        "by_stage": dict(detector.standing.by_stage),
        "stages": list(detector._stages),
        "treats_quiet_as_stalled": False,
        "reports_an_unlocated_stall": False,
    }


def run_no_progress_detector(
    detector: NoProgressDetector, control_socket, read_counts, publish_faults,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for stage, count in read_counts():
            detector.observe(stage, count)
        outcome = detector.check()
        if outcome.is_usable:
            publish_faults(outcome.fault)
        detector.roll_period()

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

    Progress is read off the journal: each entry's kind is a stage of a
    trade's lifecycle, and the stages setting carries them in the order they
    must occur. Counts are buffered here and handed to the detector once per
    progress_period_seconds, because a candidate and the intent it becomes are
    minutes apart and a tick-sized period would read that latency as a stall.
    Ticks between flushes hand over nothing, which the detector correctly
    reports as a quiet period; part-health is consumed as the wake signal.
    """
    import time as _time

    from runtime.input_assembly import Batch

    health = Batch(read=context.bus.reader("part-health"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    publish_faults = context.bus.publisher_for("part-fault")

    stages = tuple(str(stage) for stage in context.setting("progress_stages").value)
    detector = NoProgressDetector(
        stages=stages,
        window=int(context.number("progress_window")),
        minimum_inputs=int(context.number("progress_minimum_inputs")),
        slowdown_ratio=context.number("progress_slowdown_ratio"),
    )
    period_seconds = context.number("progress_period_seconds")
    buffered = {stage: 0 for stage in stages}
    period_started = [_time.monotonic()]

    def read_counts():
        health.payloads()
        for entry in entries.payloads():
            if entry.kind in buffered:
                buffered[entry.kind] += 1
        now = _time.monotonic()
        if now - period_started[0] < period_seconds:
            return ()
        period_started[0] = now
        counts = tuple(
            (stage, count) for stage, count in buffered.items() if count
        )
        for stage in buffered:
            buffered[stage] = 0
        return counts

    return run_no_progress_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_counts=read_counts,
        publish_faults=lambda fault: publish_faults((fault,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
