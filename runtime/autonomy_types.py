"""The vocabulary for a system that keeps itself running and changes itself.

This is the block where a mistake is not a bad trade but a system that cannot be
stopped, or one that quietly rewrote the part that was protecting it. Every shape
here is built around a single asymmetry: **expanding what the system may do requires
evidence, and contracting it requires none.**

Four ideas run through all of them:

- **Autonomy is an envelope, not a switch.** What the system may do alone is a set of
  bounded permissions that widen with demonstrated competence and snap shut on
  trouble. A binary "autonomous: yes" cannot express the state that is actually
  wanted almost all of the time.
- **A human override outranks everything and is never inferred.** It arrives from
  outside, it is not produced by any reasoning in here, and no part may clear it.
- **Every self-modification is journalled before it takes effect.** A change that
  cannot be found afterwards cannot be undone, and the failure this prevents is a
  system whose current behaviour nobody can explain.
- **Stopping is always available and always cheap.** Every gate here fails towards
  stopping, because a halt costs opportunity and a runaway costs capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How much of itself the system may change without being asked.
OBSERVE_ONLY = "observe-only"                 # it may look and report, nothing else
PROPOSE_ONLY = "propose-only"                 # it may write proposals for a human
ACT_WITHIN_LIMITS = "act-within-limits"       # it may trade inside bounded limits
MODIFY_ITSELF = "modify-itself"               # it may admit parts it wrote

AUTONOMY_LEVELS = (OBSERVE_ONLY, PROPOSE_ONLY, ACT_WITHIN_LIMITS, MODIFY_ITSELF)

# How much resource is left to run on. Not a health metric -- a budget one.
COMFORTABLE = "comfortable"
FRUGAL = "frugal"
CRITICAL = "critical"
SHUTDOWN = "shutdown"

SURVIVAL_TIERS = (COMFORTABLE, FRUGAL, CRITICAL, SHUTDOWN)


@dataclass(frozen=True)
class PartFault:
    """Something wrong with a part, named precisely enough to act on.

    The distinction that matters is between a part that is failing loudly and one
    that is failing silently: a crashed part is obvious and a part that has been
    returning the same answer for six hours is not, and the second is the one that
    does damage.
    """

    part_id: str
    kind: str
    detail: str
    first_seen_at_ns: int
    observations: int
    is_silent: bool
    severity: str
    reason: str
    detected_at_ns: int

    @property
    def needs_restarting(self) -> bool:
        return self.severity in ("fatal", "stalled")


@dataclass(frozen=True)
class RestartRequest:
    """A request to restart a part, with what has already been tried.

    Restart counts travel with the request because the second restart is a fix and
    the fifth is a loop, and a warden that cannot tell them apart will restart a
    broken part forever.
    """

    part_id: str
    reason: str
    restarts_already: int
    within_seconds: float
    is_backing_off: bool
    backoff_seconds: float
    requested_at_ns: int

    @property
    def is_a_loop(self) -> bool:
        return self.restarts_already >= 3


@dataclass(frozen=True)
class OutageState:
    """Whether a venue is reachable, and what may still be believed about it.

    An outage is not just an absence of data. Positions held at an unreachable venue
    are still open, still moving, and still able to be liquidated, so the state
    carries whether exposure exists there.
    """

    venue_id: str
    is_reachable: bool
    last_message_at_ns: int
    silent_seconds: float
    has_open_exposure: bool
    consecutive_failures: int
    state: str
    reason: str
    measured_at_ns: int

    @property
    def is_dangerous(self) -> bool:
        """Unreachable with exposure is the case that has to reach a human."""
        return not self.is_reachable and self.has_open_exposure


@dataclass(frozen=True)
class SurvivalTier:
    """How much runway is left, in the resource that runs out first."""

    tier: str
    binding_resource: str
    fraction_remaining: float
    seconds_to_exhaustion: float | None
    reason: str
    measured_at_ns: int

    @property
    def permits_discretionary_work(self) -> bool:
        return self.tier in (COMFORTABLE, FRUGAL)


@dataclass(frozen=True)
class ConservationPlan:
    """What to stop doing, in order, when the runway shortens.

    Ordered so the things that produce the least value per unit of resource go
    first, and so nothing that protects capital is ever in the list.
    """

    tier: str
    parts_to_stop: tuple
    parts_that_never_stop: tuple
    expected_saving_fraction: float
    reason: str
    planned_at_ns: int


@dataclass(frozen=True)
class CapabilityGap:
    """Something the system cannot do that it can tell it cannot do.

    A gap has to be reachable and namable to be worth anything: "we would do better
    with a better model" is neither, and a backlog full of those is indistinguishable
    from no backlog.
    """

    gap_id: str
    description: str
    evidence: tuple
    blocks_what: str
    would_be_a_new_part: bool
    is_reachable: bool
    blocked_by: str | None
    found_at_ns: int

    @property
    def is_actionable(self) -> bool:
        return self.is_reachable and bool(self.evidence)


@dataclass(frozen=True)
class ProposedPart:
    """A part the system wrote for itself, before anything has checked it.

    Deliberately a different type from an admitted one: proposed code must not be
    passable where running code is expected, and the type system is a cheaper guard
    than a review.
    """

    proposal_id: str
    part_id: str
    consumes: tuple
    produces: tuple
    resource_class: str
    rate_risk: str
    skipped_tick_effect: str
    source: str
    tests: str
    fills_gap: str
    written_by: str
    proposed_at_ns: int

    @property
    def declares_itself_completely(self) -> bool:
        return bool(self.produces) and bool(self.resource_class)


@dataclass(frozen=True)
class AdmittedPart:
    """A proposed part that passed every gate, and what it passed."""

    proposal_id: str
    part_id: str
    checks_passed: tuple
    contract_holds: bool
    tests_passed: int
    reviewed_by: str
    admitted_at_ns: int


@dataclass(frozen=True)
class AutonomyEnvelope:
    """What the system may do on its own right now, and why that much.

    Widening requires demonstrated competence; narrowing requires nothing. That
    asymmetry is the whole design, and it is enforced by the part that produces
    these rather than by whoever reads them.
    """

    level: str
    may_trade: bool
    maximum_notional: float
    may_admit_parts: bool
    may_change_settings: bool
    reason: str
    narrowed_by: str | None
    issued_at_ns: int

    @property
    def is_open(self) -> bool:
        return self.level == MODIFY_ITSELF


@dataclass(frozen=True)
class PolicyDecision:
    """Whether one specific action is inside the envelope.

    Separate from the envelope because an envelope is a standing state and a policy
    decision is about a particular thing at a particular moment, and conflating them
    means every action re-derives the policy.
    """

    subject: str
    action: str
    is_allowed: bool
    envelope_level: str
    reason: str
    decided_at_ns: int


@dataclass(frozen=True)
class ModificationRecord:
    """One change the system made to itself, written before it took effect."""

    record_id: str
    part_id: str
    change: str
    before: str | None
    after: str
    authorised_by: str
    envelope_level: str
    took_effect_at_ns: int | None
    recorded_at_ns: int

    @property
    def is_reversible(self) -> bool:
        return self.before is not None


@dataclass(frozen=True)
class UpstreamChange:
    """Something outside changed that this system depends on."""

    subject: str
    kind: str
    detail: str
    affects_parts: tuple
    is_breaking: bool
    source_reference: str
    noticed_at_ns: int


@dataclass(frozen=True)
class TradingHalt:
    """A decision to stop trading, with what caused it and what clears it.

    A halt that nothing can clear is a shutdown, and a halt that clears itself on a
    timer is a pause. Both are legitimate and they are not the same thing, so the
    clearing condition is part of the halt.
    """

    is_halted: bool
    causes: tuple
    scope: str
    cleared_by: str
    may_close_positions: bool
    reason: str
    decided_at_ns: int

    @property
    def is_total(self) -> bool:
        return self.is_halted and self.scope == "everything"


@dataclass(frozen=True)
class HumanOverride:
    """An instruction from outside the system. It outranks everything in here."""

    override_id: str
    instruction: str
    scope: str
    is_active: bool
    issued_at_ns: int
    expires_at_ns: int | None
    source_reference: str

    @property
    def has_expired(self) -> bool:
        return self.expires_at_ns is not None and self.expires_at_ns < self.issued_at_ns


@dataclass(frozen=True)
class ReplacementPlan:
    """How to swap a faulty part for a replacement without a gap in between."""

    part_id: str
    faulty_reason: str
    replacement_source: str
    steps: tuple
    is_reversible: bool
    requires_a_pause: bool
    reason: str
    planned_at_ns: int


@dataclass(frozen=True)
class FoldedCircuitMap:
    """The whole system's shape at a size a decision can actually use.

    321 parts is not a picture anybody reasons over. This folds them into their
    blocks and reports where the health actually is, so a capability gap can be
    located without reading the full graph.
    """

    blocks: dict
    parts_total: int
    parts_running: int
    parts_faulted: int
    parts_never_started: int
    blocks_entirely_dark: tuple
    measured_at_ns: int

    @property
    def running_fraction(self) -> float:
        return self.parts_running / self.parts_total if self.parts_total else 0.0


def closed_envelope(reason: str, narrowed_by: str, at_ns: int) -> AutonomyEnvelope:
    """The envelope everything falls back to. Narrowing needs no evidence."""
    return AutonomyEnvelope(
        level=OBSERVE_ONLY, may_trade=False, maximum_notional=0.0,
        may_admit_parts=False, may_change_settings=False, reason=reason,
        narrowed_by=narrowed_by, issued_at_ns=at_ns,
    )
