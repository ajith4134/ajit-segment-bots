"""model-drift-monitor: when a model has stopped being the model that was measured.

A forecaster's accuracy is measured over a window, and a window is a claim about a
period. When the market changes, that claim expires -- and the failure is quiet,
because the model keeps producing confident forecasts at exactly the rate it
always did. Nothing else in the system would notice.

**Drift is detected on the model's own recent accuracy against its established
accuracy**, not against a fixed threshold. A model that was 58% right and is now
51% has drifted; a model that was always 51% has not, and a fixed threshold would
call both the same and be wrong about one of them.

**Two independent signals, and either is enough:**

- **Accuracy falling** below what this model established, by more than the
  sampling noise its own sample size implies. The noise bound matters: over
  twenty forecasts a seven-point drop is unremarkable, and a monitor without it
  fires constantly on small samples and trains everyone to ignore it.
- **The forgetting report.** A model that has stopped recalling what it was
  trained on has drifted whatever its recent accuracy says, and that failure
  arrives before the accuracy does.

**An alert names what to do about it.** A drift alert that says only "drift" is a
notification; one that says which model, on what evidence, and whether retraining
or a champion swap is the response, is something a part can act on.

**It will not alert twice for the same drift.** An alert that repeats every tick
is noise, and the response to it -- retraining -- takes longer than a tick.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "model-drift-monitor"

PART_DECLARATION = PartDeclaration(
    part_id="model-drift-monitor",
    consumes=("forecast-accuracy", "forgetting-report"),
    produces=("model-drift-alert", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NO_DRIFT = "no-drift"
ACCURACY_FELL = "accuracy-has-fallen-below-what-this-model-established"
MODEL_IS_FORGETTING = "the-model-has-stopped-recalling-what-it-was-trained-on"
NOT_ENOUGH_HISTORY = "too-little-history-to-say-what-this-model-established"

RETRAIN = "retrain-the-challenger-and-promote-it-if-it-wins"
SWAP_CHAMPION = "promote-the-challenger-now"
WATCH = "watch-it"


@dataclass(frozen=True)
class DriftAlert:
    """One model that has stopped being the model that was measured, and what to do."""

    forecaster: str
    model_name: str
    state: str
    established_accuracy: float | None
    recent_accuracy: float | None
    drop: float | None
    noise_bound: float | None
    recommended_action: str
    forgetting_score: float | None
    reason: str
    raised_at_ns: int

    @property
    def has_drifted(self) -> bool:
        return self.state in (ACCURACY_FELL, MODEL_IS_FORGETTING)


@dataclass
class MonitorStanding:
    checks: int = 0
    alerts_raised: int = 0
    alerts_suppressed_as_repeats: int = 0
    accuracy_alerts: int = 0
    forgetting_alerts: int = 0
    not_enough_history: int = 0
    largest_drop_seen: float | None = None


class ModelDriftMonitor:
    """Watches each model against its own established accuracy, with a noise bound."""

    def __init__(
        self,
        established_window: int,
        recent_window: int,
        minimum_established: int,
        noise_multiple: float,
        forgetting_threshold: float,
        realert_after_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if recent_window >= established_window:
            raise ValueError(
                "the recent window must be shorter than the established one, or there is "
                "nothing to compare against"
            )
        if noise_multiple <= 0:
            raise ValueError(
                "without a noise bound this monitor fires on small samples and trains "
                "everyone to ignore it"
            )
        if realert_after_seconds <= 0:
            raise ValueError(
                "an alert that repeats every tick is noise, and retraining takes longer "
                "than a tick"
            )
        self._established_window = established_window
        self._recent_window = recent_window
        self._minimum_established = minimum_established
        self._noise_multiple = noise_multiple
        self._forgetting_threshold = forgetting_threshold
        self._realert_after_ns = int(realert_after_seconds * 1e9)
        self._now_ns = now_ns
        self._accuracy: dict[tuple[str, str], RollingWindow] = {}
        self._forgetting: dict[tuple[str, str], float] = {}
        self._last_alert: dict[tuple[str, str], int] = {}
        self.standing = MonitorStanding()

    def observe_accuracy(self, accuracy) -> None:
        """One scored forecast's accuracy, appended to this model's history."""
        key = (accuracy.forecaster, accuracy.model_name)
        window = self._accuracy.get(key)
        if window is None:
            window = RollingWindow(length=self._established_window)
            self._accuracy[key] = window
        window.observe(accuracy.directional_accuracy.value)

    def observe_forgetting_report(self, forecaster: str, model_name: str, score: float) -> None:
        """How much of what this model was trained on it still recalls, in [0, 1]."""
        self._forgetting[(forecaster, model_name)] = score

    def check(self, forecaster: str, model_name: str) -> DriftAlert:
        self.standing.checks += 1
        key = (forecaster, model_name)
        window = self._accuracy.get(key)

        forgetting = self._forgetting.get(key)
        if forgetting is not None and forgetting < self._forgetting_threshold:
            # Forgetting arrives before the accuracy does, so it is checked first.
            return self._alert(
                key, MODEL_IS_FORGETTING, None, None, None, None, RETRAIN, forgetting,
                f"{model_name} recalls {forgetting:.0%} of what it was trained on, below the "
                f"{self._forgetting_threshold:.0%} this monitor treats as intact. That is "
                f"drift whatever its recent accuracy says, and it arrives first",
            )

        if window is None or window.count < self._minimum_established:
            self.standing.not_enough_history += 1
            return self._alert(
                key, NOT_ENOUGH_HISTORY, None, None, None, None, WATCH, forgetting,
                f"{0 if window is None else window.count} scored forecast(s) of the "
                f"{self._minimum_established} needed to say what {model_name} established",
            )

        values = list(window.values)
        recent = values[-self._recent_window :]
        established = values[: -self._recent_window] or values
        recent_accuracy = sum(recent) / len(recent)
        established_accuracy = sum(established) / len(established)
        drop = established_accuracy - recent_accuracy

        # The sampling noise this many observations implies. Over twenty
        # forecasts a seven-point drop is unremarkable; without this bound the
        # monitor fires constantly and everyone learns to ignore it.
        noise = self._noise_multiple * math.sqrt(
            max(1e-9, established_accuracy * (1 - established_accuracy)) / len(recent)
        )

        if drop > noise:
            self.standing.accuracy_alerts += 1
            if self.standing.largest_drop_seen is None or drop > self.standing.largest_drop_seen:
                self.standing.largest_drop_seen = drop
            action = SWAP_CHAMPION if drop > noise * 2 else RETRAIN
            return self._alert(
                key, ACCURACY_FELL, established_accuracy, recent_accuracy, drop, noise,
                action, forgetting,
                f"{model_name} established {established_accuracy:.1%} over "
                f"{len(established)} forecast(s) and has been {recent_accuracy:.1%} over the "
                f"last {len(recent)}: a {drop:.1%} drop against a {noise:.1%} noise bound at "
                f"this sample size. Compared against its own record rather than a fixed "
                f"threshold, because a model that was always 51% has not drifted",
            )

        return self._alert(
            key, NO_DRIFT, established_accuracy, recent_accuracy, drop, noise, WATCH, forgetting,
            f"{model_name} is at {recent_accuracy:.1%} against an established "
            f"{established_accuracy:.1%}; the {drop:+.1%} difference is inside the "
            f"{noise:.1%} this sample size explains",
        )

    def _alert(
        self, key, state, established, recent, drop, noise, action, forgetting, reason
    ) -> DriftAlert:
        forecaster, model_name = key
        alert = DriftAlert(
            forecaster=forecaster,
            model_name=model_name,
            state=state,
            established_accuracy=established,
            recent_accuracy=recent,
            drop=drop,
            noise_bound=noise,
            recommended_action=action,
            forgetting_score=forgetting,
            reason=reason,
            raised_at_ns=self._now_ns(),
        )

        if alert.has_drifted:
            last = self._last_alert.get(key)
            now = self._now_ns()
            if last is not None and now - last < self._realert_after_ns:
                self.standing.alerts_suppressed_as_repeats += 1
                return DriftAlert(
                    forecaster=forecaster,
                    model_name=model_name,
                    state=NO_DRIFT,
                    established_accuracy=established,
                    recent_accuracy=recent,
                    drop=drop,
                    noise_bound=noise,
                    recommended_action=WATCH,
                    forgetting_score=forgetting,
                    reason=(
                        f"{model_name} has already been alerted on within the last "
                        f"{self._realert_after_ns / 1e9:.0f}s; repeating it every tick would "
                        f"be noise, and retraining takes longer than a tick"
                    ),
                    raised_at_ns=now,
                )
            self._last_alert[key] = now
            self.standing.alerts_raised += 1
            if state == MODEL_IS_FORGETTING:
                self.standing.forgetting_alerts += 1
        return alert


def describe_drift_monitoring(monitor: ModelDriftMonitor) -> dict:
    return {
        "part_id": PART_ID,
        "checks": monitor.standing.checks,
        "alerts_raised": monitor.standing.alerts_raised,
        "alerts_suppressed_as_repeats": monitor.standing.alerts_suppressed_as_repeats,
        "accuracy_alerts": monitor.standing.accuracy_alerts,
        "forgetting_alerts": monitor.standing.forgetting_alerts,
        "checks_with_too_little_history": monitor.standing.not_enough_history,
        "largest_drop_seen": monitor.standing.largest_drop_seen,
        "models_watched": len(monitor._accuracy),
    }


def run_model_drift_monitor(
    monitor: ModelDriftMonitor, control_socket, read_accuracy_and_reports, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        models = read_accuracy_and_reports(monitor)
        publish_alerts(
            tuple(monitor.check(forecaster, model) for forecaster, model in models)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
