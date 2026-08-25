"""event-risk-limiter: shrink the limit around events and anomalies.

Some risks are known in advance and some announce themselves. Both mean the same
thing: the distribution the strategy was fitted on does not apply right now.

- **Scheduled events** -- a funding settlement, a listing, a macro print -- get a
  window before and after, because the move usually starts before the headline.
- **Anomalies and turbulence** -- an unexplained gap, a spike in the turbulence
  index -- get a shrink that decays as conditions normalise, rather than a
  cliff-edge release that would put full size back on at the first calm tick.

**The shrink is multiplicative across overlapping causes.** A listing during a
turbulent hour is riskier than either alone, and taking the minimum would ignore
the second one entirely.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "event-risk-limiter"

PART_DECLARATION = PartDeclaration(
    part_id="event-risk-limiter",
    consumes=(
        "market-event", "market-anomaly", "turbulence-index", "venue-announcement", "sequence-pattern"
    ),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SCHEDULED_EVENT = "scheduled-event"
ANOMALY = "anomaly"
TURBULENCE = "turbulence"
ANNOUNCEMENT = "venue-announcement"


@dataclass(frozen=True)
class RiskEvent:
    """One reason to carry less risk than usual, and for how long."""

    kind: str
    subject: str
    shrink_to: float
    starts_at_monotonic: float
    ends_at_monotonic: float
    decays: bool
    reason: str


@dataclass
class EventStanding:
    events_registered: int = 0
    events_expired: int = 0
    limits_issued: int = 0
    smallest_shrink: float = 1.0
    active_kinds: dict = field(default_factory=dict)


class EventRiskLimiter:
    """Multiplies together every active reason to carry less, and expires them."""

    def __init__(
        self,
        allowed_fraction_when_calm: float,
        turbulence_shrink_floor: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < turbulence_shrink_floor <= 1.0:
            raise ValueError("the turbulence floor must be a fraction in (0, 1]")
        self._allowed = allowed_fraction_when_calm
        self._turbulence_floor = turbulence_shrink_floor
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._events: list[RiskEvent] = []
        self.standing = EventStanding()

    def register_scheduled_event(
        self, subject: str, seconds_until: float, window_seconds: float, shrink_to: float, reason: str
    ) -> RiskEvent:
        """A known event, with a window that opens before it and closes after.

        The window opens early because the repricing happens in anticipation --
        a limiter that only shrank after the print would already be positioned
        through the move it was meant to avoid.
        """
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=SCHEDULED_EVENT,
                subject=subject,
                shrink_to=shrink_to,
                starts_at_monotonic=now + seconds_until - window_seconds,
                ends_at_monotonic=now + seconds_until + window_seconds,
                decays=False,
                reason=reason,
            )
        )

    def register_anomaly(self, subject: str, shrink_to: float, decay_seconds: float, reason: str) -> RiskEvent:
        """Something unexpected happened; carry less until it stops mattering."""
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=ANOMALY, subject=subject, shrink_to=shrink_to,
                starts_at_monotonic=now, ends_at_monotonic=now + decay_seconds,
                decays=True, reason=reason,
            )
        )

    def register_announcement(self, subject: str, shrink_to: float, seconds: float, reason: str) -> RiskEvent:
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=ANNOUNCEMENT, subject=subject, shrink_to=shrink_to,
                starts_at_monotonic=now, ends_at_monotonic=now + seconds,
                decays=False, reason=reason,
            )
        )

    def observe_turbulence(self, index: float, normal_index: float, seconds: float) -> RiskEvent | None:
        """Shrink in proportion to how far turbulence is above its normal level.

        Proportional rather than a threshold: turbulence is a continuum and a
        step function at some level would treat 1.99x normal as calm.
        """
        if normal_index <= 0 or index <= normal_index:
            return None
        ratio = normal_index / index
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=TURBULENCE,
                subject="market",
                shrink_to=max(self._turbulence_floor, ratio),
                starts_at_monotonic=now,
                ends_at_monotonic=now + seconds,
                decays=True,
                reason=f"turbulence at {index:.2f} against a normal {normal_index:.2f}",
            )
        )

    def _register(self, event: RiskEvent) -> RiskEvent:
        self._events.append(event)
        self.standing.events_registered += 1
        self.standing.active_kinds[event.kind] = self.standing.active_kinds.get(event.kind, 0) + 1
        return event

    def read_limit(self) -> RiskLimit:
        now = self._monotonic()
        before = len(self._events)
        self._events = [event for event in self._events if event.ends_at_monotonic > now]
        self.standing.events_expired += before - len(self._events)
        self.standing.limits_issued += 1

        active = [event for event in self._events if event.starts_at_monotonic <= now]
        if not active:
            return RiskLimit(
                limiter=PART_ID,
                fraction_of_allotment=self._allowed,
                reason="no event or anomaly is in force",
                is_binding=False,
                decided_at_ns=self._now_ns(),
            )

        shrink = 1.0
        for event in active:
            shrink *= self._shrink_of(event, now)
        allowed = self._allowed * shrink
        self.standing.smallest_shrink = min(self.standing.smallest_shrink, shrink)

        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=allowed,
            reason=(
                f"{len(active)} active: "
                + "; ".join(f"{event.kind} {event.subject} ({event.reason})" for event in active[:3])
            ),
            is_binding=allowed <= NO_RISK_ALLOWED,
            decided_at_ns=self._now_ns(),
        )

    def _shrink_of(self, event: RiskEvent, now: float) -> float:
        """How much this event still shrinks by, decaying toward none if it decays."""
        if not event.decays:
            return event.shrink_to
        span = event.ends_at_monotonic - event.starts_at_monotonic
        if span <= 0:
            return event.shrink_to
        elapsed = (now - event.starts_at_monotonic) / span
        # Linear return to full size across the window: a cliff-edge release
        # would put full size back on at the first calm tick.
        return event.shrink_to + (1.0 - event.shrink_to) * min(1.0, max(0.0, elapsed))

    @property
    def active_events(self) -> tuple[RiskEvent, ...]:
        now = self._monotonic()
        return tuple(
            event for event in self._events
            if event.starts_at_monotonic <= now < event.ends_at_monotonic
        )


def describe_events(limiter: EventRiskLimiter) -> dict:
    return {
        "part_id": PART_ID,
        "events_registered": limiter.standing.events_registered,
        "events_expired": limiter.standing.events_expired,
        "active_events": len(limiter.active_events),
        "limits_issued": limiter.standing.limits_issued,
        "smallest_shrink": limiter.standing.smallest_shrink,
        "by_kind": dict(limiter.standing.active_kinds),
    }


def run_event_risk_limiter(
    limiter: EventRiskLimiter, control_socket, read_events, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(limiter)
        publish_limit(limiter.read_limit())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_events(limiter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Five kinds of evidence, each registered in the limiter's own terms. A
    market event with an effective time is scheduled; one without is treated
    as an announcement. A turbulence reading is proportional, against the
    index's normal level -- the number of symbols in the index, which is the
    expectation of a Mahalanobis distance over that many dimensions. A
    sequence pattern is read as evidence about the strategy, not the market,
    and is consumed without shrinking anything: the limiter has no stated
    rule for it and an invented one would be a number with no provenance.
    """
    import time as _time

    from runtime.input_assembly import Batch

    events = Batch(read=context.bus.reader("market-event"))
    anomalies = Batch(read=context.bus.reader("market-anomaly"))
    turbulence = Batch(read=context.bus.reader("turbulence-index"))
    announcements = Batch(read=context.bus.reader("venue-announcement"))
    patterns = Batch(read=context.bus.reader("sequence-pattern"))
    publish_limits = context.bus.publisher_for("risk-limit")
    limiter = EventRiskLimiter(
        allowed_fraction_when_calm=context.number("risk_allowed_fraction_when_clear"),
        turbulence_shrink_floor=context.number("event_risk_turbulence_floor"),
    )
    anomaly_shrink = context.number("event_risk_anomaly_shrink_to")
    anomaly_decay = context.number("event_risk_anomaly_decay")
    announcement_shrink = context.number("event_risk_announcement_shrink_to")
    announcement_window = context.number("event_risk_announcement_window")
    scheduled_window = context.number("event_risk_scheduled_window")
    scheduled_shrink = context.number("event_risk_scheduled_shrink_to")
    turbulence_horizon = context.number("event_risk_turbulence_horizon")

    def register_scheduled_or_announced(subject: str, effective_at_ns, reason: str) -> None:
        if effective_at_ns is not None:
            seconds_until = (effective_at_ns - _time.time_ns()) / 1e9
            if seconds_until + scheduled_window > 0:
                limiter.register_scheduled_event(subject, seconds_until, scheduled_window, scheduled_shrink, reason)
            return
        limiter.register_announcement(subject, announcement_shrink, announcement_window, reason)

    def read_events(_limiter) -> None:
        for event in events.payloads():
            subject = ",".join(event.symbols) if event.symbols else event.venue_id
            register_scheduled_or_announced(subject, event.effective_at_ns, f"{event.event_type}: {event.title}")
        for announcement in announcements.payloads():
            symbols = getattr(announcement, "symbols", ())
            subject = ",".join(symbols) if symbols else announcement.venue_id
            # `headline` and nothing else: the alternative read here named a field
            # `VenueAnnouncement` has never carried, which is a fallback that could
            # only ever have returned its default.
            headline = announcement.headline
            register_scheduled_or_announced(subject, announcement.effective_at_ns, headline)
        for anomaly in anomalies.payloads():
            if anomaly.is_anomalous:
                limiter.register_anomaly(
                    f"{anomaly.venue_id}:{anomaly.symbol}", anomaly_shrink, anomaly_decay, anomaly.reason
                )
        for reading in turbulence.payloads():
            if reading.distance is not None and reading.symbols:
                limiter.observe_turbulence(reading.distance, float(len(reading.symbols)), turbulence_horizon)
        patterns.payloads()

    return run_event_risk_limiter(
        limiter=limiter,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_limit=lambda limit: publish_limits((limit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
