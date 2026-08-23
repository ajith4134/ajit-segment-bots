"""lesson-extractor: one thing to do differently, stated so it can be tested.

The output of decoding a trade must be an instruction, not a lesson. "Be more
patient" cannot be tested, cannot be retired, and cannot be wrong -- which is exactly
why it feels satisfying to write and changes nothing. An instruction names the
condition it applies under, the change it asks for, and the effect it expects, so it
can be checked against later trades and dropped when it stops holding.

Four constraints, each closing a way that decoding turns into folklore:

- **An instruction needs more than one trade behind it.** A single trade produces a
  narrative, not a rule, and a system that changes something after every trade is
  being tuned by noise.
- **It must not come from an insignificant outcome.** If the result was
  indistinguishable from the symbol moving, there is nothing to learn from it,
  however clear the story reads.
- **It must be conditional.** An unconditional instruction applies to every future
  trade, including all the ones it was never derived from, which is how one bad
  quarter rewrites a working strategy.
- **Contradictory instructions block each other.** "Widen the stop" and "tighten the
  stop" from the same conditions are not two lessons -- they are evidence that the
  stop is not what mattered, and following either makes things worse.

The extractor writes instructions. It does not apply them: an instruction goes to the
hypothesis system to be tested, because a change adopted because it was derived is a
change adopted without evidence.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import DecodedTradeInstruction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "lesson-extractor"

PART_DECLARATION = PartDeclaration(
    part_id="lesson-extractor",
    consumes=(
        "trade-episode", "trade-narrative", "pnl-attribution", "stop-audit",
        "sequence-pattern",
    ),
    produces=("decoded-trade-instruction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

EXTRACTED = "extracted"
TOO_FEW_TRADES = "one-trade-produces-a-narrative-not-a-rule"
NOT_SIGNIFICANT = "the-outcomes-it-rests-on-were-indistinguishable-from-noise"
UNCONDITIONAL = "it-names-no-condition-so-it-would-apply-to-everything"
CONTRADICTED = "an-opposite-instruction-exists-for-the-same-conditions"
NOT_TESTABLE = "it-cannot-be-checked-against-a-later-trade"

# Changes this part can express. Each is a knob something downstream actually has,
# because an instruction naming a knob that does not exist can never be applied.
WIDEN_THE_STOP = "widen-the-stop"
TIGHTEN_THE_STOP = "tighten-the-stop"
ENTER_LATER = "enter-later"
ENTER_EARLIER = "enter-earlier"
HOLD_LONGER = "hold-longer"
EXIT_SOONER = "exit-sooner"
TRADE_SMALLER = "trade-smaller"
AVOID_THE_SETUP = "avoid-the-setup"
PREFER_A_CHEAPER_INSTRUMENT = "prefer-a-cheaper-instrument"

EXPRESSIBLE_CHANGES = (
    WIDEN_THE_STOP, TIGHTEN_THE_STOP, ENTER_LATER, ENTER_EARLIER, HOLD_LONGER,
    EXIT_SOONER, TRADE_SMALLER, AVOID_THE_SETUP, PREFER_A_CHEAPER_INSTRUMENT,
)

OPPOSITES = {
    WIDEN_THE_STOP: TIGHTEN_THE_STOP,
    TIGHTEN_THE_STOP: WIDEN_THE_STOP,
    ENTER_LATER: ENTER_EARLIER,
    ENTER_EARLIER: ENTER_LATER,
    HOLD_LONGER: EXIT_SOONER,
    EXIT_SOONER: HOLD_LONGER,
}


@dataclass(frozen=True)
class ExtractionOutcome:
    change: str
    state: str
    instruction: DecodedTradeInstruction | None
    supporting_trades: tuple
    reason: str
    extracted_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == EXTRACTED and self.instruction is not None


@dataclass
class ExtractorStanding:
    extractions_attempted: int = 0
    instructions_written: int = 0
    refused_thin: int = 0
    refused_insignificant: int = 0
    refused_unconditional: int = 0
    refused_contradicted: int = 0
    contradictions_blocked: tuple = ()
    instructions_applied: int = 0


class LessonExtractor:
    """Turns decomposed trades into conditional, testable instructions."""

    def __init__(
        self,
        minimum_trades: int,
        minimum_significant_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_trades < 2:
            raise ValueError(
                "a single trade produces a narrative, not a rule, and a system that "
                "changes something after every trade is being tuned by noise"
            )
        if not 0.0 < minimum_significant_fraction <= 1.0:
            raise ValueError(
                "an instruction resting on outcomes indistinguishable from the symbol "
                "moving has nothing behind it"
            )
        self._minimum_trades = minimum_trades
        self._minimum_significant_fraction = minimum_significant_fraction
        self._now_ns = now_ns
        self._evidence: dict[tuple, list] = {}
        self._written: dict[tuple, DecodedTradeInstruction] = {}
        self._sequence = 0
        self.standing = ExtractorStanding()

    @staticmethod
    def _key(change: str, conditions: dict) -> tuple:
        return (change, tuple(sorted(conditions.items())))

    def observe_evidence(
        self, change: str, conditions: dict, trade_id: str, effect: float,
        was_significant: bool,
    ) -> None:
        """One trade supporting one change under one set of conditions."""
        if change not in EXPRESSIBLE_CHANGES:
            raise ValueError(
                f"{change!r} is not a change anything downstream can apply. An "
                f"instruction naming a knob that does not exist can never be acted on"
            )
        self._evidence.setdefault(self._key(change, conditions), []).append(
            {"trade_id": trade_id, "effect": effect, "significant": was_significant}
        )

    def extract(self, change: str, conditions: dict) -> ExtractionOutcome:
        self.standing.extractions_attempted += 1
        key = self._key(change, conditions)
        evidence = self._evidence.get(key, [])
        trade_ids = tuple(item["trade_id"] for item in evidence)

        if not conditions:
            self.standing.refused_unconditional += 1
            return self._outcome(
                change, UNCONDITIONAL, None, trade_ids,
                "it names no condition, so it would apply to every future trade "
                "including all the ones it was never derived from. That is how one bad "
                "quarter rewrites a working strategy",
            )

        if len(evidence) < self._minimum_trades:
            self.standing.refused_thin += 1
            return self._outcome(
                change, TOO_FEW_TRADES, None, trade_ids,
                f"{len(evidence)} trade(s) behind it, below the {self._minimum_trades} "
                f"needed. One trade produces a narrative, not a rule",
            )

        significant = sum(1 for item in evidence if item["significant"])
        fraction = significant / len(evidence)
        if fraction < self._minimum_significant_fraction:
            self.standing.refused_insignificant += 1
            return self._outcome(
                change, NOT_SIGNIFICANT, None, trade_ids,
                f"only {fraction:.0%} of the supporting outcomes were distinguishable "
                f"from the symbol moving, below the "
                f"{self._minimum_significant_fraction:.0%} bar. However clear the story "
                f"reads, there is nothing there to learn from",
            )

        opposite = OPPOSITES.get(change)
        if opposite is not None:
            opposite_key = self._key(opposite, conditions)
            if opposite_key in self._written:
                self.standing.refused_contradicted += 1
                self.standing.contradictions_blocked = tuple(
                    sorted(set(self.standing.contradictions_blocked) | {change})
                )
                return self._outcome(
                    change, CONTRADICTED, None, trade_ids,
                    f"an instruction to {opposite} already exists for the same "
                    f"conditions. These are not two lessons -- they are evidence that "
                    f"this is not what mattered, and following either makes things worse",
                )

        expected = statistics.mean(item["effect"] for item in evidence)
        self._sequence += 1
        instruction = DecodedTradeInstruction(
            instruction_id=f"instruction-{self._sequence}",
            applies_when=dict(conditions),
            change=change,
            derived_from=trade_ids,
            expected_effect=expected,
            trades_supporting=len(evidence),
            is_testable=True,
            reason=(
                f"{change} when "
                + ", ".join(f"{name} = {value}" for name, value in sorted(conditions.items()))
                + f", from {len(evidence)} trade(s) with a mean effect of {expected:+.4f} "
                  f"and {fraction:.0%} of them significant. It is testable and retirable, "
                  f"which 'be more patient' is not. It is not applied here: a change "
                  f"adopted because it was derived is a change adopted without evidence"
            ),
            written_at_ns=self._now_ns(),
        )
        self._written[key] = instruction
        self.standing.instructions_written += 1

        return self._outcome(
            change, EXTRACTED, instruction, trade_ids, instruction.reason,
        )

    def instructions(self) -> tuple:
        return tuple(self._written.values())

    def _outcome(self, change, state, instruction, trade_ids, reason) -> ExtractionOutcome:
        return ExtractionOutcome(
            change=change, state=state, instruction=instruction,
            supporting_trades=trade_ids, reason=reason, extracted_at_ns=self._now_ns(),
        )


def describe_lesson_extraction(extractor: LessonExtractor) -> dict:
    return {
        "part_id": PART_ID,
        "extractions_attempted": extractor.standing.extractions_attempted,
        "instructions_written": extractor.standing.instructions_written,
        "refused_too_few_trades": extractor.standing.refused_thin,
        "refused_insignificant_outcomes": extractor.standing.refused_insignificant,
        "refused_unconditional": extractor.standing.refused_unconditional,
        "refused_contradicted": extractor.standing.refused_contradicted,
        "contradictions_blocked": list(extractor.standing.contradictions_blocked),
        "expressible_changes": list(EXPRESSIBLE_CHANGES),
        "applies_an_instruction": False,
        "instructions_applied": extractor.standing.instructions_applied,
    }


def run_lesson_extractor(
    extractor: LessonExtractor, control_socket, read_evidence, publish_instructions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_evidence():
            extractor.observe_evidence(**job)
        for change, conditions in list(extractor._evidence):
            outcome = extractor.extract(change, dict(conditions))
            if outcome.is_usable:
                publish_instructions(outcome.instruction)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
