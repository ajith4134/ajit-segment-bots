"""cross-segment-lesson-bridge: a lesson learned once, applied where it also holds.

When the futures segment loses money because a venue's funding settled during a
position it had sized without carry, that is not a futures lesson. It is a lesson
about carry, and the options segment will make the same mistake unless something
carries it across.

But most lessons do not travel, and a bridge that forwarded everything would
teach every segment the other segments' particulars. So the discipline is in
**what does not cross**:

- **A lesson about a mechanism travels. A lesson about an instrument does not.**
  "Funding settles against the short in the regimes where shorts look best" is
  about perpetuals; "carry accrues against a position whose horizon exceeds the
  settlement interval" is about carry, and only the second applies to a segment
  with different instruments.
- **A lesson travels only to segments that can act on it.** A lesson about
  option expiry means nothing to a spot segment, and forwarding it anyway trains
  every receiver to ignore the bridge.
- **A lesson carries the evidence that produced it**, so the receiving segment
  can judge whether its own conditions match rather than adopting a conclusion
  reached under conditions it does not share.

**A lesson is not an instruction.** It changes what a segment weighs; whether it
changes what a segment does is that segment's decision, which is what keeps every
trade decision inside the segment that owns it (RL-048).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "cross-segment-lesson-bridge"

PART_DECLARATION = PartDeclaration(
    part_id="cross-segment-lesson-bridge",
    consumes=("decoded-trade-instruction", "loss-cause"),
    produces=("cross-segment-lesson", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FUTURES = "futures"
SPOT = "spot"
OPTIONS = "options"

# What a lesson can be about. A lesson about a mechanism travels; one about an
# instrument does not, and the difference is the whole design.
CARRY = "carry"
LIQUIDITY = "liquidity"
EXECUTION = "execution"
SIZING = "sizing"
REGIME = "regime"
VENUE_BEHAVIOUR = "venue-behaviour"
INSTRUMENT_SPECIFIC = "instrument-specific"

# Which segments each mechanism can be acted on by.
APPLIES_TO = {
    CARRY: (FUTURES, OPTIONS),
    LIQUIDITY: (FUTURES, SPOT, OPTIONS),
    EXECUTION: (FUTURES, SPOT, OPTIONS),
    SIZING: (FUTURES, SPOT, OPTIONS),
    REGIME: (FUTURES, SPOT, OPTIONS),
    VENUE_BEHAVIOUR: (FUTURES, SPOT, OPTIONS),
    INSTRUMENT_SPECIFIC: (),
}

CROSSED = "crossed"
STAYS_HOME = "instrument-specific-so-it-does-not-travel"
NO_RECEIVER = "no-other-segment-can-act-on-it"
NOT_YET_PROVEN = "this-lesson-has-not-held-often-enough-to-cross"


@dataclass(frozen=True)
class CrossSegmentLesson:
    """One lesson, where it was learned, and where else it holds."""

    mechanism: str
    lesson: str
    learned_in: str
    applies_to: tuple
    times_it_has_held: Estimate
    evidence: dict
    state: str
    reason: str
    bridged_at_ns: int

    @property
    def travels(self) -> bool:
        return self.state == CROSSED

    @property
    def is_an_instruction(self) -> bool:
        """Never. A lesson changes what a segment weighs, not what it does."""
        return False


@dataclass
class BridgeStanding:
    lessons_seen: int = 0
    crossed: int = 0
    stayed_home: int = 0
    no_receiver: int = 0
    not_yet_proven: int = 0
    by_mechanism: dict = field(default_factory=dict)
    by_receiving_segment: dict = field(default_factory=dict)


class CrossSegmentLessonBridge:
    """Carries mechanism lessons between segments, and keeps particulars at home."""

    def __init__(
        self,
        minimum_occurrences: int,
        minimum_hold_rate: float,
        prior_hold_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_hold_rate <= 1.0:
            raise ValueError(
                "a lesson must have held more often than not before it is worth teaching "
                "another segment"
            )
        self._minimum = minimum_occurrences
        self._minimum_hold_rate = minimum_hold_rate
        self._prior_hold_rate = prior_hold_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._held: dict[tuple[str, str], RateEstimator] = {}
        self.standing = BridgeStanding()

    def observe_occurrence(self, mechanism: str, lesson: str, it_held: bool) -> None:
        """One more time this lesson did or did not hold, wherever it was observed."""
        self._estimator_for(mechanism, lesson).observe(it_held)

    def hold_rate(self, mechanism: str, lesson: str) -> Estimate:
        return self._estimator_for(mechanism, lesson).estimate(self._minimum)

    def bridge(self, mechanism: str, lesson: str, learned_in: str, evidence: dict) -> CrossSegmentLesson:
        self.standing.lessons_seen += 1
        self.standing.by_mechanism[mechanism] = self.standing.by_mechanism.get(mechanism, 0) + 1
        held = self.hold_rate(mechanism, lesson)

        if mechanism == INSTRUMENT_SPECIFIC or mechanism not in APPLIES_TO:
            self.standing.stayed_home += 1
            return self._lesson(
                mechanism, lesson, learned_in, (), held, evidence, STAYS_HOME,
                f"this is about the instrument rather than a mechanism, so it stays in "
                f"{learned_in}. A bridge that forwarded everything would teach every segment "
                f"the other segments' particulars",
            )

        receivers = tuple(
            segment for segment in APPLIES_TO[mechanism] if segment != learned_in
        )
        if not receivers:
            self.standing.no_receiver += 1
            return self._lesson(
                mechanism, lesson, learned_in, (), held, evidence, NO_RECEIVER,
                f"no other segment can act on a {mechanism} lesson; forwarding it anyway "
                f"trains every receiver to ignore this bridge",
            )

        if not held.is_fitted or held.value < self._minimum_hold_rate:
            self.standing.not_yet_proven += 1
            return self._lesson(
                mechanism, lesson, learned_in, receivers, held, evidence, NOT_YET_PROVEN,
                f"this lesson has held {held.value:.0%} of the time over {held.observations} "
                f"occurrence(s), below the {self._minimum_hold_rate:.0%} over "
                f"{self._minimum} needed before another segment should change what it weighs",
            )

        self.standing.crossed += 1
        for segment in receivers:
            self.standing.by_receiving_segment[segment] = (
                self.standing.by_receiving_segment.get(segment, 0) + 1
            )

        return self._lesson(
            mechanism, lesson, learned_in, receivers, held, evidence, CROSSED,
            f"a {mechanism} lesson learned in {learned_in} that has held {held.value:.0%} of "
            f"the time over {held.observations} occurrence(s), so it crosses to "
            f"{', '.join(receivers)}. It carries its evidence so each can judge whether its "
            f"own conditions match, rather than adopting a conclusion reached under conditions "
            f"it does not share. It changes what they weigh, not what they do",
        )

    def _estimator_for(self, mechanism: str, lesson: str) -> RateEstimator:
        key = (mechanism, lesson)
        estimator = self._held.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hold_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._held[key] = estimator
        return estimator

    def _lesson(
        self, mechanism, lesson, learned_in, receivers, held, evidence, state, reason
    ) -> CrossSegmentLesson:
        return CrossSegmentLesson(
            mechanism=mechanism,
            lesson=lesson,
            learned_in=learned_in,
            applies_to=receivers,
            times_it_has_held=held,
            evidence=dict(evidence),
            state=state,
            reason=reason,
            bridged_at_ns=self._now_ns(),
        )


def describe_lesson_bridging(bridge: CrossSegmentLessonBridge) -> dict:
    return {
        "part_id": PART_ID,
        "lessons_seen": bridge.standing.lessons_seen,
        "crossed": bridge.standing.crossed,
        "kept_home_as_instrument_specific": bridge.standing.stayed_home,
        "no_segment_could_act_on_it": bridge.standing.no_receiver,
        "not_yet_proven_enough_to_cross": bridge.standing.not_yet_proven,
        "by_mechanism": dict(sorted(bridge.standing.by_mechanism.items())),
        "by_receiving_segment": dict(sorted(bridge.standing.by_receiving_segment.items())),
        "mechanisms": sorted(APPLIES_TO),
        "produces_instructions": False,
    }


def run_cross_segment_lesson_bridge(
    bridge: CrossSegmentLessonBridge, control_socket, read_lessons, publish_lessons,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_lessons(
            tuple(
                bridge.bridge(mechanism, lesson, learned_in, evidence)
                for mechanism, lesson, learned_in, evidence in read_lessons(bridge)
            )
        )

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

    A decoded instruction is a lesson about the mechanism its change names:
    a stop lesson is about sizing, a fee or slippage lesson about execution,
    a funding lesson about carry, a regime lesson about regime, and a
    detector lesson about this segment's own instrument, which stays home.
    A loss cause is one occurrence of the mechanism that produced it. Each
    lesson is offered to the bridge once per health interval; the bridge
    says whether it has held often enough to cross.
    """
    import time as _time

    from runtime.input_assembly import Batch
    from runtime.trade_decoding_types import (
        COSTS_ATE_IT, THE_ENTRY_WAS_EARLY, THE_ENTRY_WAS_LATE, THE_EXIT_WAS_LATE,
        THE_REGIME_TURNED, THE_STOP_WAS_INSIDE_THE_NOISE, THE_STOP_WAS_TOO_WIDE,
    )

    instructions = Batch(read=context.bus.reader("decoded-trade-instruction"))
    causes = Batch(read=context.bus.reader("loss-cause"))
    publish_lessons = context.bus.publisher_for("cross-segment-lesson")
    segment = str(context.setting("segment_id").value)
    bridge = CrossSegmentLessonBridge(
        minimum_occurrences=int(context.number("lesson_minimum_occurrences")),
        minimum_hold_rate=context.number("lesson_minimum_hold_rate"),
        prior_hold_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    mechanism_of_change_prefix = {
        "stop": SIZING, "pnl-from": EXECUTION, "sequence": REGIME, "detector": INSTRUMENT_SPECIFIC,
    }
    mechanism_of_cause = {
        COSTS_ATE_IT: EXECUTION, THE_EXIT_WAS_LATE: EXECUTION,
        THE_ENTRY_WAS_EARLY: EXECUTION, THE_ENTRY_WAS_LATE: EXECUTION,
        THE_STOP_WAS_INSIDE_THE_NOISE: SIZING, THE_STOP_WAS_TOO_WIDE: SIZING,
        THE_REGIME_TURNED: REGIME,
    }
    lessons: dict[tuple[str, str], dict] = {}
    offered_at: dict[tuple[str, str], float] = {}

    def mechanism_of(instruction) -> str:
        change = str(instruction.change)
        prefix, _, rest = change.partition(":")
        if prefix == "pnl-from" and "funding" in rest:
            return CARRY
        return mechanism_of_change_prefix.get(prefix, INSTRUMENT_SPECIFIC)

    def read_lessons(_bridge):
        for instruction in instructions.payloads():
            key = (mechanism_of(instruction), str(instruction.change))
            lessons[key] = {
                "instruction_id": instruction.instruction_id, "applies_when": dict(instruction.applies_when),
                "trades_supporting": instruction.trades_supporting, "expected_effect": instruction.expected_effect,
            }
            bridge.observe_occurrence(key[0], key[1], instruction.can_be_acted_on)
        for loss in causes.payloads():
            mechanism = mechanism_of_cause.get(loss.cause)
            if mechanism is None:
                continue
            key = (mechanism, str(loss.cause))
            lessons.setdefault(key, {"from_losses": True})
            bridge.observe_occurrence(mechanism, str(loss.cause), bool(loss.is_fitted))
        now = _time.monotonic()
        due = []
        for key, evidence in lessons.items():
            if now - offered_at.get(key, float("-inf")) < context.health_interval_seconds:
                continue
            offered_at[key] = now
            due.append((key[0], key[1], segment, dict(evidence)))
        return tuple(due)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_lessons(kept)

    return run_cross_segment_lesson_bridge(
        bridge=bridge,
        control_socket=context.control_socket,
        read_lessons=read_lessons,
        publish_lessons=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
