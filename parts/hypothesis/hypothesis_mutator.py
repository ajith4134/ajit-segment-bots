"""hypothesis-mutator: small variations on what nearly worked, counted as trials.

The cheapest source of new hypotheses and the most dangerous. An instruction that
almost worked, varied slightly, is far more likely to test well than a random
idea -- and far more likely to test well *by chance*, because the variations are
correlated and each one is another draw from the same distribution.

So mutation is allowed, and every mutation is counted:

- **Every variation is a trial in the parent's family**, not a new family. Twenty
  mutations of one instruction are twenty trials of one idea, and letting each
  start a fresh family is exactly how a search defeats its own correction.
- **A retired instruction is mutated once, not repeatedly.** Retirement means the
  edge stopped working; producing thirty variants of it is a system arguing with
  its own evidence.
- **Near misses are the richest source.** A trade the system almost took that
  would have worked says the threshold is wrong, which is a specific mutation
  rather than a random one.

The mutations themselves are small and bounded: a threshold moved, a horizon
changed, a regime condition added or removed. **A mutation that changes the
measurement is not a mutation** -- it is a new hypothesis, and it goes through the
deduplicator and the falsifier as one.

**Mutation stops when the family stops producing.** A family whose last several
mutations all failed is a family being mined for noise, and continuing is how a
search burns its whole trial budget on one dead idea.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import Hypothesis
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hypothesis-mutator"

PART_DECLARATION = PartDeclaration(
    part_id="hypothesis-mutator",
    consumes=(
        "instruction-scorecard", "retired-instruction", "instruction-history",
        "near-miss-episode",
    ),
    produces=("mutated-hypothesis", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MUTATED = "mutated"
FAMILY_IS_EXHAUSTED = "this-family's-last-mutations-all-failed"
ALREADY_MUTATED = "a-retired-instruction-is-mutated-once"
NOT_A_MUTATION = "changing-the-measurement-makes-it-a-new-hypothesis"
NOTHING_TO_MUTATE = "no-parent-to-vary"

MOVE_THE_THRESHOLD = "move-the-threshold"
CHANGE_THE_HORIZON = "change-the-horizon"
ADD_A_REGIME_CONDITION = "add-a-regime-condition"
REMOVE_A_REGIME_CONDITION = "remove-a-regime-condition"


@dataclass(frozen=True)
class ParentInstruction:
    """What is being varied, and how it did."""

    instruction_id: str
    family: str
    measurement: str
    comparison: str
    threshold: float
    horizon_seconds: float
    regime_tag: str | None
    trades: int
    hit_rate: float
    was_retired: bool


@dataclass
class MutatorStanding:
    mutations_requested: int = 0
    mutated: int = 0
    families_exhausted: int = 0
    retired_parents_refused: int = 0
    measurement_changes_refused: int = 0
    by_mutation: dict = field(default_factory=dict)
    largest_family: int = 0


class HypothesisMutator:
    """Varies what nearly worked, and counts every variation in the parent's family."""

    def __init__(
        self,
        threshold_step_fraction: float,
        horizon_step_fraction: float,
        consecutive_failures_before_stopping: int,
        now_ns=time.time_ns,
    ) -> None:
        if threshold_step_fraction <= 0 or horizon_step_fraction <= 0:
            raise ValueError("a mutation of zero produces the parent again")
        if consecutive_failures_before_stopping < 1:
            raise ValueError(
                "a family with no stopping rule burns the whole trial budget on one dead idea"
            )
        self._threshold_step = threshold_step_fraction
        self._horizon_step = horizon_step_fraction
        self._failures_before_stopping = consecutive_failures_before_stopping
        self._now_ns = now_ns
        self._trials: dict[str, int] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._mutated_retired: set[str] = set()
        self._near_misses: dict[str, list] = {}
        self.standing = MutatorStanding()

    def observe_outcome(self, family: str, the_mutation_worked: bool) -> None:
        """Whether a family's last mutation produced anything."""
        if the_mutation_worked:
            self._consecutive_failures[family] = 0
        else:
            self._consecutive_failures[family] = self._consecutive_failures.get(family, 0) + 1

    def observe_near_miss(self, instruction_id: str, measurement_value: float, would_have_worked: bool) -> None:
        """A trade almost taken. Says the threshold is wrong, which is a specific mutation."""
        if would_have_worked:
            self._near_misses.setdefault(instruction_id, []).append(measurement_value)

    def family_is_exhausted(self, family: str) -> bool:
        return (
            self._consecutive_failures.get(family, 0) >= self._failures_before_stopping
        )

    def trials_in(self, family: str) -> int:
        return self._trials.get(family, 0)

    def mutate(self, parent: ParentInstruction, mutation: str) -> tuple[Hypothesis | None, str]:
        """One variation of one parent, counted in the parent's own family."""
        self.standing.mutations_requested += 1

        if self.family_is_exhausted(parent.family):
            # A family whose last several mutations all failed is being mined
            # for noise.
            self.standing.families_exhausted += 1
            return None, FAMILY_IS_EXHAUSTED

        if parent.was_retired:
            if parent.instruction_id in self._mutated_retired:
                # Retirement means the edge stopped working; thirty variants of
                # it is a system arguing with its own evidence.
                self.standing.retired_parents_refused += 1
                return None, ALREADY_MUTATED
            self._mutated_retired.add(parent.instruction_id)

        if mutation not in (
            MOVE_THE_THRESHOLD, CHANGE_THE_HORIZON,
            ADD_A_REGIME_CONDITION, REMOVE_A_REGIME_CONDITION,
        ):
            self.standing.measurement_changes_refused += 1
            return None, NOT_A_MUTATION

        threshold, horizon, regime = self._apply(parent, mutation)

        # Counted in the parent's family, never a fresh one: letting each
        # mutation start a new family is how a search defeats its own correction.
        self._trials[parent.family] = self._trials.get(parent.family, 0) + 1
        trials = self._trials[parent.family]
        self.standing.largest_family = max(self.standing.largest_family, trials)
        self.standing.mutated += 1
        self.standing.by_mutation[mutation] = self.standing.by_mutation.get(mutation, 0) + 1

        return (
            Hypothesis(
                hypothesis_id=f"{parent.family}:mutation-{trials}",
                statement=(
                    f"{parent.measurement} {parent.comparison} {threshold:.6g} over "
                    f"{horizon:.0f}s"
                    + (f" in the {regime} regime" if regime else "")
                    + f" resolves more often than this system's base rate"
                ),
                what_would_refute_it=(
                    f"the mutated form resolving at or below the base rate over its own "
                    f"required sample, which would mean the parent's near-miss was noise"
                ),
                family=parent.family,
                trials_in_family=trials,
                source=f"{PART_ID}:{mutation}",
                context={
                    "parent": parent.instruction_id,
                    "measurement": parent.measurement,
                    "comparison": parent.comparison,
                    "threshold": threshold,
                    "horizon_seconds": horizon,
                    "regime": regime,
                },
                required_sample_size=None,
                regime_tag=regime,
                novelty=None,
                evidence={
                    "parent_threshold": parent.threshold,
                    "parent_horizon_seconds": parent.horizon_seconds,
                    "parent_hit_rate": parent.hit_rate,
                    "parent_trades": parent.trades,
                    "mutation": mutation,
                    "near_misses_used": len(self._near_misses.get(parent.instruction_id, [])),
                },
                proposed_at_ns=self._now_ns(),
            ),
            MUTATED,
        )

    def _apply(self, parent: ParentInstruction, mutation: str) -> tuple:
        """The mutation itself: small, bounded, and never a change of measurement."""
        threshold = parent.threshold
        horizon = parent.horizon_seconds
        regime = parent.regime_tag

        if mutation == MOVE_THE_THRESHOLD:
            near_misses = self._near_misses.get(parent.instruction_id)
            if near_misses:
                # The near misses say where the threshold should have been,
                # which is a specific mutation rather than a random step.
                threshold = sum(near_misses) / len(near_misses)
            else:
                threshold = parent.threshold * (1.0 - self._threshold_step)
        elif mutation == CHANGE_THE_HORIZON:
            horizon = parent.horizon_seconds * (1.0 + self._horizon_step)
        elif mutation == ADD_A_REGIME_CONDITION:
            regime = regime or "trending"
        elif mutation == REMOVE_A_REGIME_CONDITION:
            regime = None

        return threshold, horizon, regime


def describe_mutation(mutator: HypothesisMutator) -> dict:
    return {
        "part_id": PART_ID,
        "mutations_requested": mutator.standing.mutations_requested,
        "mutated": mutator.standing.mutated,
        "families_exhausted": mutator.standing.families_exhausted,
        "retired_parents_refused_a_second_mutation": mutator.standing.retired_parents_refused,
        "measurement_changes_refused": mutator.standing.measurement_changes_refused,
        "by_mutation": dict(sorted(mutator.standing.by_mutation.items())),
        "largest_family": mutator.standing.largest_family,
        "trials_by_family": dict(sorted(mutator._trials.items())),
    }


def run_hypothesis_mutator(
    mutator: HypothesisMutator, control_socket, read_parents, publish_hypotheses,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        hypotheses = []
        for parent, mutation in read_parents(mutator):
            hypothesis, _ = mutator.mutate(parent, mutation)
            if hypothesis is not None:
                hypotheses.append(hypothesis)
        publish_hypotheses(tuple(hypotheses))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_mutation(mutator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A retired instruction whose archive history is known is a parent; the
    mutation tried is the first the family has not exhausted. Scorecards say
    whether an earlier mutation worked; near misses say where a threshold
    would have.
    """
    from runtime.input_assembly import Batch, LatestByKey

    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    retired = Batch(read=context.bus.reader("retired-instruction"))
    histories = LatestByKey(read=context.bus.reader("instruction-history"), key_of=lambda h: h.instruction_id)
    near_misses = Batch(read=context.bus.reader("near-miss-episode"))
    publish_hypotheses = context.bus.publisher_for("mutated-hypothesis")
    mutator = HypothesisMutator(
        threshold_step_fraction=context.number("hypothesis_threshold_step_fraction"),
        horizon_step_fraction=context.number("hypothesis_horizon_step_fraction"),
        consecutive_failures_before_stopping=int(context.number("hypothesis_failures_before_stopping")),
    )
    mutations = (MOVE_THE_THRESHOLD, CHANGE_THE_HORIZON, ADD_A_REGIME_CONDITION, REMOVE_A_REGIME_CONDITION)

    def read_parents(_mutator):
        for card in scorecards.payloads():
            if card.is_measured:
                mutator.observe_outcome(card.instruction_id.split("@")[0], card.expectancy is not None and card.expectancy > 0)
        for miss in near_misses.payloads():
            # `was_a_mistake_to_skip` is the episode's own verdict. It was read as
            # `would_have_worked` until 2026-08-25 -- a field NearMissEpisode has
            # never carried -- so every near miss was observed as one this system
            # was right to skip, which is the answer that teaches it nothing.
            mutator.observe_near_miss(
                miss.why_not_taken, float(miss.reference_price), miss.was_a_mistake_to_skip
            )
        by_id = histories.mapping()
        jobs = []
        for item in retired.payloads():
            if not item.may_be_mutated:
                continue
            history = by_id.get(item.instruction_id)
            if history is None:
                continue
            parent = ParentInstruction(
                instruction_id=item.instruction_id, family=history.family, measurement=history.measurement,
                comparison=history.comparison, threshold=history.threshold,
                horizon_seconds=history.horizon_seconds,
                regime_tag=history.regime_tag, trades=item.trades_at_retirement,
                hit_rate=(history.realised > 0) * 1.0 if history.trades else 0.0, was_retired=True,
            )
            for mutation in mutations:
                if not mutator.family_is_exhausted(history.family):
                    jobs.append((parent, mutation))
                    break
        return tuple(jobs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_hypotheses(kept)

    return run_hypothesis_mutator(
        mutator=mutator,
        control_socket=context.control_socket,
        read_parents=read_parents,
        publish_hypotheses=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
