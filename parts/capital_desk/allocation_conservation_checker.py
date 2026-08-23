"""allocation-conservation-checker: alert when more is allocated than exists.

One number cannot be in two places. If three segments are each allocated 40% of
the main balance, every one of them will size trades believing it has capital
that another segment has already committed -- and each of them is individually
correct. The overrun only appears when they trade at once, which is exactly when
it is most expensive.

Nothing here stops trading. It alerts, and the alert names the shortfall, because
the fix is an operator editing the settings file and only they can decide which
segment gives up its share.

**Reserved but unallocated headroom is reported too.** An operator who has
allocated 30% of the account is not making a mistake, but they are almost
certainly not doing what they intended either, and nothing else in the system
would ever mention it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "allocation-conservation-checker"

PART_DECLARATION = PartDeclaration(
    part_id="allocation-conservation-checker",
    consumes=("main-account-setting", "capital-allotment"),
    produces=("allocation-headroom", "alert", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CONSERVED = "conserved"
OVER_ALLOCATED = "over-allocated"
UNDER_ALLOCATED = "under-allocated"
NO_BALANCE = "no-main-balance"

ALERT_SEVERITY_HIGH = "high"
ALERT_SEVERITY_LOW = "low"


@dataclass(frozen=True)
class AllocationHeadroom:
    """What the account holds against what has been promised out of it."""

    main_balance: float
    total_allocated: float
    headroom: float
    headroom_fraction: float
    state: str
    segments: dict
    reason: str
    checked_at_ns: int

    @property
    def is_conserved(self) -> bool:
        return self.state in (CONSERVED, UNDER_ALLOCATED)


@dataclass(frozen=True)
class Alert:
    """Something an operator needs to see, with what it would cost to ignore."""

    source: str
    severity: str
    subject: str
    message: str
    raised_at_ns: int


@dataclass
class ConservationStanding:
    checks: int = 0
    over_allocations: int = 0
    alerts_raised: int = 0
    worst_overrun: float = 0.0
    segments_known: int = 0
    state: str = CONSERVED


class AllocationConservationChecker:
    """Sums every segment's allocation against the main balance and alerts on a breach."""

    def __init__(self, under_allocation_alert_fraction: float, now_ns=time.time_ns) -> None:
        if not 0.0 <= under_allocation_alert_fraction < 1.0:
            raise ValueError("the under-allocation threshold is a fraction of the balance in [0, 1)")
        self._under_threshold = under_allocation_alert_fraction
        self._now_ns = now_ns
        self._allocations: dict[str, float] = {}
        self._main_balance: float | None = None
        self.standing = ConservationStanding()

    def observe_main_balance(self, balance: float) -> None:
        self._main_balance = balance

    def observe_segment_allocation(self, segment: str, allotted: float) -> None:
        self._allocations[segment] = allotted
        self.standing.segments_known = len(self._allocations)

    def check(self) -> tuple[AllocationHeadroom, tuple[Alert, ...]]:
        """The headroom, and any alert an operator needs to act on."""
        self.standing.checks += 1
        total = sum(self._allocations.values())

        if self._main_balance is None:
            headroom = self._headroom(0.0, total, NO_BALANCE, "no main balance has been read yet")
            return headroom, ()

        balance = self._main_balance
        remaining = balance - total

        if balance <= 0:
            # Zero is how this ships, and allocating against it is not an error
            # to alert on -- it is a system that has not been told to trade.
            headroom = self._headroom(
                balance, total, NO_BALANCE,
                "the main balance is zero; no segment can size a real trade",
            )
            return headroom, ()

        if remaining < 0:
            self.standing.over_allocations += 1
            self.standing.worst_overrun = max(self.standing.worst_overrun, -remaining)
            self.standing.state = OVER_ALLOCATED
            headroom = self._headroom(
                balance, total, OVER_ALLOCATED,
                f"{total:,.2f} is allocated across {len(self._allocations)} segment(s) against a "
                f"balance of {balance:,.2f}: {-remaining:,.2f} more than exists",
            )
            alert = Alert(
                source=PART_ID,
                severity=ALERT_SEVERITY_HIGH,
                subject="allocation exceeds the main balance",
                message=(
                    f"{headroom.reason}. Each segment will size trades believing it has capital "
                    f"another segment has already committed, and every one of them is individually "
                    f"correct. Reduce an allocation in the settings file."
                ),
                raised_at_ns=self._now_ns(),
            )
            self.standing.alerts_raised += 1
            return headroom, (alert,)

        fraction = remaining / balance
        if fraction > self._under_threshold:
            self.standing.state = UNDER_ALLOCATED
            headroom = self._headroom(
                balance, total, UNDER_ALLOCATED,
                f"{fraction:.0%} of the balance is unallocated ({remaining:,.2f} of {balance:,.2f})",
            )
            alert = Alert(
                source=PART_ID,
                severity=ALERT_SEVERITY_LOW,
                subject="most of the account is unallocated",
                message=(
                    f"{headroom.reason}. Not an error, but probably not what was intended, "
                    f"and nothing else in this system would mention it."
                ),
                raised_at_ns=self._now_ns(),
            )
            self.standing.alerts_raised += 1
            return headroom, (alert,)

        self.standing.state = CONSERVED
        return (
            self._headroom(
                balance, total, CONSERVED,
                f"{total:,.2f} of {balance:,.2f} allocated, {remaining:,.2f} free",
            ),
            (),
        )

    def _headroom(self, balance, total, state, reason) -> AllocationHeadroom:
        return AllocationHeadroom(
            main_balance=balance,
            total_allocated=total,
            headroom=balance - total,
            headroom_fraction=(balance - total) / balance if balance else 0.0,
            state=state,
            segments=dict(sorted(self._allocations.items())),
            reason=reason,
            checked_at_ns=self._now_ns(),
        )


def describe_conservation(checker: AllocationConservationChecker) -> dict:
    return {
        "part_id": PART_ID,
        "state": checker.standing.state,
        "checks": checker.standing.checks,
        "segments_known": checker.standing.segments_known,
        "over_allocations": checker.standing.over_allocations,
        "worst_overrun": checker.standing.worst_overrun,
        "alerts_raised": checker.standing.alerts_raised,
    }


def run_allocation_conservation_checker(
    checker: AllocationConservationChecker, control_socket, read_allocations, publish,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_allocations(checker)
        publish(*checker.check())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
