"""drawdown-breaker: cut the limit to zero when drawdown breaches its floor.

The last line before a losing run becomes an account-ending one. Everything else
in this block shrinks risk; this one stops it.

Drawdown is measured from the **high-water mark of realised equity**, not from
the starting balance. Measuring from the start would mean a system that doubled
the account could then lose all of the gain without tripping anything, which is
precisely the run that matters most.

The breaker is **sticky**: once tripped it stays tripped until equity recovers a
stated fraction of what was lost, not merely until it stops falling. Without that
it releases at the bottom of every dip and re-trips on the next tick, and a
system that re-enters at each low is worse than one that stopped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "drawdown-breaker"

PART_DECLARATION = PartDeclaration(
    part_id="drawdown-breaker",
    consumes=("account-balance", "closed-trade", "drawdown-episode"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TRADING = "trading"
TRIPPED = "tripped"
RECOVERING = "recovering"


@dataclass
class BreakerStanding:
    balances_seen: int = 0
    trips: int = 0
    releases: int = 0
    high_water_mark: float = 0.0
    lowest_equity: float | None = None
    deepest_drawdown: float = 0.0
    state: str = TRADING
    tripped_at_equity: float | None = None


class DrawdownBreaker:
    """Zeroes the risk limit once equity falls a stated fraction below its peak."""

    def __init__(
        self,
        maximum_drawdown_fraction: float,
        recovery_fraction: float,
        allowed_fraction_when_trading: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_drawdown_fraction < 1.0:
            raise ValueError("a drawdown floor must be a fraction of equity between 0 and 1")
        if not 0.0 < recovery_fraction <= 1.0:
            raise ValueError("recovery must be a fraction of what was lost, above 0")
        self._maximum_drawdown = maximum_drawdown_fraction
        self._recovery = recovery_fraction
        self._allowed = allowed_fraction_when_trading
        self._now_ns = now_ns
        self.standing = BreakerStanding()

    def observe_equity(self, equity: float) -> RiskLimit:
        """One equity reading; returns the limit that follows from it."""
        self.standing.balances_seen += 1
        if equity > self.standing.high_water_mark:
            self.standing.high_water_mark = equity
        if self.standing.lowest_equity is None or equity < self.standing.lowest_equity:
            self.standing.lowest_equity = equity

        peak = self.standing.high_water_mark
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        self.standing.deepest_drawdown = max(self.standing.deepest_drawdown, drawdown)

        if self.standing.state == TRIPPED:
            return self._while_tripped(equity, peak, drawdown)

        if drawdown >= self._maximum_drawdown:
            self.standing.state = TRIPPED
            self.standing.trips += 1
            self.standing.tripped_at_equity = equity
            return self._limit(
                NO_RISK_ALLOWED,
                f"equity is {drawdown:.1%} below its high-water mark of {peak:,.2f}, "
                f"past the {self._maximum_drawdown:.1%} floor",
            )

        return self._limit(
            self._allowed,
            f"drawdown {drawdown:.1%} of a {self._maximum_drawdown:.1%} floor",
        )

    def _while_tripped(self, equity: float, peak: float, drawdown: float) -> RiskLimit:
        """Stay stopped until enough of the loss is recovered, not merely until it stops."""
        tripped_at = self.standing.tripped_at_equity or equity
        lost = peak - tripped_at
        recovered = (equity - tripped_at) / lost if lost > 0 else 1.0

        if recovered >= self._recovery:
            self.standing.state = TRADING
            self.standing.releases += 1
            self.standing.tripped_at_equity = None
            return self._limit(
                self._allowed,
                f"recovered {recovered:.0%} of the drawdown, past the {self._recovery:.0%} needed",
            )

        self.standing.state = TRIPPED
        return self._limit(
            NO_RISK_ALLOWED,
            f"stopped after a {drawdown:.1%} drawdown; {recovered:.0%} recovered of the "
            f"{self._recovery:.0%} needed to resume",
        )

    def _limit(self, fraction: float, reason: str) -> RiskLimit:
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=fraction,
            reason=reason,
            is_binding=fraction <= NO_RISK_ALLOWED,
            decided_at_ns=self._now_ns(),
        )

    @property
    def is_tripped(self) -> bool:
        return self.standing.state == TRIPPED


def describe_drawdown(breaker: DrawdownBreaker) -> dict:
    return {
        "part_id": PART_ID,
        "state": breaker.standing.state,
        "balances_seen": breaker.standing.balances_seen,
        "trips": breaker.standing.trips,
        "releases": breaker.standing.releases,
        "high_water_mark": breaker.standing.high_water_mark,
        "lowest_equity": breaker.standing.lowest_equity,
        "deepest_drawdown": breaker.standing.deepest_drawdown,
    }


def run_drawdown_breaker(
    breaker: DrawdownBreaker, control_socket, read_equity, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        equity = read_equity()
        if equity is not None:
            publish_limit(breaker.observe_equity(equity))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Equity is read off the segment's account balance as the account keeper
    publishes it. closed-trade and drawdown-episode are declared and read so
    the breaker is woken by the events that move equity, but the number it
    judges is the keeper's equity: one source of truth for what the account is
    worth, not a second one summed here.
    """
    from runtime.input_assembly import Batch, LatestByKey

    balances = LatestByKey(read=context.bus.reader("account-balance"), key_of=lambda b: b.segment)
    closed = Batch(read=context.bus.reader("closed-trade"))
    episodes = Batch(read=context.bus.reader("drawdown-episode"))
    publish_limits = context.bus.publisher_for("risk-limit")
    segment = str(context.setting("segment_id").value)
    breaker = DrawdownBreaker(
        maximum_drawdown_fraction=context.number("risk_maximum_drawdown_fraction"),
        recovery_fraction=context.number("risk_drawdown_recovery_fraction"),
        allowed_fraction_when_trading=context.number("risk_allowed_fraction_when_clear"),
    )

    def read_equity():
        closed.payloads()
        episodes.payloads()
        balance = balances.mapping().get(segment)
        return None if balance is None else balance.equity

    return run_drawdown_breaker(
        breaker=breaker,
        control_socket=context.control_socket,
        read_equity=read_equity,
        publish_limit=lambda limit: publish_limits((limit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
