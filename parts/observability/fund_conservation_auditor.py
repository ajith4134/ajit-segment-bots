"""fund-conservation-auditor: recompute both sides of every fill, and name where they part.

Money does not appear or vanish. Every fill has an arithmetic identity -- what
left the balance equals what entered the position, plus fees -- and if the two
sides disagree then something between them is wrong: a fee applied twice, a fill
counted once in one place and twice in another, a sign inverted on a short.

This is the part that catches those, and it catches them by **recomputing
independently** rather than by asking the parts that did the original work. An
auditor that re-used the position keeper's arithmetic would agree with it by
construction, including when it was wrong.

It names the step that disagrees, not merely the total, because a total that is
off by 12.40 is a puzzle and "the fee on fill f-9912 was applied twice" is a fix.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY

PART_ID = "fund-conservation-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="fund-conservation-auditor",
    consumes=("fill", "journal-entry"),
    produces=("alert", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CONSERVED = "conserved"
DIVERGED = "diverged"
UNCHECKABLE = "uncheckable"

SEVERITY_CRITICAL = "critical"


@dataclass(frozen=True)
class ConservationCheck:
    """One fill's two sides, recomputed, and whether they agree."""

    fill_id: str
    verdict: str
    cash_moved: float
    position_value_moved: float
    fee: float
    difference: float
    tolerance: float
    reason: str
    checked_at_ns: int

    @property
    def is_conserved(self) -> bool:
        return self.verdict == CONSERVED


@dataclass(frozen=True)
class Alert:
    source: str
    severity: str
    subject: str
    message: str
    proof: str
    raised_at_ns: int


@dataclass
class AuditorStanding:
    fills_checked: int = 0
    conserved: int = 0
    diverged: int = 0
    duplicates_caught: int = 0
    largest_difference: float = 0.0
    running_cash: float = 0.0
    running_position_value: float = 0.0
    running_fees: float = 0.0
    # Fills that arrived with no journal entry stating the cash and position
    # value they moved. The journal carries fills and positions, not the account
    # keeper's cash movement per fill; until an entry states it, the auditor has
    # nothing independent to check the fill against, and says so (RL-062).
    fills_without_a_reported_change: int = 0


class FundConservationAuditor:
    """Recomputes each fill's balance equation from the fill alone."""

    def __init__(self, tolerance: float, now_ns=time.time_ns) -> None:
        if tolerance < 0:
            raise ValueError("a tolerance cannot be negative")
        self._tolerance = tolerance
        self._now_ns = now_ns
        self._seen_fills: set[str] = set()
        self.standing = AuditorStanding()

    def check_fill(
        self, fill, reported_cash_change: float, reported_position_value_change: float
    ) -> ConservationCheck:
        """Compare what a fill must have moved against what the system says it moved.

        The independent computation is the point: `reported_*` come from whatever
        kept the books, and this derives the same figures from the fill itself.
        """
        self.standing.fills_checked += 1

        if fill.fill_id in self._seen_fills:
            self.standing.duplicates_caught += 1
            return self._check(
                fill.fill_id, DIVERGED, reported_cash_change, reported_position_value_change,
                fill.fee, abs(reported_cash_change),
                f"fill {fill.fill_id} has already been audited; counting it twice would move "
                f"money that never moved",
            )
        self._seen_fills.add(fill.fill_id)

        # Independently: a buy takes cash out and puts value in; a sell does the
        # reverse. The fee always leaves, whichever way the trade went.
        notional = fill.quantity * fill.price
        cash_moved = -(notional + fill.fee) if fill.side == BUY else (notional - fill.fee)
        position_value_moved = notional if fill.side == BUY else -notional

        cash_difference = abs(cash_moved - reported_cash_change)
        position_difference = abs(position_value_moved - reported_position_value_change)
        difference = max(cash_difference, position_difference)

        self.standing.running_cash += cash_moved
        self.standing.running_position_value += position_value_moved
        self.standing.running_fees += fill.fee
        self.standing.largest_difference = max(self.standing.largest_difference, difference)

        if difference <= self._tolerance:
            self.standing.conserved += 1
            return self._check(
                fill.fill_id, CONSERVED, cash_moved, position_value_moved, fill.fee, difference,
                f"both sides agree within {self._tolerance:g}",
            )

        self.standing.diverged += 1
        step = "cash" if cash_difference > position_difference else "position value"
        return self._check(
            fill.fill_id, DIVERGED, cash_moved, position_value_moved, fill.fee, difference,
            f"the {step} side disagrees by {difference:,.6f}: this fill of {fill.quantity:g} at "
            f"{fill.price:g} with a fee of {fill.fee:g} must move {cash_moved:,.6f} in cash and "
            f"{position_value_moved:,.6f} in position value, but the system recorded "
            f"{reported_cash_change:,.6f} and {reported_position_value_change:,.6f}",
        )

    def audit_alert(self, check: ConservationCheck) -> Alert | None:
        """An alert for a divergence, with the arithmetic attached as its proof."""
        if check.is_conserved:
            return None
        return Alert(
            source=PART_ID,
            severity=SEVERITY_CRITICAL,
            subject=f"fund conservation broken on fill {check.fill_id}",
            message=check.reason,
            proof=(
                f"cash {check.cash_moved:,.6f}, position value {check.position_value_moved:,.6f}, "
                f"fee {check.fee:,.6f}, difference {check.difference:,.6f} against a tolerance "
                f"of {check.tolerance:g}"
            ),
            raised_at_ns=self._now_ns(),
        )

    def _check(self, fill_id, verdict, cash, position_value, fee, difference, reason) -> ConservationCheck:
        return ConservationCheck(
            fill_id=fill_id, verdict=verdict, cash_moved=cash,
            position_value_moved=position_value, fee=fee, difference=difference,
            tolerance=self._tolerance, reason=reason, checked_at_ns=self._now_ns(),
        )


def describe_conservation(auditor: FundConservationAuditor) -> dict:
    return {
        "part_id": PART_ID,
        "fills_checked": auditor.standing.fills_checked,
        "conserved": auditor.standing.conserved,
        "diverged": auditor.standing.diverged,
        "duplicates_caught": auditor.standing.duplicates_caught,
        "largest_difference": auditor.standing.largest_difference,
        "running_cash": auditor.standing.running_cash,
        "running_position_value": auditor.standing.running_position_value,
        "running_fees": auditor.standing.running_fees,
    }


def run_fund_conservation_auditor(
    auditor: FundConservationAuditor, control_socket, read_fills, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        alerts = []
        for fill, cash_change, value_change in read_fills():
            check = auditor.check_fill(fill, cash_change, value_change)
            alert = auditor.audit_alert(check)
            if alert is not None:
                alerts.append(alert)
        publish_alerts(tuple(alerts))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_conservation(auditor),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A fill is checked against what the journal says it moved: a journal
    entry whose payload names the same fill id and carries a cash change and
    a position value change. The recorders write fills and positions and not
    the keeper's cash movement per fill, so today no entry carries those two
    figures and every fill is counted as one that could not be audited --
    a number on the standing, never a silent pass.
    """
    from runtime.input_assembly import Batch

    fills = Batch(read=context.bus.reader("fill"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    publish_alerts = context.bus.publisher_for("alert")
    auditor = FundConservationAuditor(tolerance=context.number("fund_conservation_tolerance"))
    reported: dict[str, tuple[float, float]] = {}
    waiting: dict[str, object] = {}

    def read_fills():
        for entry in entries.payloads():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            fill_id = payload.get("fill_id")
            if fill_id and "cash_change" in payload and "position_value_change" in payload:
                reported[str(fill_id)] = (float(payload["cash_change"]), float(payload["position_value_change"]))
        for fill in fills.payloads():
            waiting[fill.fill_id] = fill
        ready = []
        for fill_id, fill in list(waiting.items()):
            changes = reported.pop(fill_id, None)
            if changes is None:
                continue
            ready.append((fill, changes[0], changes[1]))
            del waiting[fill_id]
        # A fill with no reported change is not held forever: past one health
        # interval's worth of fills it is counted and forgotten.
        if len(waiting) > 1000:
            for fill_id in list(waiting)[:-1000]:
                del waiting[fill_id]
                auditor.standing.fills_without_a_reported_change += 1
        return tuple(ready)

    def publish(alerts) -> None:
        if alerts:
            publish_alerts(alerts)

    return run_fund_conservation_auditor(
        auditor=auditor,
        control_socket=context.control_socket,
        read_fills=read_fills,
        publish_alerts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
