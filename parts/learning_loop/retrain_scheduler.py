"""retrain-scheduler: when to retrain, which is not "whenever something changed".

Retraining is the most expensive thing this system does and the easiest to
trigger badly. Retrain too eagerly and every noisy week rewrites the model;
retrain too rarely and it trades a market that ended. Both failures are quiet.

So a retrain needs a reason **and** enough new evidence to act on it:

- **Drift is a reason, not a trigger.** A drift alert says something changed; a
  retrain on drift alone fits the change, and the change is often noise. So a
  drift alert schedules a retrain only when enough new labels have accumulated to
  learn from.
- **Forgetting is a stronger reason** and needs less new data, because the fix is
  to replay what was lost rather than to learn something new.
- **New labels alone are a reason too**, on a cadence -- a model that never
  retrains without a crisis is a model that only ever learns from crises.

**Only the challenger is ever retrained.** The live model keeps trading, so
retraining is never a live change and never has to be rushed. A scheduler that
could retrain the champion would make every retrain a risk decision.

**The duty cycle is respected.** Retraining competes with everything else on this
machine, and a scheduler that ignored the governor's duty cycle would be a
feature reaching into the control plane (T-2).

**Two retrains are never scheduled at once.** The second would train on the same
labels as the first and produce a model that looks like an independent
confirmation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "retrain-scheduler"

PART_DECLARATION = PartDeclaration(
    part_id="retrain-scheduler",
    consumes=("model-drift-alert", "forgetting-report", "training-label", "duty-cycle"),
    produces=("retrain-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SCHEDULED = "scheduled"
NOT_ENOUGH_NEW_LABELS = "not-enough-new-labels-to-learn-from-yet"
ALREADY_RUNNING = "a-retrain-is-already-running-for-this-model"
NO_DUTY_CYCLE = "the-governor-has-not-granted-the-time"
NO_REASON = "nothing-has-asked-for-a-retrain"

BECAUSE_OF_DRIFT = "the-model-has-drifted"
BECAUSE_OF_FORGETTING = "the-model-has-forgotten-an-era"
BECAUSE_OF_CADENCE = "enough-new-evidence-has-accumulated"

CHALLENGER = "challenger"


@dataclass(frozen=True)
class RetrainRequest:
    """One scheduled retrain, of the challenger, with the reason that earned it."""

    model_name: str
    role: str
    state: str
    because: str | None
    new_labels: int
    required_labels: int
    duty_cycle_granted: float
    reason: str
    scheduled_at_ns: int

    @property
    def is_scheduled(self) -> bool:
        return self.state == SCHEDULED

    @property
    def touches_the_live_model(self) -> bool:
        """Never. The live model keeps trading, so a retrain is never a live change."""
        return False


@dataclass
class SchedulerStanding:
    requests: int = 0
    scheduled: int = 0
    refused_not_enough_labels: int = 0
    refused_already_running: int = 0
    refused_no_duty_cycle: int = 0
    labels_seen: int = 0
    by_reason: dict = field(default_factory=dict)


class RetrainScheduler:
    """Schedules challenger retrains when there is both a reason and enough evidence."""

    def __init__(
        self,
        labels_for_drift: int,
        labels_for_forgetting: int,
        labels_for_cadence: int,
        minimum_duty_cycle: float,
        now_ns=time.time_ns,
    ) -> None:
        if labels_for_forgetting > labels_for_drift:
            raise ValueError(
                "forgetting needs less new data than drift, because the fix is to replay what "
                "was lost rather than to learn something new"
            )
        if not 0.0 < minimum_duty_cycle <= 1.0:
            raise ValueError(
                "retraining competes with everything else on this machine; ignoring the "
                "governor's duty cycle would be a feature reaching into the control plane"
            )
        self._labels_for_drift = labels_for_drift
        self._labels_for_forgetting = labels_for_forgetting
        self._labels_for_cadence = labels_for_cadence
        self._minimum_duty_cycle = minimum_duty_cycle
        self._now_ns = now_ns
        self._new_labels: dict[str, int] = {}
        self._drifted: dict[str, str] = {}
        self._forgetting: dict[str, str] = {}
        self._running: set[str] = set()
        self._duty_cycle: float = 1.0
        self.standing = SchedulerStanding()

    def observe_label(self, model_name: str) -> None:
        """One new labelled example this model has not trained on."""
        self._new_labels[model_name] = self._new_labels.get(model_name, 0) + 1
        self.standing.labels_seen += 1

    def observe_drift_alert(self, model_name: str, reason: str) -> None:
        """Drift is a reason, not a trigger: a retrain on drift alone fits the noise."""
        self._drifted[model_name] = reason

    def observe_forgetting_report(self, model_name: str, forgotten_era: str) -> None:
        self._forgetting[model_name] = forgotten_era

    def observe_duty_cycle(self, granted: float) -> None:
        self._duty_cycle = granted

    def retrain_started(self, model_name: str) -> None:
        self._running.add(model_name)

    def retrain_finished(self, model_name: str) -> None:
        """A finished retrain clears its labels: the next one learns from new evidence."""
        self._running.discard(model_name)
        self._new_labels[model_name] = 0
        self._drifted.pop(model_name, None)
        self._forgetting.pop(model_name, None)

    def consider(self, model_name: str) -> RetrainRequest:
        self.standing.requests += 1
        new_labels = self._new_labels.get(model_name, 0)

        if model_name in self._running:
            # The second would train on the same labels and produce a model that
            # looks like an independent confirmation.
            self.standing.refused_already_running += 1
            return self._request(
                model_name, ALREADY_RUNNING, None, new_labels, 0,
                "a retrain is already running for this model; a second would train on the "
                "same labels and produce something that looks like an independent confirmation",
            )

        if self._duty_cycle < self._minimum_duty_cycle:
            self.standing.refused_no_duty_cycle += 1
            return self._request(
                model_name, NO_DUTY_CYCLE, None, new_labels, 0,
                f"the governor has granted {self._duty_cycle:.0%} duty cycle, below the "
                f"{self._minimum_duty_cycle:.0%} a retrain needs; running anyway would be "
                f"reaching into the control plane",
            )

        because, required = self._reason_for(model_name)
        if because is None:
            return self._request(
                model_name, NO_REASON, None, new_labels, 0,
                "nothing has asked for a retrain: no drift, no forgetting, and not enough new "
                "evidence to justify one on cadence",
            )

        if new_labels < required:
            self.standing.refused_not_enough_labels += 1
            return self._request(
                model_name, NOT_ENOUGH_NEW_LABELS, because, new_labels, required,
                f"{because}, but only {new_labels} new label(s) of the {required} needed. "
                f"Retraining now would fit the change rather than learn from it, and the "
                f"change is often noise",
            )

        self.standing.scheduled += 1
        self.standing.by_reason[because] = self.standing.by_reason.get(because, 0) + 1
        return self._request(
            model_name, SCHEDULED, because, new_labels, required,
            f"{because}, with {new_labels} new label(s) against the {required} required. "
            f"Scheduled on the challenger only -- the live model keeps trading, so this is "
            f"never a live change and never has to be rushed",
        )

    def _reason_for(self, model_name: str) -> tuple:
        """The strongest reason to retrain, and how much new evidence it needs."""
        if model_name in self._forgetting:
            return BECAUSE_OF_FORGETTING, self._labels_for_forgetting
        if model_name in self._drifted:
            return BECAUSE_OF_DRIFT, self._labels_for_drift
        if self._new_labels.get(model_name, 0) >= self._labels_for_cadence:
            # A model that never retrains without a crisis only ever learns
            # from crises.
            return BECAUSE_OF_CADENCE, self._labels_for_cadence
        return None, 0

    def _request(self, model_name, state, because, new_labels, required, reason) -> RetrainRequest:
        return RetrainRequest(
            model_name=model_name,
            role=CHALLENGER,
            state=state,
            because=because,
            new_labels=new_labels,
            required_labels=required,
            duty_cycle_granted=self._duty_cycle,
            reason=reason,
            scheduled_at_ns=self._now_ns(),
        )


def describe_retrain_scheduling(scheduler: RetrainScheduler) -> dict:
    return {
        "part_id": PART_ID,
        "requests": scheduler.standing.requests,
        "scheduled": scheduler.standing.scheduled,
        "refused_not_enough_new_labels": scheduler.standing.refused_not_enough_labels,
        "refused_a_retrain_already_running": scheduler.standing.refused_already_running,
        "refused_no_duty_cycle": scheduler.standing.refused_no_duty_cycle,
        "labels_seen": scheduler.standing.labels_seen,
        "by_reason": dict(sorted(scheduler.standing.by_reason.items())),
        "retrains_only": CHALLENGER,
        "duty_cycle_granted": scheduler._duty_cycle,
    }


def run_retrain_scheduler(
    scheduler: RetrainScheduler, control_socket, read_signals, publish_requests,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        models = read_signals(scheduler)
        publish_requests(tuple(scheduler.consider(model) for model in models))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_retrain_scheduling(scheduler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Labels, drift alerts and forgetting reports each name the model they
    are about; the duty cycle the planner grants is read as the fraction of
    the current UTC hour that is allowed. Every model that received a signal
    this wake is considered.
    """
    import datetime

    from runtime.input_assembly import Batch

    drift = Batch(read=context.bus.reader("model-drift-alert"))
    forgetting = Batch(read=context.bus.reader("forgetting-report"))
    labels = Batch(read=context.bus.reader("training-label"))
    cycles = Batch(read=context.bus.reader("duty-cycle"))
    publish_requests = context.bus.publisher_for("retrain-request")
    scheduler = RetrainScheduler(
        labels_for_drift=int(context.number("retrain_labels_for_drift")),
        labels_for_forgetting=int(context.number("retrain_labels_for_forgetting")),
        labels_for_cadence=int(context.number("retrain_labels_for_cadence")),
        minimum_duty_cycle=context.number("retrain_minimum_duty_cycle"),
    )

    def read_signals(_scheduler):
        touched = set()
        for label in labels.payloads():
            # A label is evidence for the bot that trades the detector's side;
            # the detector name is the model family it trains.
            scheduler.observe_label(label.detector)
            touched.add(label.detector)
        for alert in drift.payloads():
            scheduler.observe_drift_alert(alert.model_name, alert.reason)
            touched.add(alert.model_name)
        for report in forgetting.payloads():
            for era in report.forgotten_eras:
                scheduler.observe_forgetting_report(report.model_name, str(era))
            touched.add(report.model_name)
        hour = datetime.datetime.now(datetime.UTC).hour
        for cycle in cycles.payloads():
            scheduler.observe_duty_cycle(1.0 if hour in cycle.allowed_hours else 0.0)
        return tuple(sorted(touched))

    def publish(requests) -> None:
        if requests:
            publish_requests(requests)

    return run_retrain_scheduler(
        scheduler=scheduler,
        control_socket=context.control_socket,
        read_signals=read_signals,
        publish_requests=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
