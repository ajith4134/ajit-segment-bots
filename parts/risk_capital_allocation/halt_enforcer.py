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
from runtime.risk_types import EVERY_SYMBOL, NO_RISK_ALLOWED, RiskLimit

PART_ID = "halt-enforcer"

PART_DECLARATION = PartDeclaration(
    part_id="halt-enforcer",
    consumes=("capital-settings-verdict", "human-override", "instrument-restriction",
              "policy-decision", "trading-halt"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HUMAN_OVERRIDE = "human-override"
TRADING_HALT = "trading-halt"
POLICY_REFUSAL = "policy-refusal"
SETTINGS_INVALID = "capital-settings-invalid"
# The exchange's own restriction on named instruments: an F&O ban, or an ASM
# surveillance stage. Unlike every other kind here it is scoped to symbols
# rather than to everything, and it is released by absence rather than by an
# explicit release -- NSE publishes no un-ban, a lifted name simply stops
# appearing in fo_secban.csv.
INSTRUMENT_RESTRICTION = "instrument-restriction"

# The instructions human-override-reader actually publishes (its own
# INSTRUCTIONS tuple) that mean trading itself must stop. Named locally
# rather than imported from that part (T-4: a part names data, never
# another part) -- `position_flattener.py` names its own `CLOSE_POSITIONS`
# the same way.
#
# Until 2026-08-30 this checked `"halt" in override.instruction.lower()`,
# and none of the five real instructions -- stop-everything, stop-trading,
# stop-self-modification, close-positions, resume -- contain the word
# "halt". So an operator's stop-trading override, the system's own
# emergency door, published and read correctly all the way to this part
# and then enforced nothing: risk-limit never went to zero, and the sizer
# went on producing new orders under an active stop.
STOP_EVERYTHING = "stop-everything"
STOP_TRADING = "stop-trading"
# Closing every position and opening new ones are not two operations an
# operator asking for the first would want running at once: measured
# 2026-08-30, running `close-positions` alone let the bots open 71 new
# positions across other symbols while position-flattener worked through
# the original 50, because nothing about "close everything" also meant
# "stop opening things" until this line existed.
CLOSE_POSITIONS = "close-positions"
HALTING_INSTRUCTIONS = (STOP_EVERYTHING, STOP_TRADING, CLOSE_POSITIONS)


def wants_halt(instruction: str) -> bool:
    """Whether a human-override instruction means trading itself must stop.

    A named, tested function rather than inline logic: the previous
    (`"halt" in instruction.lower()`) version lived only inside `start_part`'s
    closure, which is why no test caught it matching none of the five real
    instructions for over four days on the live spine.
    """
    return instruction in HALTING_INSTRUCTIONS

# Which stop outranks which when several are in force. A human's decision is
# first because it is the one nothing in the system is entitled to reason past.
#
# INSTRUMENT_RESTRICTION is last because it is the narrowest stop here: every
# kind above it scopes to EVERYTHING, so when one of those stands it subsumes
# the restriction rather than being narrowed to a handful of banned symbols.
PRECEDENCE = (HUMAN_OVERRIDE, TRADING_HALT, POLICY_REFUSAL, SETTINGS_INVALID,
              INSTRUMENT_RESTRICTION)


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
    # Limits that zeroed only the symbols a halt was actually about. Counted
    # apart from the total, because "the whole book is stopped" and "two symbols
    # are stopped" are the two facts an operator most needs to tell apart.
    symbol_scoped_limits_issued: int = 0
    halts_released: int = 0
    limits_issued: int = 0
    zero_limits_issued: int = 0
    active: dict = field(default_factory=dict)


# What a halt's scope says when it is about the whole book rather than a list of
# symbols. The decider's own word, matched here rather than guessed at.
EVERYTHING = "everything"


def symbols_in_scope(scope: str) -> tuple[str, ...]:
    """The symbols a halt is about, or none at all when it is about everything.

    A scope this part cannot parse is treated as everything: an unreadable scope
    is not a reason to narrow a halt, and narrowing on a guess is how a halt stops
    protecting what it was raised over.
    """
    if not scope or scope == EVERYTHING:
        return EVERY_SYMBOL
    named = tuple(part.strip() for part in scope.split(",") if part.strip())
    return named or EVERY_SYMBOL


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
        """Zero while anything is halting, with the highest-precedence reason named.

        **The halt's scope travels with the limit.** A halt names what it is about
        -- "everything", or the symbols an anomaly was seen on -- and this part had
        nowhere to put that: it zeroed the segment's whole risk whatever the scope
        said. On 2026-08-25 two anomalous symbols out of a hundred stopped every
        trade the system could make, and the only trace was a counter of zero
        limits issued.
        """
        self.standing.limits_issued += 1
        for kind in PRECEDENCE:
            halt = self._halts.get(kind)
            if halt is not None:
                self.standing.zero_limits_issued += 1
                symbols = symbols_in_scope(halt.source)
                if symbols:
                    self.standing.symbol_scoped_limits_issued += 1
                return RiskLimit(
                    limiter=PART_ID,
                    fraction_of_allotment=NO_RISK_ALLOWED,
                    reason=f"{kind} from {halt.source}: {halt.reason}",
                    is_binding=True,
                    decided_at_ns=self._now_ns(),
                    symbols=symbols,
                )
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=self._allowed,
            reason="nothing is halting trading",
            is_binding=False,
            decided_at_ns=self._now_ns(),
        )

    def observe_restrictions(self, restrictions) -> None:
        """Halt the restricted symbols; lift the halt when none are reported.

        The only halt kind here released by absence rather than by an explicit
        release, and deliberately so: every other kind is a decision someone
        made and must un-make, while a ban is a list the exchange republishes
        daily, and a lifted ban is a name that has stopped appearing on it.

        The symbols travel in `source` because that is where this part already
        carries a halt's scope -- `symbols_in_scope` parses exactly this shape.
        Sorted, because the reason and the scope ride on every published
        risk-limit and an order that wandered would read as a changing halt.
        """
        restricted = sorted(
            restriction.symbol for restriction in restrictions
            if not restriction.may_open_new_position
        )
        if not restricted:
            self.release_halt(INSTRUMENT_RESTRICTION)
            return
        sources = sorted({
            source
            for restriction in restrictions
            if not restriction.may_open_new_position
            for source in restriction.sources
        })
        self.raise_halt(
            INSTRUMENT_RESTRICTION,
            ",".join(restricted),
            f"restricted by {', '.join(sources)}",
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
        "symbol_scoped_limits_issued": enforcer.standing.symbol_scoped_limits_issued,
    }


def run_halt_enforcer(
    enforcer: HaltEnforcer, control_socket, read_halt_events, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_halts(enforcer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Four sources can halt. A trading halt is raised while is_halted and
    released when a decision says it is not; a policy decision refusing the
    subject "trading" halts until one allows it; an active human override
    whose instruction says halt holds until it is inactive or expired; and a
    capital-settings verdict other than valid halts the segment until the
    settings read cleanly again. Each is its own kind, so the limit names
    every reason standing rather than the last one.
    """
    from runtime.input_assembly import Batch

    from runtime.input_assembly import LatestValue

    halts = Batch(read=context.bus.reader("trading-halt"))
    # A level, not an event: the whole restricted list is republished, and the
    # last one is true until it changes. Deliberately unbounded on this side --
    # instrument-restriction-state already expires each source's claim, and
    # ageing it twice would expire it here while it still stands there.
    restrictions = LatestValue(read=context.bus.reader("instrument-restriction"))
    decisions = Batch(read=context.bus.reader("policy-decision"))
    overrides = Batch(read=context.bus.reader("human-override"))
    verdicts = Batch(read=context.bus.reader("capital-settings-verdict"))
    publish_limits = context.bus.publisher_for("risk-limit")
    enforcer = HaltEnforcer(allowed_fraction_when_clear=context.number("risk_allowed_fraction_when_clear"))
    segment = str(context.setting("segment_id").value)

    def read_halt_events(_enforcer) -> None:
        standing = restrictions.value()
        if standing is not None:
            enforcer.observe_restrictions(standing)
        for halt in halts.payloads():
            if halt.is_halted:
                enforcer.raise_halt(TRADING_HALT, halt.scope, halt.reason)
            else:
                enforcer.release_halt(TRADING_HALT)
        for decision in decisions.payloads():
            if decision.subject != "trading":
                continue
            if decision.is_allowed:
                enforcer.release_halt(POLICY_REFUSAL)
            else:
                enforcer.raise_halt(POLICY_REFUSAL, decision.envelope_level, decision.reason)
        for override in overrides.payloads():
            should_halt = wants_halt(override.instruction)
            if override.is_active and should_halt:
                enforcer.raise_halt(HUMAN_OVERRIDE, override.source_reference, override.instruction)
            elif should_halt:
                enforcer.release_halt(HUMAN_OVERRIDE)
        for verdict in verdicts.payloads():
            if verdict.segment != segment:
                continue
            if verdict.permits_trading:
                enforcer.release_halt(SETTINGS_INVALID)
            else:
                enforcer.raise_halt(SETTINGS_INVALID, verdict.segment, verdict.reason)

    return run_halt_enforcer(
        enforcer=enforcer,
        control_socket=context.control_socket,
        read_halt_events=read_halt_events,
        publish_limit=lambda limit: publish_limits((limit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
