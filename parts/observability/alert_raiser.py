"""alert-raiser: what a human should look at now, with the proof attached.

An alerting system fails in one of two ways, and the second is the dangerous one:
it stays silent when something is wrong, or it cries so often that a person stops
reading it. The second failure looks like a working system right up until the
alert that mattered scrolls past unread.

So three mechanisms, all aimed at the second failure:

- **Deduplication.** The same condition alerts once and then updates a count, so
  a part faulting every second is one alert with a number on it.
- **A cooldown per condition**, so a flapping condition cannot refill the list.
- **Severity by consequence, not by source.** What decides urgency is what
  happens if it is ignored -- money at risk, capture lost, a number that is wrong
  while looking right -- not which part noticed.

**Every alert carries its proof.** An alert a person cannot verify is one they
must either act on blindly or ignore, and they will learn to ignore it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "alert-raiser"

PART_DECLARATION = PartDeclaration(
    part_id="alert-raiser",
    consumes=(
        "part-fault", "trading-halt", "journal-gap", "market-anomaly",
        "hog-report", "feed-coverage", "replay-mismatch",
    ),
    produces=("alert", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CRITICAL = "critical"
HIGH = "high"
LOW = "low"

# What each source means if ignored, which is what decides urgency. A journal gap
# is critical because it means the system's own account of itself cannot be
# trusted; a hog report is low because the worst case is slow, not wrong.
SEVERITY_BY_SOURCE = {
    "journal-gap": CRITICAL,
    "replay-mismatch": CRITICAL,
    "trading-halt": HIGH,
    "part-fault": HIGH,
    "market-anomaly": HIGH,
    "feed-coverage": HIGH,
    "hog-report": LOW,
}


@dataclass(frozen=True)
class Alert:
    """One thing worth a person's attention, and how to check it."""

    alert_id: str
    source: str
    severity: str
    subject: str
    message: str
    proof: str
    occurrences: int
    first_raised_at_ns: int
    last_raised_at_ns: int

    @property
    def needs_immediate_attention(self) -> bool:
        return self.severity in (CRITICAL, HIGH)


@dataclass
class _Condition:
    alert: Alert
    last_raised_monotonic: float


@dataclass
class RaiserStanding:
    raised: int = 0
    suppressed_duplicates: int = 0
    suppressed_cooldown: int = 0
    resolved: int = 0
    by_severity: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)


class AlertRaiser:
    """Raises each distinct condition once, updates it after, and attaches its proof."""

    def __init__(self, cooldown_seconds: float, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        if cooldown_seconds < 0:
            raise ValueError("a cooldown cannot be negative")
        self._cooldown = cooldown_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._active: dict[str, _Condition] = {}
        self.standing = RaiserStanding()

    def raise_alert(
        self, source: str, subject: str, message: str, proof: str, severity: str | None = None
    ) -> Alert | None:
        """Raise one condition. Returns None when it is a duplicate inside the cooldown.

        The identity is the source plus the subject, so "part-fault on the sizer"
        is one condition however many times it recurs, and a fault on a different
        part is a different one.
        """
        alert_id = f"{source}:{subject}"
        severity = severity or SEVERITY_BY_SOURCE.get(source, LOW)
        now = self._monotonic()
        held = self._active.get(alert_id)

        if held is not None:
            updated = Alert(
                alert_id=alert_id, source=source, severity=severity, subject=subject,
                message=message, proof=proof,
                occurrences=held.alert.occurrences + 1,
                first_raised_at_ns=held.alert.first_raised_at_ns,
                last_raised_at_ns=self._now_ns(),
            )
            if now - held.last_raised_monotonic < self._cooldown:
                # Counted, not re-raised. A part faulting every second is one
                # alert with a number on it, not a list nobody will read.
                self._active[alert_id] = _Condition(updated, held.last_raised_monotonic)
                self.standing.suppressed_cooldown += 1
                self.standing.suppressed_duplicates += 1
                return None
            self._active[alert_id] = _Condition(updated, now)
            self.standing.raised += 1
            self._count(severity, source)
            return updated

        alert = Alert(
            alert_id=alert_id, source=source, severity=severity, subject=subject,
            message=message, proof=proof, occurrences=1,
            first_raised_at_ns=self._now_ns(), last_raised_at_ns=self._now_ns(),
        )
        self._active[alert_id] = _Condition(alert, now)
        self.standing.raised += 1
        self._count(severity, source)
        return alert

    def resolve(self, source: str, subject: str) -> bool:
        """The condition has ended. Returns whether there was one to end."""
        removed = self._active.pop(f"{source}:{subject}", None) is not None
        if removed:
            self.standing.resolved += 1
        return removed

    def active_alerts(self, minimum_severity: str = LOW) -> tuple[Alert, ...]:
        """Everything still wrong, worst first."""
        order = {CRITICAL: 0, HIGH: 1, LOW: 2}
        floor = order[minimum_severity]
        alerts = [
            condition.alert
            for condition in self._active.values()
            if order[condition.alert.severity] <= floor
        ]
        return tuple(sorted(alerts, key=lambda alert: (order[alert.severity], alert.alert_id)))

    def _count(self, severity: str, source: str) -> None:
        self.standing.by_severity[severity] = self.standing.by_severity.get(severity, 0) + 1
        self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1


def describe_alerts(raiser: AlertRaiser) -> dict:
    active = raiser.active_alerts()
    return {
        "part_id": PART_ID,
        "raised": raiser.standing.raised,
        "suppressed_duplicates": raiser.standing.suppressed_duplicates,
        "resolved": raiser.standing.resolved,
        "active": len(active),
        "critical_active": sum(1 for alert in active if alert.severity == CRITICAL),
        "by_severity": dict(raiser.standing.by_severity),
        "by_source": dict(raiser.standing.by_source),
    }


def run_alert_raiser(
    raiser: AlertRaiser, control_socket, read_conditions, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        raised = [raiser.raise_alert(**condition) for condition in read_conditions()]
        publish_alerts(tuple(alert for alert in raised if alert is not None))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
