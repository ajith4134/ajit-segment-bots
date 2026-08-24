"""bull-setup-filter: which candidates are a long setup at all, and how much they count.

The scanner produces candidates for everything it notices; most of them are not
this bot's business. This is the gate, and it is deliberately the cheapest part
in the chain -- everything downstream costs feature building and a model call, so
a candidate rejected here is the difference between a bot that keeps up with the
tape and one that falls behind it.

Three things happen here and nothing else:

1. **Side.** A long bot takes long candidates. A short candidate is not a weak
   long, it is somebody else's work.
2. **The learned weight of the setup.** `bull-setup-weight` is what the weight
   learner has concluded about this detector's long calls specifically. A
   detector that is right about shorts and wrong about longs must be discounted
   here, and one number over both sides would hide that.
3. **A floor on the weighted strength**, so a detector that has been consistently
   wrong stops occupying the bot even when it keeps firing.

**A rejection is counted by reason.** A bot that quietly accepts nothing looks
exactly like a bot with nothing to accept, and the difference is the whole
question of whether it is working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, SideCandidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-setup-filter"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-setup-filter",
    consumes=("entry-candidate", "bull-setup-weight"),
    produces=("bull-side-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ACCEPTED = "accepted"
WRONG_SIDE = "not-a-long-setup"
SETUP_DISCOUNTED = "setup-weight-below-floor"
WEIGHTED_STRENGTH_TOO_LOW = "weighted-strength-below-floor"


@dataclass
class FilterStanding:
    candidates_seen: int = 0
    accepted: int = 0
    by_rejection: dict = field(default_factory=dict)
    by_detector: dict = field(default_factory=dict)
    weights_applied: int = 0


class BullSetupFilter:
    """Keeps the long candidates worth the rest of the bot's time."""

    def __init__(
        self,
        default_setup_weight: float,
        minimum_setup_weight: float,
        minimum_weighted_strength: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= minimum_setup_weight <= default_setup_weight:
            raise ValueError(
                "the floor must sit at or below the weight an unproven detector starts with, "
                "or no detector could ever be tried"
            )
        self._default_weight = default_setup_weight
        self._minimum_weight = minimum_setup_weight
        self._minimum_weighted_strength = minimum_weighted_strength
        self._now_ns = now_ns
        self._weights: dict[str, float] = {}
        self.standing = FilterStanding()

    def observe_setup_weight(self, detector: str, weight: float) -> None:
        """What the weight learner has concluded about this detector's long calls."""
        if weight < 0.0:
            raise ValueError("a negative weight would invert the detector rather than mute it")
        self._weights[detector] = weight
        self.standing.weights_applied += 1

    def weight_for(self, detector: str) -> float:
        """The learned weight, or the starting weight for a detector never judged."""
        return self._weights.get(detector, self._default_weight)

    def filter_candidate(self, candidate) -> tuple[SideCandidate | None, str]:
        self.standing.candidates_seen += 1

        if candidate.direction != LONG:
            return None, self._reject(WRONG_SIDE)

        weight = self.weight_for(candidate.detector)
        if weight < self._minimum_weight:
            return None, self._reject(SETUP_DISCOUNTED)

        weighted_strength = candidate.signal_strength * weight
        if weighted_strength < self._minimum_weighted_strength:
            return None, self._reject(WEIGHTED_STRENGTH_TOO_LOW)

        self.standing.accepted += 1
        self.standing.by_detector[candidate.detector] = (
            self.standing.by_detector.get(candidate.detector, 0) + 1
        )
        return (
            SideCandidate(
                bot=BOT,
                side=LONG,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                detector=candidate.detector,
                expectation=candidate.expectation,
                signal_strength=candidate.signal_strength,
                detector_confidence=candidate.confidence,
                setup_weight=weight,
                horizon_seconds=candidate.horizon_seconds,
                evidence=dict(candidate.evidence),
                reason=(
                    f"{candidate.detector} calls a long on {candidate.symbol} at strength "
                    f"{candidate.signal_strength:.4g}; this bot weights that detector "
                    f"{weight:.2f} "
                    f"({'learned' if candidate.detector in self._weights else 'unproven, so the starting weight'}), "
                    f"giving {weighted_strength:.4g} against a floor of "
                    f"{self._minimum_weighted_strength:.4g}"
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


def describe_filtering(filter_: BullSetupFilter) -> dict:
    return {
        "part_id": PART_ID,
        "candidates_seen": filter_.standing.candidates_seen,
        "accepted": filter_.standing.accepted,
        "rejected_by_reason": dict(filter_.standing.by_rejection),
        "accepted_by_detector": dict(filter_.standing.by_detector),
        "setup_weights_learned": len(filter_._weights),
    }


def run_bull_setup_filter(
    setup_filter: BullSetupFilter, control_socket, read_candidates_and_weights,
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
        read_standing=lambda: describe_filtering(setup_filter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A learned setup weight is a level -- what this detector is currently worth --
    and it is applied to every candidate until the learner says otherwise. Until
    any weight has been learned the filter uses the default, which is what makes an
    unproven detector neither favoured nor silenced.
    """
    from runtime.input_assembly import Batch

    candidates = Batch(read=context.bus.reader("entry-candidate"))
    weights = Batch(read=context.bus.reader("bull-setup-weight"))
    publish_side_candidates = context.bus.publisher_for("bull-side-candidate")

    def read_candidates_and_weights(setup_filter):
        for weight in weights.payloads():
            setup_filter.observe_setup_weight(weight)
        return candidates.payloads()

    return run_bull_setup_filter(
        setup_filter=BullSetupFilter(
            default_setup_weight=context.number("bull_default_setup_weight"),
            minimum_setup_weight=context.number("bull_minimum_setup_weight"),
            minimum_weighted_strength=context.number("bull_minimum_weighted_strength"),
        ),
        control_socket=context.control_socket,
        read_candidates_and_weights=read_candidates_and_weights,
        publish_side_candidates=publish_side_candidates,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
