"""live-balance-divergence-watch: alert when the venue's real balance is not what settings promise.

Only meaningful once a segment is live. In paper mode the account is a fiction
this system maintains and comparing it to a venue balance says nothing; the moment
real money is behind it, the settings file becomes a claim about the world that
can be false.

Three ways it goes wrong, and each means something different:

- **The venue holds less than the allocation.** Money was withdrawn, lost
  elsewhere, or never there. Every sizing decision downstream is against capital
  that does not exist.
- **The venue holds more.** Usually harmless, occasionally the sign that a
  segment is trading an account it was not meant to share.
- **The venue cannot be read at all.** Not a divergence -- an absence -- and
  reporting it as agreement would be the worst of the three.

It alerts and does not act. Turning a divergence into a halt is `halt-enforcer`'s
decision from a policy, and a watch that stopped trading on a momentary balance
read failure would be an outage generator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "live-balance-divergence-watch"

PART_DECLARATION = PartDeclaration(
    part_id="live-balance-divergence-watch",
    consumes=("account-balance", "capital-allotment", "money-mode"),
    produces=("alert", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LIVE = "live"
PAPER = "paper"

AGREES = "agrees"
VENUE_HOLDS_LESS = "venue-holds-less-than-allocated"
VENUE_HOLDS_MORE = "venue-holds-more-than-allocated"
UNREADABLE = "venue-balance-unreadable"
NOT_LIVE = "not-live-so-not-compared"

SEVERITY_HIGH = "high"
SEVERITY_LOW = "low"


@dataclass(frozen=True)
class DivergenceReport:
    """What the venue holds against what the settings promised."""

    segment: str
    money_mode: str
    allocated: float | None
    venue_balance: float | None
    difference: float | None
    difference_fraction: float | None
    state: str
    reason: str
    checked_at_ns: int

    @property
    def needs_an_operator(self) -> bool:
        return self.state in (VENUE_HOLDS_LESS, UNREADABLE)


@dataclass(frozen=True)
class Alert:
    source: str
    severity: str
    subject: str
    message: str
    raised_at_ns: int


@dataclass
class WatchStanding:
    checks: int = 0
    divergences: int = 0
    shortfalls: int = 0
    unreadable: int = 0
    alerts_raised: int = 0
    largest_shortfall: float = 0.0
    state: str = NOT_LIVE


class LiveBalanceDivergenceWatch:
    """Compares the venue's balance with the allocation, once real money is behind it."""

    def __init__(self, tolerance_fraction: float, now_ns=time.time_ns) -> None:
        if not 0.0 <= tolerance_fraction < 1.0:
            raise ValueError("the tolerance is a fraction of the allocation in [0, 1)")
        self._tolerance = tolerance_fraction
        self._now_ns = now_ns
        self._mode: dict[str, str] = {}
        self._allocated: dict[str, float] = {}
        self._venue_balance: dict[str, float | None] = {}
        self.standing = WatchStanding()

    def set_money_mode(self, segment: str, mode: str) -> None:
        self._mode[segment] = mode

    def set_allocation(self, segment: str, allocated: float) -> None:
        self._allocated[segment] = allocated

    def observe_venue_balance(self, segment: str, balance: float | None) -> None:
        """The venue's own figure, or None where it could not be read.

        None is kept as None rather than dropped: an unreadable balance is a
        state to report, and treating it as "no news" would report agreement.
        """
        self._venue_balance[segment] = balance

    def check(self, segment: str) -> tuple[DivergenceReport, tuple[Alert, ...]]:
        self.standing.checks += 1
        mode = self._mode.get(segment, PAPER)
        allocated = self._allocated.get(segment)

        if mode != LIVE:
            self.standing.state = NOT_LIVE
            return (
                self._report(
                    segment, mode, allocated, None, NOT_LIVE,
                    "this segment is on paper money; the venue balance is not its account",
                ),
                (),
            )

        if segment not in self._venue_balance or self._venue_balance[segment] is None:
            self.standing.unreadable += 1
            self.standing.state = UNREADABLE
            report = self._report(
                segment, mode, allocated, None, UNREADABLE,
                "the venue balance could not be read while this segment is live; unknown is "
                "not agreement",
            )
            return report, (self._alert(report, SEVERITY_HIGH, "venue balance unreadable while live"),)

        balance = self._venue_balance[segment]
        if allocated is None:
            return (
                self._report(
                    segment, mode, None, balance, UNREADABLE,
                    "no allocation has been read for this segment; nothing to compare against",
                ),
                (),
            )

        difference = balance - allocated
        fraction = abs(difference) / allocated if allocated > 0 else 0.0

        if fraction <= self._tolerance:
            self.standing.state = AGREES
            return (
                self._report(
                    segment, mode, allocated, balance, AGREES,
                    f"the venue holds {balance:,.2f} against {allocated:,.2f} allocated, "
                    f"inside the {self._tolerance:.1%} tolerance",
                ),
                (),
            )

        self.standing.divergences += 1
        if difference < 0:
            self.standing.shortfalls += 1
            self.standing.largest_shortfall = max(self.standing.largest_shortfall, -difference)
            self.standing.state = VENUE_HOLDS_LESS
            report = self._report(
                segment, mode, allocated, balance, VENUE_HOLDS_LESS,
                f"the venue holds {balance:,.2f}, {-difference:,.2f} less than the "
                f"{allocated:,.2f} allocated; every sizing decision is against capital "
                f"that is not there",
            )
            return report, (self._alert(report, SEVERITY_HIGH, "the venue holds less than allocated"),)

        self.standing.state = VENUE_HOLDS_MORE
        report = self._report(
            segment, mode, allocated, balance, VENUE_HOLDS_MORE,
            f"the venue holds {balance:,.2f}, {difference:,.2f} more than the {allocated:,.2f} "
            f"allocated; usually harmless, occasionally a shared account",
        )
        return report, (self._alert(report, SEVERITY_LOW, "the venue holds more than allocated"),)

    def _report(self, segment, mode, allocated, balance, state, reason) -> DivergenceReport:
        difference = (balance - allocated) if (balance is not None and allocated is not None) else None
        return DivergenceReport(
            segment=segment,
            money_mode=mode,
            allocated=allocated,
            venue_balance=balance,
            difference=difference,
            difference_fraction=(
                abs(difference) / allocated if difference is not None and allocated else None
            ),
            state=state,
            reason=reason,
            checked_at_ns=self._now_ns(),
        )

    def _alert(self, report: DivergenceReport, severity: str, subject: str) -> Alert:
        self.standing.alerts_raised += 1
        return Alert(
            source=PART_ID,
            severity=severity,
            subject=subject,
            message=f"{report.segment}: {report.reason}",
            raised_at_ns=self._now_ns(),
        )


def describe_divergence(watch: LiveBalanceDivergenceWatch) -> dict:
    return {
        "part_id": PART_ID,
        "state": watch.standing.state,
        "checks": watch.standing.checks,
        "divergences": watch.standing.divergences,
        "shortfalls": watch.standing.shortfalls,
        "unreadable": watch.standing.unreadable,
        "alerts_raised": watch.standing.alerts_raised,
        "largest_shortfall": watch.standing.largest_shortfall,
    }


def run_live_balance_divergence_watch(
    watch: LiveBalanceDivergenceWatch, control_socket, read_balances, publish,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        segments = read_balances(watch)
        for segment in segments:
            publish(*watch.check(segment))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_divergence(watch),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    balances = Batch(read=context.bus.reader("account-balance"))
    allotments = Batch(read=context.bus.reader("capital-allotment"))
    modes = Batch(read=context.bus.reader("money-mode"))
    publish_alerts = context.bus.publisher_for("alert")
    watch = LiveBalanceDivergenceWatch(tolerance_fraction=context.number("live_balance_tolerance_fraction"))

    def read_balances(_watch):
        touched = set()
        for mode in modes.payloads():
            watch.set_money_mode(mode.segment, mode.mode)
            touched.add(mode.segment)
        for allotment in allotments.payloads():
            watch.set_allocation(allotment.segment, allotment.allotted)
            touched.add(allotment.segment)
        for balance in balances.payloads():
            segment = getattr(balance, "segment", None)
            if segment is None:
                continue  # a venue balance names a venue, not a segment; the keeper's carries one
            watch.observe_venue_balance(segment, getattr(balance, "equity", None))
            touched.add(segment)
        return tuple(sorted(touched))

    def publish(verdict, alerts) -> None:
        if alerts:
            publish_alerts(tuple(alerts))

    return run_live_balance_divergence_watch(
        watch=watch,
        control_socket=context.control_socket,
        read_balances=read_balances,
        publish=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
