"""instruction-promotion-gate: what has to be true before an instruction trades.

This is the last gate between a number that looks good and money at risk, and it is
built to refuse. Every condition below corresponds to a way a strategy passes a
backtest and loses live, and each has to hold on its own -- a strong result on one
axis cannot buy a failure on another, because these are not different amounts of the
same evidence.

- **The run must be believable.** A defective backtest's number is not a small
  overstatement, it is unrelated to anything achievable.
- **It must clear every fold, not most of them.** A rule that works in three folds
  out of five is a rule that works in some market conditions and has not identified
  which -- and the ones it failed are as informative as the ones it passed.
- **The sample must support the claim.** The scorer computes how many effective bets
  the edge needs; fewer means the result is indistinguishable from luck.
- **It must have survived an attempt to refute it**, not merely an attempt to
  confirm it. Confirmation is available for anything.
- **Multiple testing must be accounted for.** The hundredth rule tried has roughly a
  one-in-two chance of clearing a 99% bar by luck alone, so the bar rises with the
  number of trials that came before it.
- **It must state what would falsify it.** An instruction with no falsification
  criterion can never be retired, and a system that cannot retire rules accumulates
  them until they contradict each other.

Promotion is not a judgement that the instruction works. It is a statement that it
survived, and the difference matters when it stops working.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.backtest_types import ProvenInstruction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-promotion-gate"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-promotion-gate",
    consumes=(
        "backtest-result", "backtest-verdict", "refutation-verdict", "trial-ledger",
        "required-sample-size", "falsification-criterion",
    ),
    produces=("proven-instruction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROMOTED = "promoted"
NOT_BELIEVABLE = "at-least-one-run-has-a-fatal-defect"
FAILED_A_FOLD = "it-does-not-hold-in-every-fold"
SAMPLE_TOO_SMALL = "the-edge-cannot-be-told-apart-from-luck-at-this-sample-size"
NOT_REFUTED = "nothing-tried-to-refute-it"
WAS_REFUTED = "an-attempt-to-refute-it-succeeded"
LOST_TO_MULTIPLE_TESTING = "it-clears-the-plain-bar-but-not-the-one-adjusted-for-trials"
NO_FALSIFICATION_CRITERION = "it-does-not-state-what-would-prove-it-wrong"
NO_RUNS = "there-is-nothing-to-judge"


@dataclass(frozen=True)
class GateOutcome:
    instruction_id: str
    state: str
    proven: ProvenInstruction | None
    failures: tuple
    adjusted_bar: float | None
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == PROMOTED and self.proven is not None


@dataclass
class GateStanding:
    decisions: int = 0
    promoted: int = 0
    refused_not_believable: int = 0
    refused_failed_a_fold: int = 0
    refused_sample_too_small: int = 0
    refused_not_refuted: int = 0
    refused_was_refuted: int = 0
    refused_multiple_testing: int = 0
    refused_no_falsification: int = 0
    highest_adjusted_bar: float = 0.0


class InstructionPromotionGate:
    """Refuses on any of six independent grounds, and states which."""

    def __init__(
        self,
        base_expectancy_bar: float,
        multiple_testing_exponent: float,
        now_ns=time.time_ns,
    ) -> None:
        if base_expectancy_bar <= 0:
            raise ValueError(
                "an expectancy bar at or below zero promotes anything that did not lose"
            )
        if multiple_testing_exponent <= 0:
            raise ValueError(
                "the hundredth rule tried has roughly a one-in-two chance of clearing a "
                "99% bar by luck, so the bar must rise with the number of trials"
            )
        self._base_bar = base_expectancy_bar
        self._exponent = multiple_testing_exponent
        self._now_ns = now_ns
        self._results: dict[str, list] = {}
        self._verdicts: dict[str, object] = {}
        self._refutations: dict[str, bool] = {}
        self._trials: dict[str, int] = {}
        self._criteria: dict[str, str] = {}
        self.standing = GateStanding()

    def observe_result(self, result) -> None:
        self._results.setdefault(result.instruction_id, []).append(result)

    def observe_verdict(self, verdict) -> None:
        self._verdicts[verdict.run_id] = verdict

    def observe_refutation(self, instruction_id: str, survived: bool) -> None:
        """Whether an attempt to refute it failed. Confirmation is not evidence."""
        self._refutations[instruction_id] = survived

    def observe_trials(self, family: str, trials: int) -> None:
        """How many rules were tried before this one, in the same family."""
        self._trials[family] = trials

    def observe_falsification_criterion(self, instruction_id: str, criterion: str) -> None:
        self._criteria[instruction_id] = criterion

    def adjusted_bar(self, family: str) -> float:
        """The bar rises with the number of trials that came before."""
        trials = max(self._trials.get(family, 1), 1)
        bar = self._base_bar * (math.log(trials + 1) ** self._exponent)
        self.standing.highest_adjusted_bar = max(self.standing.highest_adjusted_bar, bar)
        return bar

    def decide(self, instruction_id: str, family: str, folds_expected: int) -> GateOutcome:
        self.standing.decisions += 1
        results = self._results.get(instruction_id, [])
        if not results:
            return self._outcome(
                instruction_id, NO_RUNS, None, (NO_RUNS,), None,
                "no backtest result exists for this instruction",
            )

        failures: list = []

        unbelievable = [
            result.run_id
            for result in results
            if instruction_id and not getattr(
                self._verdicts.get(result.run_id), "is_believable", False
            )
        ]
        if unbelievable:
            self.standing.refused_not_believable += 1
            failures.append(NOT_BELIEVABLE)
            return self._outcome(
                instruction_id, NOT_BELIEVABLE, None, tuple(failures), None,
                f"{len(unbelievable)} run(s) have a fatal defect. A defective backtest's "
                f"number is not a small overstatement, it is unrelated to anything "
                f"achievable",
            )

        bar = self.adjusted_bar(family)
        passed = [result for result in results if result.expectancy > 0]
        if len(results) < folds_expected or len(passed) < folds_expected:
            self.standing.refused_failed_a_fold += 1
            return self._outcome(
                instruction_id, FAILED_A_FOLD, None, (FAILED_A_FOLD,), bar,
                f"{len(passed)} of {folds_expected} fold(s) held. A rule that works in "
                f"some folds works in some market conditions and has not identified "
                f"which -- and the folds it failed are as informative as the ones it "
                f"passed",
            )

        insignificant = [result for result in results if not result.is_significant]
        if insignificant:
            self.standing.refused_sample_too_small += 1
            return self._outcome(
                instruction_id, SAMPLE_TOO_SMALL, None, (SAMPLE_TOO_SMALL,), bar,
                f"{len(insignificant)} fold(s) had fewer effective bets than the edge "
                f"needs; the largest requirement was "
                f"{max(result.required_trades for result in insignificant)}",
            )

        if instruction_id not in self._refutations:
            self.standing.refused_not_refuted += 1
            return self._outcome(
                instruction_id, NOT_REFUTED, None, (NOT_REFUTED,), bar,
                "nothing has tried to refute it. Confirmation is available for anything, "
                "so surviving an attempt at refutation is the evidence that counts",
            )
        if not self._refutations[instruction_id]:
            self.standing.refused_was_refuted += 1
            return self._outcome(
                instruction_id, WAS_REFUTED, None, (WAS_REFUTED,), bar,
                "an attempt to refute it succeeded",
            )

        if instruction_id not in self._criteria:
            self.standing.refused_no_falsification += 1
            return self._outcome(
                instruction_id, NO_FALSIFICATION_CRITERION, None,
                (NO_FALSIFICATION_CRITERION,), bar,
                "it does not state what would prove it wrong, so it could never be "
                "retired -- and a system that cannot retire rules accumulates them until "
                "they contradict each other",
            )

        mean_expectancy = sum(result.expectancy for result in results) / len(results)
        if mean_expectancy < bar:
            self.standing.refused_multiple_testing += 1
            return self._outcome(
                instruction_id, LOST_TO_MULTIPLE_TESTING, None,
                (LOST_TO_MULTIPLE_TESTING,), bar,
                f"expectancy {mean_expectancy:+.6f} against a bar of {bar:.6f}, raised "
                f"from {self._base_bar:.6f} because {self._trials.get(family, 1)} rule(s) "
                f"were tried in this family",
            )

        proven = ProvenInstruction(
            instruction_id=instruction_id,
            runs=tuple(result.run_id for result in results),
            folds_passed=len(passed),
            folds_total=folds_expected,
            net_return=sum(result.net_return for result in results),
            trials_before_it=self._trials.get(family, 1),
            survived_refutation=True,
            reason=(
                f"held in all {folds_expected} fold(s) with expectancy "
                f"{mean_expectancy:+.6f} against a trials-adjusted bar of {bar:.6f}, "
                f"every run believable, every sample sufficient, and an attempt to refute "
                f"it failed. This says it survived, not that it works -- and the "
                f"difference matters when it stops"
            ),
            proven_at_ns=self._now_ns(),
        )
        self.standing.promoted += 1
        return self._outcome(instruction_id, PROMOTED, proven, (), bar, proven.reason)

    def _outcome(
        self, instruction_id, state, proven, failures, bar, reason,
    ) -> GateOutcome:
        return GateOutcome(
            instruction_id=instruction_id, state=state, proven=proven,
            failures=failures, adjusted_bar=bar, reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_promotion(gate: InstructionPromotionGate) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": gate.standing.decisions,
        "promoted": gate.standing.promoted,
        "refused_not_believable": gate.standing.refused_not_believable,
        "refused_failed_a_fold": gate.standing.refused_failed_a_fold,
        "refused_sample_too_small": gate.standing.refused_sample_too_small,
        "refused_nothing_tried_to_refute_it": gate.standing.refused_not_refuted,
        "refused_it_was_refuted": gate.standing.refused_was_refuted,
        "refused_multiple_testing": gate.standing.refused_multiple_testing,
        "refused_no_falsification_criterion": gate.standing.refused_no_falsification,
        "highest_adjusted_bar": gate.standing.highest_adjusted_bar,
        "trades_off_one_condition_against_another": False,
        "claims_a_promoted_instruction_works": False,
    }


def run_instruction_promotion_gate(
    gate: InstructionPromotionGate, control_socket, read_candidates, publish_proven,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for instruction_id, family, folds_expected in read_candidates(gate):
            outcome = gate.decide(instruction_id, family, folds_expected)
            if outcome.is_usable:
                publish_proven(outcome.proven)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
