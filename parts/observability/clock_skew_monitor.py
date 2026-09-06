"""clock-skew-monitor: catch timestamp drift before every private order fails.

Both venues reject a signed request whose timestamp is too far from their server
clock. The failure is quiet at first -- an occasional rejection that looks like
bad luck -- and then total: once drift passes the window, every private order
fails and the system is unable to trade while appearing entirely healthy.

The signature is what makes it catchable early: rejections that **recur and
trend**. One is noise; a rising rate over a widening offset is a clock walking
away, and there is a window between the first rejection and the last successful
order in which a person can fix it.

Drift is measured from the venue's own timestamps where they arrive, because a
local clock cannot detect that it is the one that is wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "clock-skew-monitor"
# The venue whose clock this watches by default. Named rather than taken from a
# message, so a reading is attributable to a broker even when the offset is what
# is wrong with it.
BROKER_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="clock-skew-monitor",
    consumes=("broker-market-data", "raw-venue-order-status"),
    produces=("alert", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

IN_STEP = "in-step"
DRIFTING = "drifting"
REJECTING = "rejecting"

SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"

# Phrases the venues use when a request's timestamp is outside their window.
# Wire vocabulary: what they mean is fixed by the venue.
SKEW_REJECTION_PHRASES = (
    "timestamp for this request",
    "recvwindow",
    "recv_window",
    "invalid timestamp",
    "req_timestamp",
    "ahead of the server",
    "outside of the recvwindow",
)


@dataclass(frozen=True)
class SkewReading:
    """How far this machine's clock is from a venue's, and what it is costing."""

    venue_id: str
    state: str
    offset_seconds: float | None
    rejections_in_window: int
    total_rejections: int
    reason: str
    observed_at_ns: int

    @property
    def needs_attention(self) -> bool:
        return self.state in (DRIFTING, REJECTING)


@dataclass(frozen=True)
class Alert:
    source: str
    severity: str
    subject: str
    message: str
    proof: str
    raised_at_ns: int


@dataclass
class MonitorStanding:
    statuses_seen: int = 0
    skew_rejections: int = 0
    venues_watched: int = 0
    largest_offset_seconds: float = 0.0
    alerts_raised: int = 0


class ClockSkewMonitor:
    """Watches offsets and skew rejections per venue, and warns before they become total."""

    def __init__(
        self,
        drift_warning_seconds: float,
        rejections_before_alert: int,
        window_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if drift_warning_seconds <= 0 or rejections_before_alert < 1:
            raise ValueError("a warning needs both a drift threshold and a rejection count")
        self._drift_warning = drift_warning_seconds
        self._rejections_before_alert = rejections_before_alert
        self._window = window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._offsets: dict[str, float] = {}
        self._rejections: dict[str, list[float]] = {}
        self._totals: dict[str, int] = {}
        self.standing = MonitorStanding()

    def observe_venue_time(self, venue_id: str, venue_time_ns: int, local_time_ns: int) -> None:
        """The offset between this machine and the venue, from the venue's own stamp."""
        offset = (local_time_ns - venue_time_ns) / 1e9
        self._offsets[venue_id] = offset
        self.standing.venues_watched = len(set(self._offsets) | set(self._rejections))
        self.standing.largest_offset_seconds = max(
            self.standing.largest_offset_seconds, abs(offset)
        )

    def observe_status(self, venue_id: str, message: str) -> bool:
        """One venue reply. Returns whether it was a timestamp rejection."""
        self.standing.statuses_seen += 1
        lowered = (message or "").lower()
        if not any(phrase in lowered for phrase in SKEW_REJECTION_PHRASES):
            return False

        self.standing.skew_rejections += 1
        self._rejections.setdefault(venue_id, []).append(self._monotonic())
        self._totals[venue_id] = self._totals.get(venue_id, 0) + 1
        self.standing.venues_watched = len(set(self._offsets) | set(self._rejections))
        return True

    def read(self, venue_id: str) -> SkewReading:
        now = self._monotonic()
        recent = [at for at in self._rejections.get(venue_id, []) if now - at < self._window]
        self._rejections[venue_id] = recent
        offset = self._offsets.get(venue_id)
        total = self._totals.get(venue_id, 0)

        if len(recent) >= self._rejections_before_alert:
            return self._reading(
                venue_id, REJECTING, offset, len(recent), total,
                f"{len(recent)} timestamp rejections in the last {self._window:.0f}s"
                + (f"; the clock is {offset:+.3f}s from the venue" if offset is not None else "")
                + "; once drift passes the venue's window every private order fails",
            )

        if offset is not None and abs(offset) >= self._drift_warning:
            return self._reading(
                venue_id, DRIFTING, offset, len(recent), total,
                f"the clock is {offset:+.3f}s from {venue_id}, past the {self._drift_warning:.3f}s "
                f"warning; there is a window to fix this before orders start failing",
            )

        return self._reading(
            venue_id, IN_STEP, offset, len(recent), total,
            f"the clock is {offset:+.3f}s from {venue_id}" if offset is not None
            else "no venue timestamp has been observed yet",
        )

    def alert_for(self, reading: SkewReading) -> Alert | None:
        if not reading.needs_attention:
            return None
        self.standing.alerts_raised += 1
        return Alert(
            source=PART_ID,
            severity=SEVERITY_CRITICAL if reading.state == REJECTING else SEVERITY_HIGH,
            subject=f"clock skew against {reading.venue_id}",
            message=reading.reason,
            proof=(
                f"offset {reading.offset_seconds if reading.offset_seconds is not None else 'unknown'}, "
                f"{reading.rejections_in_window} rejections in window, "
                f"{reading.total_rejections} in total"
            ),
            raised_at_ns=self._now_ns(),
        )

    def read_all(self) -> tuple[SkewReading, ...]:
        venues = sorted(set(self._offsets) | set(self._rejections))
        return tuple(self.read(venue_id) for venue_id in venues)

    def _reading(self, venue_id, state, offset, recent, total, reason) -> SkewReading:
        return SkewReading(
            venue_id=venue_id, state=state, offset_seconds=offset,
            rejections_in_window=recent, total_rejections=total,
            reason=reason, observed_at_ns=self._now_ns(),
        )


def describe_skew(monitor: ClockSkewMonitor) -> dict:
    return {
        "part_id": PART_ID,
        "statuses_seen": monitor.standing.statuses_seen,
        "skew_rejections": monitor.standing.skew_rejections,
        "venues_watched": monitor.standing.venues_watched,
        "largest_offset_seconds": monitor.standing.largest_offset_seconds,
        "alerts_raised": monitor.standing.alerts_raised,
        "readings": [reading.__dict__ for reading in monitor.read_all()],
    }


def run_clock_skew_monitor(
    monitor: ClockSkewMonitor, control_socket, read_statuses, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_statuses(monitor)
        alerts = [monitor.alert_for(reading) for reading in monitor.read_all()]
        publish_alerts(tuple(alert for alert in alerts if alert is not None))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_skew(monitor),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Two readings, and they answer different halves of the same question.

    **The broker's own stamp on every price**, which is the one that carries
    today: `broker-market-data` arrives thousands of times a second with
    `broker_time_ns` on it, and the distance between that and this machine's
    clock is the offset. Added 2026-09-06 -- until then this part consumed only
    `raw-venue-order-status`, whose producers are both crypto and both off, so
    the one thing that would notice this clock drifting had never run. Two
    timestamp traps were found by hand that same day (Upstox's +05:30 historical
    rows, NSE's IST-written-as-an-epoch intraday chart) and nothing running would
    have caught either.

    **A venue saying the timestamp is wrong**, which is sharper and carries
    nothing yet. `raw-venue-order-status` is kept rather than dropped: a
    rejection naming a clock is the strongest evidence there is, the day a real
    order path exists.
    """
    import time as _time

    from runtime.input_assembly import Batch

    prices = Batch(read=context.bus.reader("broker-market-data"))
    statuses = Batch(read=context.bus.reader("raw-venue-order-status"))
    publish_alerts = context.bus.publisher_for("alert")
    monitor = ClockSkewMonitor(
        drift_warning_seconds=context.number("clock_drift_warning"),
        rejections_before_alert=int(context.number("clock_rejections_before_alert")),
        window_seconds=context.number("clock_rejection_window"),
    )

    def read_statuses(_monitor) -> None:
        for update in prices.payloads():
            # The broker's stamp against the moment this machine looked. Read
            # per message rather than once a tick: the offset is what is being
            # measured, and a tick's own duration would be folded into it.
            broker_time_ns = getattr(update, "broker_time_ns", None)
            if broker_time_ns:
                monitor.observe_venue_time(
                    BROKER_VENUE_ID, int(broker_time_ns), _time.time_ns(),
                )
        for status in statuses.payloads():
            response = status.venue_response if isinstance(status.venue_response, dict) else {}
            venue_time = response.get("serverTime") or response.get("time") or response.get("ts")
            if venue_time is not None and status.responded_at_ns is not None:
                monitor.observe_venue_time(status.venue_id, int(venue_time) * 1_000_000, status.responded_at_ns)
            monitor.observe_status(status.venue_id, status.reason)

    def publish(alerts) -> None:
        if alerts:
            publish_alerts(alerts)

    return run_clock_skew_monitor(
        monitor=monitor,
        control_socket=context.control_socket,
        read_statuses=read_statuses,
        publish_alerts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
