"""hypothesis-falsifier: what would have to be observed for this to be wrong.

A hypothesis without a refutation criterion is a story about the past. It cannot
lose, so it cannot be tested, so accumulating them makes a system more confident
and no more correct.

This part turns a claim into something that can fail, and refuses the ones that
cannot. The criterion has to be:

- **Observable.** "The regime was unfavourable" is not a criterion, because
  nothing measures it at the moment the trade resolves. "Hit rate at or below
  the base rate over the required sample" is.
- **Stated in advance, with a sample size.** A criterion agreed after the data
  is a criterion chosen to fit it, and one without a sample size is satisfied by
  whichever direction the first few trades went.
- **Reachable.** A criterion that would need more trades than the system can
  produce cannot refute anything, and a hypothesis carrying one is untestable in
  a way that reads as unrefuted.

**A hypothesis that cannot be falsified is rejected, not weakened.** Softening it
into something vaguer makes it harder to refute rather than easier, which is
backwards.

**The criterion is checked against real outcomes and can fire.** A falsifier that
only ever writes criteria is a formality; this one evaluates them, and a
hypothesis whose criterion is met is reported as refuted so it can be retired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hypothesis-falsifier"

PART_DECLARATION = PartDeclaration(
    part_id="hypothesis-falsifier",
    consumes=("candidate-formula", "inverted-hypothesis", "novel-idea"),
    produces=("falsification-criterion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WRITTEN = "written"
NOT_OBSERVABLE = "nothing-measures-this-at-the-moment-a-trade-resolves"
NO_SAMPLE_SIZE = "a-criterion-with-no-sample-size-is-met-by-the-first-few-trades"
UNREACHABLE = "it-would-need-more-trades-than-this-system-can-produce"

STANDING = "standing"
REFUTED = "refuted"
NOT_YET_DECIDABLE = "not-yet-enough-trades-to-decide"

# What a criterion may be measured on. Closed on purpose: a criterion naming
# something nothing measures cannot fire, and a hypothesis carrying it is
# untestable in a way that reads as unrefuted.
OBSERVABLE_MEASURES = (
    "hit-rate",
    "expectancy",
    "median-realised",
    "directional-accuracy",
    "capture-of-peak",
)


@dataclass(frozen=True)
class FalsificationCriterion:
    """What would have to be observed for a hypothesis to be wrong."""

    hypothesis_id: str
    state: str
    measure: str
    comparison: str
    threshold: float
    required_trades: int
    written_before_any_trade: bool
    reason: str
    written_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == WRITTEN

    def is_met_by(self, observed: float, trades: int) -> str:
        """Whether the observed record refutes the hypothesis yet."""
        if trades < self.required_trades:
            return NOT_YET_DECIDABLE
        if self.comparison == "at-or-below":
            return REFUTED if observed <= self.threshold else STANDING
        return REFUTED if observed >= self.threshold else STANDING


@dataclass
class FalsifierStanding:
    criteria_requested: int = 0
    written: int = 0
    rejected_not_observable: int = 0
    rejected_no_sample_size: int = 0
    rejected_unreachable: int = 0
    evaluations: int = 0
    refuted: int = 0
    still_standing: int = 0
    not_yet_decidable: int = 0


class HypothesisFalsifier:
    """Writes what would refute a hypothesis, and evaluates it when the trades arrive."""

    def __init__(self, maximum_reachable_trades: int, now_ns=time.time_ns) -> None:
        if maximum_reachable_trades < 1:
            raise ValueError("a criterion needing no trades cannot be evaluated")
        self._maximum = maximum_reachable_trades
        self._now_ns = now_ns
        self._criteria: dict[str, FalsificationCriterion] = {}
        self._trades_when_written: dict[str, int] = {}
        self.standing = FalsifierStanding()

    def write(
        self,
        hypothesis_id: str,
        measure: str,
        comparison: str,
        threshold: float,
        required_trades: int,
        trades_already_taken: int = 0,
    ) -> FalsificationCriterion:
        """One criterion, stated before the data rather than fitted to it."""
        self.standing.criteria_requested += 1

        if measure not in OBSERVABLE_MEASURES:
            # Nothing measures it at the moment a trade resolves, so it can
            # never fire -- and a hypothesis carrying it reads as unrefuted.
            self.standing.rejected_not_observable += 1
            return self._criterion(
                hypothesis_id, NOT_OBSERVABLE, measure, comparison, threshold, required_trades,
                trades_already_taken == 0,
                f"{measure!r} is not one of {', '.join(OBSERVABLE_MEASURES)}. A criterion "
                f"nothing measures cannot fire, and the hypothesis would read as unrefuted "
                f"forever",
            )

        if required_trades < 1:
            self.standing.rejected_no_sample_size += 1
            return self._criterion(
                hypothesis_id, NO_SAMPLE_SIZE, measure, comparison, threshold, required_trades,
                trades_already_taken == 0,
                "a criterion with no sample size is satisfied by whichever direction the first "
                "few trades happened to go",
            )

        if required_trades > self._maximum:
            self.standing.rejected_unreachable += 1
            return self._criterion(
                hypothesis_id, UNREACHABLE, measure, comparison, threshold, required_trades,
                trades_already_taken == 0,
                f"{required_trades:,} trade(s) is past the {self._maximum:,} this system can "
                f"produce; a criterion that can never be reached cannot refute anything",
            )

        criterion = self._criterion(
            hypothesis_id, WRITTEN, measure, comparison, threshold, required_trades,
            trades_already_taken == 0,
            f"refuted if {measure} is {comparison} {threshold:.4g} over {required_trades:,} "
            f"trade(s)"
            + (
                ". Written before any trade, so it was not chosen to fit the data"
                if trades_already_taken == 0
                else f". Written after {trades_already_taken} trade(s) had already been taken, "
                f"which is worth knowing when this is evaluated"
            ),
        )
        self._criteria[hypothesis_id] = criterion
        self._trades_when_written[hypothesis_id] = trades_already_taken
        self.standing.written += 1
        return criterion

    def evaluate(self, hypothesis_id: str, observed: float, trades: int) -> tuple[str, str]:
        """Whether the record has refuted the hypothesis, and why."""
        self.standing.evaluations += 1
        criterion = self._criteria.get(hypothesis_id)
        if criterion is None:
            return NOT_YET_DECIDABLE, (
                f"no falsification criterion was written for {hypothesis_id}, so nothing about "
                f"it can be refuted -- which is the state this part exists to prevent"
            )

        verdict = criterion.is_met_by(observed, trades)
        if verdict == REFUTED:
            self.standing.refuted += 1
        elif verdict == STANDING:
            self.standing.still_standing += 1
        else:
            self.standing.not_yet_decidable += 1

        return verdict, (
            f"{criterion.measure} is {observed:.4g} over {trades:,} trade(s) against a "
            f"criterion of {criterion.comparison} {criterion.threshold:.4g} at "
            f"{criterion.required_trades:,}: "
            + {
                REFUTED: "refuted, and it can be retired",
                STANDING: "still standing, which is not the same as demonstrated",
                NOT_YET_DECIDABLE: "not yet enough trades to decide either way",
            }[verdict]
        )

    def criterion_for(self, hypothesis_id: str) -> FalsificationCriterion | None:
        return self._criteria.get(hypothesis_id)

    def _criterion(
        self, hypothesis_id, state, measure, comparison, threshold, required_trades,
        before_any_trade, reason,
    ) -> FalsificationCriterion:
        return FalsificationCriterion(
            hypothesis_id=hypothesis_id,
            state=state,
            measure=measure,
            comparison=comparison,
            threshold=threshold,
            required_trades=required_trades,
            written_before_any_trade=before_any_trade,
            reason=reason,
            written_at_ns=self._now_ns(),
        )


def describe_falsification(falsifier: HypothesisFalsifier) -> dict:
    return {
        "part_id": PART_ID,
        "criteria_requested": falsifier.standing.criteria_requested,
        "written": falsifier.standing.written,
        "rejected_not_observable": falsifier.standing.rejected_not_observable,
        "rejected_no_sample_size": falsifier.standing.rejected_no_sample_size,
        "rejected_unreachable": falsifier.standing.rejected_unreachable,
        "evaluations": falsifier.standing.evaluations,
        "refuted": falsifier.standing.refuted,
        "still_standing": falsifier.standing.still_standing,
        "not_yet_decidable": falsifier.standing.not_yet_decidable,
        "observable_measures": list(OBSERVABLE_MEASURES),
    }


def run_hypothesis_falsifier(
    falsifier: HypothesisFalsifier, control_socket, read_hypotheses, publish_criteria,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_criteria(
            tuple(
                falsifier.write(*request) for request in read_hypotheses(falsifier)
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

    A criterion is written for every hypothesis the moment it arrives,
    before any trade: the measure it claims, the comparison, the threshold
    it must clear and the trades it needs. An idea with no measurable claim
    is passed over, which the falsifier counts.
    """
    from runtime.input_assembly import Batch

    formulas = Batch(read=context.bus.reader("candidate-formula"))
    inverted = Batch(read=context.bus.reader("inverted-hypothesis"))
    ideas = Batch(read=context.bus.reader("novel-idea"))
    publish_criteria = context.bus.publisher_for("falsification-criterion")
    falsifier = HypothesisFalsifier(maximum_reachable_trades=int(context.number("hypothesis_maximum_reachable_trades")))

    def read_hypotheses(_falsifier):
        requests = []
        for source in (formulas, inverted, ideas):
            for item in source.payloads():
                hypothesis_id = getattr(item, "hypothesis_id", None) or getattr(item, "formula_id", None) or getattr(item, "idea_id", None)
                claimed = getattr(item, "fitted_hit_rate", None)
                context_of = getattr(item, "context", None) or {}
                if claimed is None and isinstance(context_of, dict):
                    claimed = context_of.get("claimed_hit_rate")
                base = getattr(item, "base_rate", None) or (context_of.get("base_rate") if isinstance(context_of, dict) else None)
                required = getattr(item, "required_sample_size", None) or (context_of.get("required_trades") if isinstance(context_of, dict) else None)
                if hypothesis_id is None or claimed is None or base is None:
                    continue
                requests.append((hypothesis_id, "hit-rate", "above", float(base), int(required or context.number("decoding_minimum_trades")), 0))
        return tuple(requests)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_criteria(kept)

    return run_hypothesis_falsifier(
        falsifier=falsifier,
        control_socket=context.control_socket,
        read_hypotheses=read_hypotheses,
        publish_criteria=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
