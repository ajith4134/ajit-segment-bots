"""prompt-drift-monitor: the prompt did not change, so what did.

A prompt version is immutable. The model behind it is not. Providers update weights,
change defaults, deprecate a snapshot and silently route to its successor -- and the
first evidence of any of that is a version whose scores move while its text is
byte-identical.

That is the signal this part watches for, and it is the only signal in this block
whose value comes from a *change* rather than a level. So the monitor tracks each
active version's scores over time and alerts when the same prompt starts behaving
differently, with the direction stated: **a version that suddenly improves is as
much evidence of a model change as one that degrades**, and only one of those gets
noticed by anyone watching quality.

Three things it separates, because conflating them makes the alert useless:

- **Drift against a new baseline.** After a promotion the baseline resets; comparing
  a new version against the old one's history reports the promotion as drift.
- **Drift against noise.** Every score moves. The threshold is in standard
  deviations of that version's own history, not a fixed percentage, so a stable
  version alerts on a small move and a noisy one does not alert on the same move.
- **Drift against a changed model identity.** When the model id itself changed, the
  alert says so -- that is not drift to investigate, it is a known cause.

It alerts. It does not roll back: a rollback is a promotion, and promotions happen
at the gate with evidence, not here in reaction to one bad measurement.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "prompt-drift-monitor"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-drift-monitor",
    consumes=("prompt-score",),
    produces=("alert", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

STABLE = "stable"
DEGRADED = "the-same-prompt-is-doing-worse"
IMPROVED = "the-same-prompt-is-doing-better"
MODEL_CHANGED = "the-model-behind-this-version-changed"
NO_BASELINE = "not-enough-history-for-this-version-yet"
BASELINE_RESET = "the-baseline-restarted-at-a-promotion"

WATCHED_DIMENSIONS = (
    "schema_valid_fraction",
    "factually_supported_fraction",
    "agreement_with_outcome",
)


@dataclass(frozen=True)
class DriftAlert:
    version_id: str
    state: str
    dimension: str | None
    latest: float | None
    baseline_mean: float | None
    deviations: float | None
    model_id: str | None
    reason: str
    raised_at_ns: int

    @property
    def is_an_alert(self) -> bool:
        return self.state in (DEGRADED, IMPROVED, MODEL_CHANGED)


@dataclass
class DriftStanding:
    scores_seen: int = 0
    versions_watched: int = 0
    alerts_raised: int = 0
    degradations: int = 0
    improvements: int = 0
    model_changes: int = 0
    baselines_reset: int = 0


class PromptDriftMonitor:
    """Watches immutable versions for behaviour changes and names the direction."""

    def __init__(
        self,
        baseline_window: int,
        deviation_threshold: float,
        minimum_baseline: int,
        now_ns=time.time_ns,
    ) -> None:
        if baseline_window < 3:
            raise ValueError(
                "a baseline needs enough history to have a spread, or every move is an alert"
            )
        if deviation_threshold <= 0:
            raise ValueError(
                "the threshold is in standard deviations of this version's own history, "
                "so a stable version alerts on a small move and a noisy one does not"
            )
        if minimum_baseline < 2:
            raise ValueError("a spread over one observation is zero")
        self._baseline_window = baseline_window
        self._deviation_threshold = deviation_threshold
        self._minimum_baseline = minimum_baseline
        self._now_ns = now_ns
        self._history: dict[tuple, RollingWindow] = {}
        self._model_of: dict[str, str] = {}
        self.standing = DriftStanding()

    def reset_baseline(self, version_id: str) -> None:
        """A promotion restarts the baseline; otherwise the promotion reads as drift."""
        removed = [key for key in self._history if key[0] == version_id]
        for key in removed:
            del self._history[key]
        if removed:
            self.standing.baselines_reset += 1

    def observe(self, score, model_id: str | None = None) -> tuple:
        """One score for one version. Returns every alert it raises."""
        self.standing.scores_seen += 1
        alerts = []

        if model_id is not None:
            known = self._model_of.get(score.version_id)
            if known is not None and known != model_id:
                self.standing.model_changes += 1
                self.standing.alerts_raised += 1
                alerts.append(
                    self._alert(
                        score.version_id, MODEL_CHANGED, None, None, None, None, model_id,
                        f"the model behind {score.version_id} changed from {known} to "
                        f"{model_id}. That is a known cause rather than drift to "
                        f"investigate, and the baseline is restarted",
                    )
                )
                self.reset_baseline(score.version_id)
            self._model_of[score.version_id] = model_id

        for dimension in WATCHED_DIMENSIONS:
            latest = getattr(score, dimension)
            key = (score.version_id, dimension)
            window = self._history.get(key)
            if window is None:
                window = RollingWindow(self._baseline_window)
                self._history[key] = window
                self.standing.versions_watched += 1

            if window.count >= self._minimum_baseline:
                mean = window.mean(self._minimum_baseline)
                spread = window.standard_deviation(self._minimum_baseline)
                if mean is not None and spread is not None and spread > 0:
                    deviations = (latest - mean) / spread
                    if abs(deviations) >= self._deviation_threshold:
                        state = DEGRADED if deviations < 0 else IMPROVED
                        if state == DEGRADED:
                            self.standing.degradations += 1
                        else:
                            self.standing.improvements += 1
                        self.standing.alerts_raised += 1
                        alerts.append(
                            self._alert(
                                score.version_id, state, dimension, latest, mean,
                                deviations, self._model_of.get(score.version_id),
                                f"{dimension} moved {deviations:+.1f} deviation(s) to "
                                f"{latest:.3f} from a baseline of {mean:.3f}, with the "
                                f"prompt byte-identical throughout"
                                + (
                                    ". An unexplained improvement is as much evidence of a "
                                    "model change as a degradation, and only one of them "
                                    "gets noticed"
                                    if state == IMPROVED
                                    else ""
                                ),
                            )
                        )
            window.observe(latest)

        return tuple(alerts)

    def baseline_for(self, version_id: str, dimension: str) -> float | None:
        window = self._history.get((version_id, dimension))
        if window is None:
            return None
        return window.mean(self._minimum_baseline)

    def _alert(
        self, version_id, state, dimension, latest, mean, deviations, model_id, reason,
    ) -> DriftAlert:
        return DriftAlert(
            version_id=version_id, state=state, dimension=dimension, latest=latest,
            baseline_mean=mean, deviations=deviations, model_id=model_id, reason=reason,
            raised_at_ns=self._now_ns(),
        )


def describe_drift_monitoring(monitor: PromptDriftMonitor) -> dict:
    return {
        "part_id": PART_ID,
        "scores_seen": monitor.standing.scores_seen,
        "version_dimensions_watched": monitor.standing.versions_watched,
        "alerts_raised": monitor.standing.alerts_raised,
        "degradations": monitor.standing.degradations,
        "unexplained_improvements": monitor.standing.improvements,
        "model_changes": monitor.standing.model_changes,
        "baselines_reset_at_a_promotion": monitor.standing.baselines_reset,
        "watched_dimensions": list(WATCHED_DIMENSIONS),
        "rolls_anything_back": False,
        "uses_a_fixed_percentage_threshold": False,
    }


def run_prompt_drift_monitor(
    monitor: PromptDriftMonitor, control_socket, read_scores, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for score, model_id in read_scores():
            for alert in monitor.observe(score, model_id):
                publish_alerts(alert)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
