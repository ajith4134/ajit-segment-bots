"""halt-enforcer: a halt, a refused policy or a human override becomes a zero limit.

The part that makes "stop" mean stop. Every other limiter reasons about market
conditions; this one carries decisions that have already been made -- by the
autonomy policy engine, by a trading halt, or by a person -- into the one number
the sizer actually reads.

**A human override outranks everything, including the system's own reasons to
continue.** That is the point of having one: an operator who says stop is not
offering an opinion for the system to weigh.

**A halt is not cleared by the thing that raised it going quiet.** Halts are
released explicitly, because "the condition stopped being reported" and "the
condition ended" are different, and only one of them is a reason to resume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "halt-enforcer"

PART_DECLARATION = PartDeclaration(
    part_id="halt-enforcer",
    consumes=("trading-halt", "policy-decision", "human-override", "capital-settings-verdict"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HUMAN_OVERRIDE = "human-override"
TRADING_HALT = "trading-halt"
POLICY_REFUSAL = "policy-refusal"
SETTINGS_INVALID = "capital-settings-invalid"

# Which stop outranks which when several are in force. A human's decision is
# first because it is the one nothing in the system is entitled to reason past.
PRECEDENCE = (HUMAN_OVERRIDE, TRADING_HALT, POLICY_REFUSAL, SETTINGS_INVALID)


@dataclass(frozen=True)
class Halt:
    """One reason trading is stopped, and who stopped it."""

    kind: str
    source: str
    reason: str
    raised_at_ns: int


@dataclass
class EnforcerStanding:
    halts_raised: int = 0
    halts_released: int = 0
    limits_issued: int = 0
    zero_limits_issued: int = 0
    active: dict = field(default_factory=dict)


class HaltEnforcer:
    """Holds every active stop and issues the limit that follows from them."""

    def __init__(self, allowed_fraction_when_clear: float, now_ns=time.time_ns) -> None:
        self._allowed = allowed_fraction_when_clear
        self._now_ns = now_ns
        self._halts: dict[str, Halt] = {}
        self.standing = EnforcerStanding()

    def raise_halt(self, kind: str, source: str, reason: str) -> Halt:
        """Stop trading for a stated reason. Raising an active halt refreshes it."""
        if kind not in PRECEDENCE:
            raise ValueError(f"{kind!r} is not a kind of halt this enforcer knows")
        halt = Halt(kind=kind, source=source, reason=reason, raised_at_ns=self._now_ns())
        if kind not in self._halts:
            self.standing.halts_raised += 1
        self._halts[kind] = halt
        self.standing.active = {k: h.reason for k, h in self._halts.items()}
        return halt

    def release_halt(self, kind: str) -> bool:
        """Explicitly lift one halt. Returns whether there was one to lift.

        Explicit because a halt that lapsed when its source went quiet would be
        released by the source crashing, which is the worst possible moment.
        """
        removed = self._halts.pop(kind, None) is not None
        if removed:
            self.standing.halts_released += 1
        self.standing.active = {k: h.reason for k, h in self._halts.items()}
        return removed

    def read_limit(self) -> RiskLimit:
        """Zero while anything is halting, with the highest-precedence reason named."""
        self.standing.limits_issued += 1
        for kind in PRECEDENCE:
            halt = self._halts.get(kind)
            if halt is not None:
                self.standing.zero_limits_issued += 1
                return RiskLimit(
                    limiter=PART_ID,
                    fraction_of_allotment=NO_RISK_ALLOWED,
                    reason=f"{kind} from {halt.source}: {halt.reason}",
                    is_binding=True,
                    decided_at_ns=self._now_ns(),
                )
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=self._allowed,
            reason="nothing is halting trading",
            is_binding=False,
            decided_at_ns=self._now_ns(),
        )

    @property
    def is_halted(self) -> bool:
        return bool(self._halts)

    @property
    def active_halts(self) -> tuple[Halt, ...]:
        return tuple(self._halts[kind] for kind in PRECEDENCE if kind in self._halts)


def describe_halts(enforcer: HaltEnforcer) -> dict:
    return {
        "part_id": PART_ID,
        "is_halted": enforcer.is_halted,
        "active": dict(enforcer.standing.active),
        "halts_raised": enforcer.standing.halts_raised,
        "halts_released": enforcer.standing.halts_released,
        "limits_issued": enforcer.standing.limits_issued,
        "zero_limits_issued": enforcer.standing.zero_limits_issued,
    }


def run_halt_enforcer(
    enforcer: HaltEnforcer, control_socket, read_halt_events, publish_limit,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_halt_events(enforcer)
        publish_limit(enforcer.read_limit())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
