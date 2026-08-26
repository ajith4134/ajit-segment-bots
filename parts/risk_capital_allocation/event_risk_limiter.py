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
    # Which symbols this event is about. Empty means every symbol, which is what
    # turbulence over the whole index and a venue-wide announcement are about; an
    # anomaly on one venue-symbol is about that one. Carried rather than parsed
    # back out of `subject`, because a subject is a sentence for a person to read
    # and deriving a decision from one is how a rename becomes a risk change.
    symbols: tuple[str, ...] = ()


@dataclass
class EventStanding:
    events_registered: int = 0
    events_expired: int = 0
    # Registrations that restated a condition already standing rather than adding
    # one. High is healthy: it is a detector repeating itself, which is what a
    # detector of an ongoing condition does.
    events_restated: int = 0
    limits_issued: int = 0
    smallest_shrink: float = 1.0
    # How many symbols carried a scoped limit on the last read. Zero with events
    # active means every one of them was about the whole market.
    symbols_scoped: int = 0
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
        # Keyed by the condition, not appended. A cause that is still going on is
        # restated by its detector many times a second -- turbulence on every index
        # reading, an anomaly on every trade that disagrees -- and appending each
        # restatement makes one condition into hundreds of overlapping events whose
        # shrinks then multiply. Measured on the live spine at 13:28 on 2026-08-26:
        # 165 active events from 165 registrations in three minutes, 35 of them
        # scoped to symbols and the rest all the same market-wide turbulence, and
        # `smallest_shrink` 2.2e-53. The same reading restated is one condition; the
        # newest statement of it replaces the last, window and all.
        self._events: dict[tuple, RiskEvent] = {}
        self.standing = EventStanding()

    def register_scheduled_event(
        self, subject: str, seconds_until: float, window_seconds: float, shrink_to: float,
        reason: str, symbols=(),
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
                symbols=tuple(symbols),
                shrink_to=shrink_to,
                starts_at_monotonic=now + seconds_until - window_seconds,
                ends_at_monotonic=now + seconds_until + window_seconds,
                decays=False,
                reason=reason,
            )
        )

    def register_anomaly(self, subject: str, shrink_to: float, decay_seconds: float, reason: str, symbols=()) -> RiskEvent:
        """Something unexpected happened; carry less until it stops mattering."""
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=ANOMALY, subject=subject, shrink_to=shrink_to,
                starts_at_monotonic=now, ends_at_monotonic=now + decay_seconds,
                decays=True, reason=reason, symbols=tuple(symbols),
            )
        )

    def register_announcement(self, subject: str, shrink_to: float, seconds: float, reason: str, symbols=()) -> RiskEvent:
        now = self._monotonic()
        return self._register(
            RiskEvent(
                kind=ANNOUNCEMENT, subject=subject, shrink_to=shrink_to,
                starts_at_monotonic=now, ends_at_monotonic=now + seconds,
                decays=False, reason=reason, symbols=tuple(symbols),
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

    def _cause_of(self, event: RiskEvent) -> tuple:
        """What makes two registrations the same ongoing condition.

        The kind, what it is about, and which symbols it binds. Deliberately not
        the reason: a reason carries measured numbers -- "turbulence at 3.71
        against a normal 100.00" -- so keying on it would make every restatement a
        new condition again, which is the defect this key exists to stop.
        """
        return (event.kind, event.subject, event.symbols)

    def _register(self, event: RiskEvent) -> RiskEvent:
        cause = self._cause_of(event)
        if cause in self._events:
            self.standing.events_restated += 1
        self._events[cause] = event
        self.standing.events_registered += 1
        self.standing.active_kinds[event.kind] = self.standing.active_kinds.get(event.kind, 0) + 1
        return event

    def read_limits(self) -> tuple[RiskLimit, ...]:
        """Every limit in force right now, as one statement.

        The whole tuple is this limiter's current word, and the reader tells one
        word from the next by `decided_at_ns` -- so every limit in it is stamped
        once, here, rather than each stamping itself. Stamped separately they
        would differ by a nanosecond or two and read as that many statements, and
        a reader keeping only the newest would hold the last symbol's limit and
        drop the rest.
        """
        now = self._monotonic()
        decided_at_ns = self._now_ns()
        before = len(self._events)
        self._events = {
            cause: event
            for cause, event in self._events.items()
            if event.ends_at_monotonic > now
        }
        self.standing.events_expired += before - len(self._events)
        self.standing.limits_issued += 1

        active = [
            event for event in self._events.values() if event.starts_at_monotonic <= now
        ]
        if not active:
            return (
                RiskLimit(
                    limiter=PART_ID,
                    fraction_of_allotment=self._allowed,
                    reason="no event or anomaly is in force",
                    is_binding=False,
                    decided_at_ns=decided_at_ns,
                ),
            )

        # Compounded per symbol, never once across everything. The shrink is
        # multiplicative on purpose -- a listing during a turbulent hour is
        # riskier than either alone -- but that reasoning is about causes
        # *overlapping on one symbol*. Multiplying every symbol's anomaly into a
        # single unscoped limit is a different operation wearing the same
        # arithmetic, and it does not converge:
        #
        # Measured 2026-08-26. market-anomaly-detector raised 2,899 anomalies of
        # the kind "one venue moved and the others did not" across 100
        # venue-symbols. 636 were active at once, and 636 factors multiplied to a
        # smallest_shrink of 2.7e-237 -- a limit indistinguishable from zero, on
        # every symbol, including the ninety-odd that had no event at all. The
        # sizer refused 1,284 intents with `refused_no_risk_allowed` while the
        # arbiter was forming 479 actionable ones at conviction 0.95.
        #
        # This is the defect halt-enforcer was fixed for on 2026-08-25, in a
        # second part: "two anomalous symbols out of a hundred stopped every
        # trade the system could make, and the only trace was a counter of zero
        # limits issued". RiskLimit.symbols was added for exactly this.
        everywhere = [event for event in active if not event.symbols]
        global_shrink = 1.0
        for event in everywhere:
            global_shrink *= self._shrink_of(event, now)

        by_symbol: dict[str, float] = {}
        reasons: dict[str, list] = {}
        for event in active:
            for symbol in event.symbols:
                by_symbol[symbol] = by_symbol.get(symbol, 1.0) * self._shrink_of(event, now)
                reasons.setdefault(symbol, []).append(event)

        limits = [
            RiskLimit(
                limiter=PART_ID,
                fraction_of_allotment=self._allowed * global_shrink,
                reason=(
                    f"{len(everywhere)} affecting every symbol: "
                    + "; ".join(
                        f"{event.kind} {event.subject} ({event.reason})"
                        for event in everywhere[:3]
                    )
                    if everywhere
                    else "no event affects every symbol"
                ),
                is_binding=self._allowed * global_shrink <= NO_RISK_ALLOWED,
                decided_at_ns=decided_at_ns,
            )
        ]
        smallest = global_shrink
        for symbol, shrink in sorted(by_symbol.items()):
            # The symbol's own causes on top of whatever affects everything, so a
            # symbol with an event is never treated as calmer than the market.
            together = shrink * global_shrink
            smallest = min(smallest, together)
            limits.append(
                RiskLimit(
                    limiter=PART_ID,
                    fraction_of_allotment=self._allowed * together,
                    reason=(
                        f"{len(reasons[symbol])} on {symbol}: "
                        + "; ".join(
                            f"{event.kind} ({event.reason})" for event in reasons[symbol][:3]
                        )
                    ),
                    is_binding=self._allowed * together <= NO_RISK_ALLOWED,
                    decided_at_ns=decided_at_ns,
                    symbols=(symbol,),
                )
            )
        self.standing.smallest_shrink = min(self.standing.smallest_shrink, smallest)
        self.standing.symbols_scoped = len(by_symbol)
        return tuple(limits)

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
            event for event in self._events.values()
            if event.starts_at_monotonic <= now < event.ends_at_monotonic
        )


def describe_events(limiter: EventRiskLimiter) -> dict:
    return {
        "part_id": PART_ID,
        "events_registered": limiter.standing.events_registered,
        "events_expired": limiter.standing.events_expired,
        "active_events": len(limiter.active_events),
        "events_restated": limiter.standing.events_restated,
        "limits_issued": limiter.standing.limits_issued,
        "smallest_shrink": limiter.standing.smallest_shrink,
        "symbols_scoped": limiter.standing.symbols_scoped,
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
        publish_limit(limiter.read_limits())

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

    def register_scheduled_or_announced(subject: str, effective_at_ns, reason: str, symbols=()) -> None:
        if effective_at_ns is not None:
            seconds_until = (effective_at_ns - _time.time_ns()) / 1e9
            if seconds_until + scheduled_window > 0:
                limiter.register_scheduled_event(
                    subject, seconds_until, scheduled_window, scheduled_shrink, reason, symbols
                )
            return
        limiter.register_announcement(
            subject, announcement_shrink, announcement_window, reason, symbols
        )

    def read_events(_limiter) -> None:
        for event in events.payloads():
            subject = ",".join(event.symbols) if event.symbols else event.venue_id
            register_scheduled_or_announced(
                subject, event.effective_at_ns, f"{event.event_type}: {event.title}",
                tuple(event.symbols or ()),
            )
        for announcement in announcements.payloads():
            symbols = getattr(announcement, "symbols", ())
            subject = ",".join(symbols) if symbols else announcement.venue_id
            # `headline` and nothing else: the alternative read here named a field
            # `VenueAnnouncement` has never carried, which is a fallback that could
            # only ever have returned its default.
            headline = announcement.headline
            register_scheduled_or_announced(
                subject, announcement.effective_at_ns, headline, tuple(symbols or ())
            )
        for anomaly in anomalies.payloads():
            if anomaly.is_anomalous:
                limiter.register_anomaly(
                    # The anomaly's own name is part of the subject, because the
                    # subject is what says whether two registrations are the same
                    # ongoing condition. Two different anomalies on one symbol are
                    # two conditions; the same one seen again is one, restated.
                    f"{anomaly.venue_id}:{anomaly.symbol}:{anomaly.anomaly}",
                    anomaly_shrink, anomaly_decay,
                    anomaly.reason,
                    # Scoped to the symbol it was seen on. Unscoped, one anomaly
                    # shrank every symbol's risk, and 636 of them compounded to
                    # 2.7e-237.
                    symbols=(anomaly.symbol,),
                )
        for reading in turbulence.payloads():
            if reading.distance is not None and reading.symbols:
                limiter.observe_turbulence(reading.distance, float(len(reading.symbols)), turbulence_horizon)
        patterns.payloads()

    return run_event_risk_limiter(
        limiter=limiter,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_limit=publish_limits,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
