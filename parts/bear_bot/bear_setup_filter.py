"""bear-setup-filter: which candidates are a short setup, and what a short costs to hold.

The mirror of the bull filter in position and nothing else. Being short is not
being long with the sign flipped, and the filter is where the asymmetries that
matter get their first say:

- **Funding.** A perpetual short is paid when funding is positive and pays when
  it is negative. A short into deeply negative funding bleeds every settlement
  whatever the price does, and a filter blind to that would keep handing the bot
  setups whose carry eats the edge before the thesis resolves.
- **The squeeze.** A long's worst case is the position going to zero. A short's
  worst case has no ceiling, and it arrives fastest in exactly the thin symbols
  where a short setup looks best. That is not this part's job to size, but it is
  this part's job not to pass on setups in symbols where it is unmanageable.

**The weight is learned from short results only.** A detector that is right about
longs and wrong about shorts must be weighted low here while the bull bot weights
it high, and this bot's scorecard is the only thing that can tell them apart --
which is why `bear-setup-weight` is a different data type from `bull-setup-weight`
rather than one weight both bots read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SHORT, SideCandidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-setup-filter"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-setup-filter",
    consumes=("entry-candidate", "bear-setup-weight"),
    produces=("bear-side-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ACCEPTED = "accepted"
WRONG_SIDE = "not-a-short-setup"
SETUP_DISCOUNTED = "setup-weight-below-floor"
WEIGHTED_STRENGTH_TOO_LOW = "weighted-strength-below-floor"
CARRY_EATS_THE_EDGE = "funding-carry-costs-more-than-the-setup-is-worth"


@dataclass
class FilterStanding:
    candidates_seen: int = 0
    accepted: int = 0
    by_rejection: dict = field(default_factory=dict)
    by_detector: dict = field(default_factory=dict)
    worst_carry_refused: float = 0.0


class BearSetupFilter:
    """Keeps the short candidates worth the rest of the bot's time and its carry."""

    def __init__(
        self,
        default_setup_weight: float,
        minimum_setup_weight: float,
        minimum_weighted_strength: float,
        settlements_per_day: float,
        maximum_carry_fraction_of_horizon: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= minimum_setup_weight <= default_setup_weight:
            raise ValueError(
                "the floor must sit at or below the weight an unproven detector starts with, "
                "or no detector could ever be tried"
            )
        if settlements_per_day <= 0:
            raise ValueError(
                "funding settles on a schedule; without it a carry cost cannot be projected "
                "over a horizon"
            )
        self._default_weight = default_setup_weight
        self._minimum_weight = minimum_setup_weight
        self._minimum_weighted_strength = minimum_weighted_strength
        self._settlements_per_day = settlements_per_day
        self._maximum_carry = maximum_carry_fraction_of_horizon
        self._now_ns = now_ns
        self._weights: dict[str, float] = {}
        self._funding: dict[tuple[str, str], float] = {}
        self.standing = FilterStanding()

    def observe_setup_weight(self, detector: str, weight: float) -> None:
        if weight < 0.0:
            raise ValueError("a negative weight would invert the detector rather than mute it")
        self._weights[detector] = weight

    def observe_funding_rate(self, venue_id: str, symbol: str, rate: float) -> None:
        """The rate per settlement. Positive pays the short; negative charges it."""
        self._funding[(venue_id, symbol)] = rate

    def weight_for(self, detector: str) -> float:
        return self._weights.get(detector, self._default_weight)

    def projected_carry(self, venue_id: str, symbol: str, horizon_seconds: float) -> float | None:
        """What holding this short over its horizon costs, as a fraction of notional.

        Positive means the short is charged. A short into negative funding pays
        every settlement whatever the price does.
        """
        rate = self._funding.get((venue_id, symbol))
        if rate is None:
            return None
        settlements = horizon_seconds / 86400.0 * self._settlements_per_day
        return -rate * settlements

    def filter_candidate(self, candidate) -> tuple[SideCandidate | None, str]:
        self.standing.candidates_seen += 1

        if candidate.direction != SHORT:
            return None, self._reject(WRONG_SIDE)

        weight = self.weight_for(candidate.detector)
        if weight < self._minimum_weight:
            return None, self._reject(SETUP_DISCOUNTED)

        weighted_strength = candidate.signal_strength * weight
        if weighted_strength < self._minimum_weighted_strength:
            return None, self._reject(WEIGHTED_STRENGTH_TOO_LOW)

        carry = self.projected_carry(candidate.venue_id, candidate.symbol, candidate.horizon_seconds)
        if carry is not None and carry > self._maximum_carry:
            self.standing.worst_carry_refused = max(self.standing.worst_carry_refused, carry)
            return None, self._reject(CARRY_EATS_THE_EDGE)

        self.standing.accepted += 1
        self.standing.by_detector[candidate.detector] = (
            self.standing.by_detector.get(candidate.detector, 0) + 1
        )
        return (
            SideCandidate(
                bot=BOT,
                side=SHORT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                detector=candidate.detector,
                expectation=candidate.expectation,
                signal_strength=candidate.signal_strength,
                detector_confidence=candidate.confidence,
                setup_weight=weight,
                horizon_seconds=candidate.horizon_seconds,
                evidence={
                    **candidate.evidence,
                    "projected_carry_fraction": carry,
                },
                reason=(
                    f"{candidate.detector} calls a short on {candidate.symbol} at strength "
                    f"{candidate.signal_strength:.4g}; this bot weights that detector "
                    f"{weight:.2f} "
                    f"({'learned' if candidate.detector in self._weights else 'unproven, so the starting weight'}), "
                    f"giving {weighted_strength:.4g} against a floor of "
                    f"{self._minimum_weighted_strength:.4g}"
                    + (
                        f"; funding over the {candidate.horizon_seconds:.0f}s horizon "
                        + (
                            f"costs the short {carry:.3%}"
                            if carry > 0
                            else f"pays the short {-carry:.3%}"
                        )
                        if carry is not None
                        else "; no funding rate has arrived, so the carry is unknown"
                    )
                ),
                accepted_at_ns=self._now_ns(),
            ),
            ACCEPTED,
        )

    def filter_all(self, candidates) -> tuple[SideCandidate, ...]:
        accepted = []
        for candidate in candidates:
            side_candidate, _ = self.filter_candidate(candidate)
            if side_candidate is not None:
                accepted.append(side_candidate)
        return tuple(accepted)

    def _reject(self, reason: str) -> str:
        self.standing.by_rejection[reason] = self.standing.by_rejection.get(reason, 0) + 1
        return reason


def describe_filtering(filter_: BearSetupFilter) -> dict:
    return {
        "part_id": PART_ID,
        "candidates_seen": filter_.standing.candidates_seen,
        "accepted": filter_.standing.accepted,
        "rejected_by_reason": dict(filter_.standing.by_rejection),
        "accepted_by_detector": dict(filter_.standing.by_detector),
        "worst_carry_refused": filter_.standing.worst_carry_refused,
        "setup_weights_learned": len(filter_._weights),
        "symbols_with_a_funding_rate": len(filter_._funding),
    }


def run_bear_setup_filter(
    setup_filter: BearSetupFilter, control_socket, read_candidates_and_weights,
    publish_side_candidates, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = read_candidates_and_weights(setup_filter)
        publish_side_candidates(setup_filter.filter_all(candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
