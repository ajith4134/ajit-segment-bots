"""instruction-writer: the one part that turns everything learned into something that acts.

Every other part in this block and the two beside it produces evidence. This is
where evidence becomes an instruction the scanner will watch for on every symbol,
which makes it the narrowest and most consequential gate in the system: whatever
gets through here is what the system actually does.

It consumes twenty data types, and the point of that is that **an instruction is
written only when every one of them agrees**. The conditions are not weighed
against each other -- each covers a distinct way a wrong instruction gets written:

- **A falsification criterion** exists, so it can be retired.
- **A required sample size** exists and is reachable, so it can be answered.
- **It clears the bar its own search implies**, so the best of two hundred
  variations is not mistaken for a finding.
- **It is novel**, so the system is not testing the same idea in three wordings
  and reading three confirmations.
- **A refutation battery did not break it.**
- **It has a regime tag**, so it fires where its evidence came from and nowhere
  else.
- **Its edge outlives its own sample size**, because confirming an edge that has
  already decayed is spending the trades to learn nothing.

**The instruction carries its retirement condition with it.** An instruction
without one outlives the market it was learned in, and nothing else is positioned
to notice.

**A refusal names every failing condition, not the first.** "It failed" sends the
next attempt to fix the wrong thing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import OpportunityInstruction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-writer"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-writer",
    consumes=(
        "decoded-trade-instruction", "expectancy-breakdown", "recalled-episode",
        "semantic-fact", "loaded-skill-section", "strategy-gap", "feature-reliability",
        "regime-break-alert", "novel-idea", "cross-segment-lesson", "inverted-hypothesis",
        "hypothesis-priority", "winner-pattern", "reflection-note", "candidate-formula",
        "mutated-hypothesis", "falsification-criterion", "knowledge-link", "regime-memory",
        "horizon-profile",
    ),
    produces=("opportunity-instruction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WRITTEN = "written"

NO_FALSIFICATION_CRITERION = "nothing-could-retire-it"
NO_SAMPLE_SIZE = "nobody-has-said-how-many-trades-would-answer-it"
SAMPLE_UNREACHABLE = "more-trades-than-this-system-can-produce"
DOES_NOT_CLEAR_ITS_TRIALS = "the-best-of-many-variations-is-not-a-finding"
NOT_NOVEL = "the-same-idea-in-different-words-is-not-three-confirmations"
WAS_REFUTED = "the-refutation-battery-broke-it"
NOT_REFUTATION_TESTED = "nothing-tried-to-break-it"
NO_REGIME_TAG = "nothing-says-which-market-this-is-a-claim-about"
EDGE_DIES_BEFORE_IT_IS_CONFIRMED = "its-edge-decays-before-its-own-sample-size-is-reached"
NO_MEASUREMENT = "the-scanner-cannot-watch-for-this"


@dataclass
class WriterStanding:
    requests: int = 0
    written: int = 0
    refused: int = 0
    by_failing_condition: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    instructions_live: int = 0


class InstructionWriter:
    """Writes an instruction only when every condition agrees, and says which failed."""

    def __init__(
        self,
        minimum_novelty: float,
        maximum_reachable_trades: int,
        known_measurements: tuple,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_novelty <= 1.0:
            raise ValueError("novelty is a fraction and its bar must be inside (0, 1]")
        if not known_measurements:
            raise ValueError(
                "an instruction over something nothing measures can never fire, so the set of "
                "measurements the scanner watches has to be stated"
            )
        self._minimum_novelty = minimum_novelty
        self._maximum_trades = maximum_reachable_trades
        self._known_measurements = tuple(known_measurements)
        self._now_ns = now_ns
        self._criteria: dict[str, object] = {}
        self._samples: dict[str, int] = {}
        self._trial_clears: dict[str, bool] = {}
        self._novelty: dict[str, float] = {}
        self._verdicts: dict[str, str] = {}
        self._tags: dict[str, str | None] = {}
        self._half_lives: dict[str, float] = {}
        self._live: dict[str, OpportunityInstruction] = {}
        self.standing = WriterStanding()

    def observe_falsification_criterion(self, hypothesis_id: str, criterion) -> None:
        self._criteria[hypothesis_id] = criterion

    def observe_required_sample(self, hypothesis_id: str, trades: int) -> None:
        self._samples[hypothesis_id] = trades

    def observe_trial_verdict(self, hypothesis_id: str, clears_its_bar: bool) -> None:
        self._trial_clears[hypothesis_id] = clears_its_bar

    def observe_novelty(self, hypothesis_id: str, novelty: float) -> None:
        self._novelty[hypothesis_id] = novelty

    def observe_refutation_verdict(self, hypothesis_id: str, verdict: str) -> None:
        self._verdicts[hypothesis_id] = verdict

    def observe_regime_tag(self, hypothesis_id: str, regime: str | None) -> None:
        self._tags[hypothesis_id] = regime

    def observe_edge_half_life(self, hypothesis_id: str, half_life_trades: float) -> None:
        self._half_lives[hypothesis_id] = half_life_trades

    def failing_conditions(self, hypothesis) -> tuple:
        """Every condition that fails, not the first -- so the next attempt fixes the right one."""
        hypothesis_id = hypothesis.hypothesis_id
        failing = []

        if hypothesis.context.get("measurement") not in self._known_measurements:
            failing.append(NO_MEASUREMENT)

        if hypothesis_id not in self._criteria:
            failing.append(NO_FALSIFICATION_CRITERION)

        trades = self._samples.get(hypothesis_id)
        if trades is None:
            failing.append(NO_SAMPLE_SIZE)
        elif trades > self._maximum_trades:
            failing.append(SAMPLE_UNREACHABLE)

        if not self._trial_clears.get(hypothesis_id, False):
            failing.append(DOES_NOT_CLEAR_ITS_TRIALS)

        if self._novelty.get(hypothesis_id, 0.0) < self._minimum_novelty:
            failing.append(NOT_NOVEL)

        verdict = self._verdicts.get(hypothesis_id)
        if verdict is None:
            failing.append(NOT_REFUTATION_TESTED)
        elif verdict == "refuted":
            failing.append(WAS_REFUTED)

        if self._tags.get(hypothesis_id) is None:
            failing.append(NO_REGIME_TAG)

        half_life = self._half_lives.get(hypothesis_id)
        if half_life is not None and trades is not None and half_life < trades:
            # Confirming an edge that decays before its own sample size is
            # reached spends the trades to learn nothing.
            failing.append(EDGE_DIES_BEFORE_IT_IS_CONFIRMED)

        return tuple(failing)

    def write(self, hypothesis) -> tuple[OpportunityInstruction | None, tuple]:
        """One hypothesis, written only when every condition agrees."""
        self.standing.requests += 1
        self.standing.by_source[hypothesis.source] = (
            self.standing.by_source.get(hypothesis.source, 0) + 1
        )

        failing = self.failing_conditions(hypothesis)
        if failing:
            self.standing.refused += 1
            for condition in failing:
                self.standing.by_failing_condition[condition] = (
                    self.standing.by_failing_condition.get(condition, 0) + 1
                )
            return None, failing

        hypothesis_id = hypothesis.hypothesis_id
        criterion = self._criteria[hypothesis_id]
        trades = self._samples[hypothesis_id]
        regime = self._tags[hypothesis_id]
        context = hypothesis.context

        instruction = OpportunityInstruction(
            instruction_id=f"instruction:{hypothesis_id}",
            hypothesis_id=hypothesis_id,
            measurement=context["measurement"],
            comparison=context.get("comparison", "above"),
            threshold=context.get("threshold", 0.0),
            direction=context.get("direction", "long"),
            expectation=context.get("expectation", "continuation"),
            horizon_seconds=context.get("horizon_seconds", 0.0),
            regime_tag=regime,
            # Carried with it: an instruction without a retirement condition
            # outlives the market it was learned in, and nothing else is
            # positioned to notice.
            retire_when=(
                f"{criterion.measure} {criterion.comparison} {criterion.threshold:.4g} over "
                f"{criterion.required_trades:,} trade(s), or the {regime} regime breaks, or "
                f"its edge half-life runs out"
            ),
            required_sample_size=trades,
            trials_in_family=hypothesis.trials_in_family,
            evidence={
                "novelty": self._novelty.get(hypothesis_id),
                "refutation_verdict": self._verdicts.get(hypothesis_id),
                "edge_half_life_trades": self._half_lives.get(hypothesis_id),
                "source": hypothesis.source,
                **hypothesis.evidence,
            },
            reason=(
                f"{hypothesis.statement}. Every condition agreed: it can be refuted "
                f"({criterion.measure} {criterion.comparison} {criterion.threshold:.4g}), "
                f"{trades:,} trade(s) would answer it, it clears the bar its "
                f"{hypothesis.trials_in_family}-trial search implies, it is novel at "
                f"{self._novelty.get(hypothesis_id, 0.0):.2f}, refutation did not break it, and "
                f"it is tagged for {regime} so it fires where its evidence came from and "
                f"nowhere else"
            ),
            written_at_ns=self._now_ns(),
        )

        self._live[instruction.instruction_id] = instruction
        self.standing.written += 1
        self.standing.instructions_live = len(self._live)
        return instruction, ()

    def retire(self, instruction_id: str) -> None:
        self._live.pop(instruction_id, None)
        self.standing.instructions_live = len(self._live)

    @property
    def live_instructions(self) -> tuple:
        return tuple(self._live.values())


def describe_instruction_writing(writer: InstructionWriter) -> dict:
    return {
        "part_id": PART_ID,
        "requests": writer.standing.requests,
        "written": writer.standing.written,
        "refused": writer.standing.refused,
        "by_failing_condition": dict(sorted(writer.standing.by_failing_condition.items())),
        "by_source": dict(sorted(writer.standing.by_source.items())),
        "instructions_live": writer.standing.instructions_live,
        "known_measurements": list(writer._known_measurements),
    }


def run_instruction_writer(
    writer: InstructionWriter, control_socket, read_hypotheses, publish_instructions,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        instructions = []
        for hypothesis in read_hypotheses(writer):
            instruction, _ = writer.write(hypothesis)
            if instruction is not None:
                instructions.append(instruction)
        publish_instructions(tuple(instructions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
