"""autonomy-boundary: what this system may do on its own, right now.

One asymmetry decides everything here: **widening the envelope requires demonstrated
competence; narrowing it requires nothing at all.** Any signal that something is
wrong closes it immediately, and reopening takes sustained evidence over time. That
is not caution for its own sake -- it is the only arrangement where a mistake in the
evidence costs opportunity rather than capital.

The envelope is four levels rather than a switch, because the state that is wanted
almost always is somewhere in the middle: allowed to trade within limits, not allowed
to rewrite itself. A binary "autonomous: yes/no" cannot express that, so systems built
on one end up either supervised constantly or unsupervised entirely.

What widening requires, in order:

- **Demonstrated competence at the level below.** Measured from what the system
  actually did there, not from how long it has been running.
- **A clean modification history.** A system that recently changed itself and broke
  something does not get to change itself more.
- **A survival tier with headroom.** Expanding what a system may do while it is
  running out of resources is how a shutdown becomes a crash.

What narrows it: any one of those failing, a human override, an unresolved fault, or
a venue unreachable with exposure. **Narrowing needs one reason. Widening needs all of
them.** And the reason for the current level is always nameable -- an envelope nobody
can explain is one nobody can trust.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import (
    ACT_WITHIN_LIMITS, AUTONOMY_LEVELS, AutonomyEnvelope, COMFORTABLE, CRITICAL,
    FRUGAL, MODIFY_ITSELF, OBSERVE_ONLY, PROPOSE_ONLY, SHUTDOWN, closed_envelope,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "autonomy-boundary"

PART_DECLARATION = PartDeclaration(
    part_id="autonomy-boundary",
    consumes=("bot-maturity", "modification-record", "survival-tier"),
    produces=("autonomy-envelope", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ISSUED = "issued"
NARROWED = "narrowed"
HELD = "held-at-the-current-level"
WIDENED = "widened"

# Why the envelope narrowed. One of these is enough.
A_HUMAN_SAID_SO = "a-human-override-is-active"
COMPETENCE_FELL = "measured-competence-fell-below-the-level"
A_MODIFICATION_BROKE_SOMETHING = "a-recent-self-modification-broke-something"
RUNWAY_IS_SHORT = "the-survival-tier-has-no-headroom"
A_FAULT_IS_UNRESOLVED = "a-part-fault-is-unresolved"
EXPOSURE_IS_UNREACHABLE = "a-venue-holding-positions-cannot-be-reached"

NARROWING_REASONS = (
    A_HUMAN_SAID_SO, COMPETENCE_FELL, A_MODIFICATION_BROKE_SOMETHING, RUNWAY_IS_SHORT,
    A_FAULT_IS_UNRESOLVED, EXPOSURE_IS_UNREACHABLE,
)

# The tier a level needs to have headroom at.
TIER_FOR_LEVEL = {
    OBSERVE_ONLY: SHUTDOWN,
    PROPOSE_ONLY: CRITICAL,
    ACT_WITHIN_LIMITS: FRUGAL,
    MODIFY_ITSELF: COMFORTABLE,
}


@dataclass(frozen=True)
class EnvelopeOutcome:
    state: str
    envelope: AutonomyEnvelope
    previous_level: str
    narrowing_reasons: tuple
    widening_blocked_by: tuple
    reason: str
    issued_at_ns: int

    @property
    def narrowed(self) -> bool:
        return AUTONOMY_LEVELS.index(self.envelope.level) < AUTONOMY_LEVELS.index(
            self.previous_level
        )


@dataclass
class BoundaryStanding:
    issues: int = 0
    narrowings: int = 0
    widenings: int = 0
    holds: int = 0
    by_narrowing_reason: dict = field(default_factory=dict)
    widening_attempts_blocked: int = 0
    times_widened_without_evidence: int = 0


class AutonomyBoundary:
    """Closes on one reason, opens on all of them, and always names the current level."""

    def __init__(
        self,
        competence_for_level: dict,
        clean_modifications_required: int,
        readings_before_widening: int,
        maximum_notional_for_level: dict,
        now_ns=time.time_ns,
    ) -> None:
        missing = set(AUTONOMY_LEVELS) - set(competence_for_level)
        if missing:
            raise ValueError(
                f"every level needs a competence bar; missing {sorted(missing)}"
            )
        if set(AUTONOMY_LEVELS) - set(maximum_notional_for_level):
            raise ValueError("every level needs a notional ceiling, including zero")
        if clean_modifications_required < 1:
            raise ValueError(
                "a system that recently broke something while changing itself does not "
                "get to change itself more"
            )
        if readings_before_widening < 2:
            raise ValueError(
                "widening takes sustained evidence; one good reading is a moment"
            )
        self._competence_for_level = dict(competence_for_level)
        self._clean_required = clean_modifications_required
        self._readings_before_widening = readings_before_widening
        self._maximum_notional = dict(maximum_notional_for_level)
        self._now_ns = now_ns
        self._level = OBSERVE_ONLY
        self._competence: float | None = None
        self._tier: str | None = None
        self._clean_modifications = 0
        self._override_active = False
        self._unresolved_faults = 0
        self._unreachable_exposure = False
        self._good_readings = 0
        self.standing = BoundaryStanding()

    def observe_competence(self, competence: float) -> None:
        self._competence = competence

    def observe_survival_tier(self, tier: str) -> None:
        self._tier = tier

    def observe_modification(self, broke_something: bool) -> None:
        self._clean_modifications = 0 if broke_something else self._clean_modifications + 1

    def observe_override(self, is_active: bool) -> None:
        self._override_active = is_active

    def observe_unresolved_faults(self, count: int) -> None:
        self._unresolved_faults = count

    def observe_unreachable_exposure(self, is_unreachable: bool) -> None:
        self._unreachable_exposure = is_unreachable

    def narrowing_reasons_for(self, level: str) -> tuple:
        reasons = []
        if self._override_active:
            reasons.append(A_HUMAN_SAID_SO)
        if self._unreachable_exposure:
            reasons.append(EXPOSURE_IS_UNREACHABLE)
        if self._unresolved_faults > 0:
            reasons.append(A_FAULT_IS_UNRESOLVED)
        if self._competence is None or self._competence < self._competence_for_level[level]:
            reasons.append(COMPETENCE_FELL)
        if self._clean_modifications < self._clean_required:
            reasons.append(A_MODIFICATION_BROKE_SOMETHING)
        tier_needed = TIER_FOR_LEVEL[level]
        if self._tier is None or self._tier_index(self._tier) > self._tier_index(tier_needed):
            reasons.append(RUNWAY_IS_SHORT)
        return tuple(reasons)

    @staticmethod
    def _tier_index(tier: str) -> int:
        from runtime.autonomy_types import SURVIVAL_TIERS

        return SURVIVAL_TIERS.index(tier) if tier in SURVIVAL_TIERS else len(SURVIVAL_TIERS)

    def issue(self) -> EnvelopeOutcome:
        self.standing.issues += 1
        previous = self._level

        # Narrowing first, and it needs only one reason.
        current_reasons = self.narrowing_reasons_for(previous)
        if current_reasons:
            target = previous
            while target != OBSERVE_ONLY and self.narrowing_reasons_for(target):
                target = AUTONOMY_LEVELS[AUTONOMY_LEVELS.index(target) - 1]
            self._level = target
            self._good_readings = 0
            for reason in current_reasons:
                self.standing.by_narrowing_reason[reason] = (
                    self.standing.by_narrowing_reason.get(reason, 0) + 1
                )
            if target != previous:
                self.standing.narrowings += 1
            return self._outcome(
                NARROWED if target != previous else HELD, previous, current_reasons, (),
                f"narrowed to {target} because {', '.join(current_reasons)}. Narrowing "
                f"needs one reason; widening needs all of them",
            )

        # Widening: every condition for the level above, sustained.
        index = AUTONOMY_LEVELS.index(previous)
        if index + 1 >= len(AUTONOMY_LEVELS):
            self.standing.holds += 1
            return self._outcome(
                HELD, previous, (), (),
                f"already at {previous}, the widest level",
            )

        higher = AUTONOMY_LEVELS[index + 1]
        blocking = self.narrowing_reasons_for(higher)
        if blocking:
            self._good_readings = 0
            self.standing.widening_attempts_blocked += 1
            return self._outcome(
                HELD, previous, (), blocking,
                f"held at {previous}: {higher} would need {', '.join(blocking)} resolved "
                f"first",
            )

        self._good_readings += 1
        if self._good_readings < self._readings_before_widening:
            self.standing.holds += 1
            return self._outcome(
                HELD, previous, (), (),
                f"held at {previous} with {self._good_readings}/"
                f"{self._readings_before_widening} clean reading(s) towards {higher}. "
                f"One good reading is a moment, not competence",
            )

        self._level = higher
        self._good_readings = 0
        self.standing.widenings += 1
        return self._outcome(
            WIDENED, previous, (), (),
            f"widened to {higher} after {self._readings_before_widening} sustained clean "
            f"reading(s), with competence {self._competence:.2f} and tier {self._tier}",
        )

    def _outcome(self, state, previous, narrowing, blocking, reason) -> EnvelopeOutcome:
        level = self._level
        return EnvelopeOutcome(
            state=state,
            envelope=AutonomyEnvelope(
                level=level,
                may_trade=level in (ACT_WITHIN_LIMITS, MODIFY_ITSELF),
                maximum_notional=self._maximum_notional[level],
                may_admit_parts=level == MODIFY_ITSELF,
                may_change_settings=level in (ACT_WITHIN_LIMITS, MODIFY_ITSELF),
                reason=reason,
                narrowed_by=narrowing[0] if narrowing else None,
                issued_at_ns=self._now_ns(),
            ),
            previous_level=previous, narrowing_reasons=narrowing,
            widening_blocked_by=blocking, reason=reason, issued_at_ns=self._now_ns(),
        )


def describe_boundary(boundary: AutonomyBoundary) -> dict:
    return {
        "part_id": PART_ID,
        "issues": boundary.standing.issues,
        "narrowings": boundary.standing.narrowings,
        "widenings": boundary.standing.widenings,
        "holds": boundary.standing.holds,
        "by_narrowing_reason": dict(boundary.standing.by_narrowing_reason),
        "widening_attempts_blocked": boundary.standing.widening_attempts_blocked,
        "levels": list(AUTONOMY_LEVELS),
        "narrowing_reasons": list(NARROWING_REASONS),
        "widens_without_sustained_evidence": False,
        "times_widened_without_evidence": (
            boundary.standing.times_widened_without_evidence
        ),
        "is_a_binary_switch": False,
    }


def run_autonomy_boundary(
    boundary: AutonomyBoundary, control_socket, read_state, publish_envelopes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_state(boundary)
        publish_envelopes(boundary.issue().envelope)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_boundary(boundary),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Competence is measured as the share of judged (bot, regime) records that are
    currently mature, from the maturity levels this part consumes -- never from
    how long the system has been running. A modification record whose change
    names a rollback counts as one that broke something, because a rollback is
    the one observable admission that the change before it was wrong; every
    other applied record counts clean. The blueprint gives this part no fault,
    override or exposure input, so those three narrowing reasons keep their
    constructor defaults -- the halt decider, which does consume them, is where
    they bite.
    """
    from runtime.autonomy_types import AUTONOMY_LEVELS
    from runtime.input_assembly import Batch, LatestByKey, LatestValue

    maturities = LatestByKey(
        read=context.bus.reader("bot-maturity"),
        key_of=lambda maturity: (maturity.bot, maturity.regime),
    )
    modifications = Batch(read=context.bus.reader("modification-record"))
    tiers = LatestValue(read=context.bus.reader("survival-tier"))
    publish_envelopes = context.bus.publisher_for("autonomy-envelope")

    bars = [float(bar) for bar in context.setting("autonomy_competence_bars").value]
    ceilings = [float(c) for c in context.setting("autonomy_notional_ceilings").value]
    if len(bars) != len(AUTONOMY_LEVELS) or len(ceilings) != len(AUTONOMY_LEVELS):
        raise ValueError(
            "autonomy_competence_bars and autonomy_notional_ceilings each carry one "
            "entry per autonomy level, in the order of AUTONOMY_LEVELS"
        )
    boundary = AutonomyBoundary(
        competence_for_level=dict(zip(AUTONOMY_LEVELS, bars)),
        clean_modifications_required=int(
            context.number("autonomy_clean_modifications_required")
        ),
        readings_before_widening=int(context.number("autonomy_readings_before_widening")),
        maximum_notional_for_level=dict(zip(AUTONOMY_LEVELS, ceilings)),
    )

    def read_state(_boundary):
        judged = maturities.mapping()
        if judged:
            mature = sum(1 for maturity in judged.values() if maturity.is_mature)
            boundary.observe_competence(mature / len(judged))
        for record in modifications.payloads():
            boundary.observe_modification(broke_something="rollback" in record.change)
        tier = tiers.value()
        if tier is not None:
            boundary.observe_survival_tier(tier.tier)

    return run_autonomy_boundary(
        boundary=boundary,
        control_socket=context.control_socket,
        read_state=read_state,
        publish_envelopes=lambda envelope: publish_envelopes((envelope,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
