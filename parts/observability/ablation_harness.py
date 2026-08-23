"""ablation-harness: measure what actually breaks when each part is switched off.

The only honest answer to "does this part matter". A system of 321 parts
accumulates ones that made sense when they were written and now do nothing, and
nothing else in this design would ever notice: a part that is running, reporting
healthy, and contributing nothing looks exactly like a part that is load-bearing.

So each part is switched off in turn and the system is measured with it gone. A
part whose absence changes nothing measurable is not thereby useless -- it may be
a safety net that has not been needed -- so the scorecard says **what changed**,
not whether the part should be removed. That judgement needs a person.

Two rules that keep this from being dangerous:

- **Never ablate a part the operator has protected.** Switching off the risk
  limiter to see what happens is an experiment with real money.
- **One at a time, with a recovery period.** Two ablations at once cannot be
  attributed, and a system measured while still recovering from the last one
  attributes the recovery to the wrong part.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "ablation-harness"

PART_DECLARATION = PartDeclaration(
    part_id="ablation-harness",
    consumes=("part-health",),
    produces=("ablation-scorecard", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

IDLE = "idle"
ABLATING = "ablating"
RECOVERING = "recovering"

MATTERED = "measurably-mattered"
NO_MEASURABLE_EFFECT = "no-measurable-effect"
PROTECTED = "protected-never-ablated"
INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class AblationScorecard:
    """What changed while one part was off, and what that does and does not prove."""

    part_id: str
    verdict: str
    baseline: dict
    ablated: dict
    changes: dict
    samples: int
    reason: str
    scored_at_ns: int

    @property
    def mattered(self) -> bool:
        return self.verdict == MATTERED


@dataclass
class HarnessStanding:
    ablations_run: int = 0
    parts_protected: int = 0
    mattered: int = 0
    no_effect: int = 0
    inconclusive: int = 0
    state: str = IDLE
    currently_ablating: str | None = None


class AblationHarness:
    """Switches one part off at a time and records what measurably changed."""

    def __init__(
        self,
        measurement_samples: int,
        recovery_seconds: float,
        significant_change_fraction: float,
        protected_parts: tuple[str, ...] = (),
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if measurement_samples < 2:
            raise ValueError("a change needs at least two samples to be a change")
        self._samples_needed = measurement_samples
        self._recovery = recovery_seconds
        self._significant = significant_change_fraction
        self._protected = set(protected_parts)
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._baseline: dict[str, list[float]] = {}
        self._ablated: dict[str, list[float]] = {}
        self._current: str | None = None
        self._recovering_until: float | None = None
        self.standing = HarnessStanding(parts_protected=len(self._protected))

    def protect(self, part_id: str) -> None:
        """Mark a part that must never be switched off to see what happens."""
        self._protected.add(part_id)
        self.standing.parts_protected = len(self._protected)

    def observe_baseline(self, metrics: dict) -> None:
        """A measurement of the system with everything running."""
        for name, value in metrics.items():
            self._baseline.setdefault(name, []).append(float(value))

    def begin_ablation(self, part_id: str) -> str:
        """Start measuring the system without one part. Returns the state entered."""
        if part_id in self._protected:
            self.standing.state = IDLE
            return PROTECTED
        now = self._monotonic()
        if self._recovering_until is not None and now < self._recovering_until:
            self.standing.state = RECOVERING
            return RECOVERING
        if self._current is not None:
            # One at a time: two ablations at once cannot be attributed.
            return ABLATING

        self._current = part_id
        self._ablated = {}
        self.standing.state = ABLATING
        self.standing.currently_ablating = part_id
        self.standing.ablations_run += 1
        return ABLATING

    def observe_ablated(self, metrics: dict) -> None:
        """A measurement taken while the current part is switched off."""
        if self._current is None:
            return
        for name, value in metrics.items():
            self._ablated.setdefault(name, []).append(float(value))

    def end_ablation(self) -> AblationScorecard | None:
        """Switch the part back on and score what its absence changed."""
        if self._current is None:
            return None
        part_id = self._current
        self._current = None
        self.standing.currently_ablating = None
        self._recovering_until = self._monotonic() + self._recovery
        self.standing.state = RECOVERING

        baseline = {name: self._mean(values) for name, values in self._baseline.items()}
        ablated = {name: self._mean(values) for name, values in self._ablated.items()}
        samples = min(
            (len(values) for values in self._ablated.values()), default=0
        )

        if samples < self._samples_needed:
            self.standing.inconclusive += 1
            return self._scorecard(
                part_id, INCONCLUSIVE, baseline, ablated, {}, samples,
                f"{samples} samples of the {self._samples_needed} needed; too little to tell "
                f"a change from noise",
            )

        changes = {}
        for name, ablated_value in ablated.items():
            base = baseline.get(name)
            if base is None or base == 0:
                continue
            change = (ablated_value - base) / abs(base)
            if abs(change) >= self._significant:
                changes[name] = change

        if changes:
            self.standing.mattered += 1
            return self._scorecard(
                part_id, MATTERED, baseline, ablated, changes, samples,
                "switching it off changed: "
                + ", ".join(f"{name} by {change:+.1%}" for name, change in sorted(changes.items())),
            )

        self.standing.no_effect += 1
        return self._scorecard(
            part_id, NO_MEASURABLE_EFFECT, baseline, ablated, {}, samples,
            f"nothing measured changed by more than {self._significant:.0%}. That is not proof "
            f"it is useless -- a safety net that has not been needed measures the same way -- "
            f"and whether to keep it is a person's judgement",
        )

    def _mean(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def _scorecard(self, part_id, verdict, baseline, ablated, changes, samples, reason) -> AblationScorecard:
        return AblationScorecard(
            part_id=part_id, verdict=verdict, baseline=baseline, ablated=ablated,
            changes=changes, samples=samples, reason=reason, scored_at_ns=self._now_ns(),
        )

    @property
    def is_ablating(self) -> bool:
        return self._current is not None


def describe_ablation(harness: AblationHarness) -> dict:
    return {
        "part_id": PART_ID,
        "state": harness.standing.state,
        "currently_ablating": harness.standing.currently_ablating,
        "ablations_run": harness.standing.ablations_run,
        "parts_protected": harness.standing.parts_protected,
        "mattered": harness.standing.mattered,
        "no_measurable_effect": harness.standing.no_effect,
        "inconclusive": harness.standing.inconclusive,
    }


def run_ablation_harness(
    harness: AblationHarness, control_socket, read_measurements, publish_scorecard,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        scorecard = read_measurements(harness)
        if scorecard is not None:
            publish_scorecard(scorecard)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
