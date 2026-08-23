"""instruction-retirer: taking an instruction out before it has finished losing.

Retirement is the decision most systems make too late, because the evidence that
an edge has gone is the same evidence as an edge having a bad run, and waiting
for certainty means waiting until the losses are certain.

So an instruction is retired on any one of these, and each is a different kind of
"it has stopped being true":

- **Its own falsification criterion has fired.** The criterion was written before
  the data; when it is met, the hypothesis is refuted and arguing is arguing with
  a promise the system made to itself.
- **The regime it was tagged for has broken.** The instruction may still be true
  in that regime -- but that regime does not exist, so it cannot fire.
- **Its edge half-life has run out.** Retired *before* the record turns negative,
  because by the time it does the instruction has been losing for a while.
- **The live-versus-replay gap is too wide.** An instruction that works only in
  replay never worked; the backtest was optimistic, and continuing to trade it is
  paying to confirm that.

**Retirement is not deletion.** A retired instruction keeps its record and can be
resurrected if its regime returns -- deleting it would lose the evidence and make
the same idea look novel next month.

**A retired instruction may be mutated once.** Once, because thirty variants of a
dead edge is a system arguing with its own evidence.

**Every retirement names which condition fired.** "Retired" is not actionable;
"its own falsification criterion fired at 340 trades" is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-retirer"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-retirer",
    consumes=(
        "instruction-scorecard", "regime-break-alert", "live-vs-replay-gap",
        "edge-half-life", "hypothesis-regime-tag", "falsification-criterion",
    ),
    produces=("retired-instruction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

STANDING = "standing"
RETIRED = "retired"
RESURRECTED = "resurrected"

CRITERION_FIRED = "its-own-falsification-criterion-fired"
REGIME_BROKE = "the-regime-it-was-tagged-for-no-longer-exists"
EDGE_DECAYED = "its-edge-half-life-has-run-out"
ONLY_WORKS_IN_REPLAY = "it-works-in-replay-and-not-live"


@dataclass(frozen=True)
class RetiredInstruction:
    """One instruction taken out, why, and what is kept."""

    instruction_id: str
    state: str
    because: str | None
    trades_at_retirement: int
    realised_at_retirement: float
    regime_tag: str | None
    may_be_mutated: bool
    may_be_resurrected: bool
    reason: str
    retired_at_ns: int

    @property
    def is_retired(self) -> bool:
        return self.state == RETIRED

    @property
    def was_deleted(self) -> bool:
        """Never. Deleting loses the evidence and makes the idea look novel next month."""
        return False


@dataclass
class RetirerStanding:
    checks: int = 0
    retirements: int = 0
    resurrections: int = 0
    mutations_permitted: int = 0
    second_mutations_refused: int = 0
    by_cause: dict = field(default_factory=dict)
    earliest_retirement_trades: int | None = None


class InstructionRetirer:
    """Retires an instruction on any one of four conditions, and keeps its record."""

    def __init__(self, replay_gap_threshold: float, now_ns=time.time_ns) -> None:
        if replay_gap_threshold <= 0:
            raise ValueError(
                "with no threshold every instruction has a replay gap and the condition stops "
                "distinguishing anything"
            )
        self._gap_threshold = replay_gap_threshold
        self._now_ns = now_ns
        self._retired: dict[str, RetiredInstruction] = {}
        self._mutated: set[str] = set()
        self._broken_regimes: set[str] = set()
        self._criteria_fired: set[str] = set()
        self._half_lives: dict[str, tuple] = {}
        self._gaps: dict[str, float] = {}
        self._tags: dict[str, str | None] = {}
        self.standing = RetirerStanding()

    def observe_criterion_fired(self, instruction_id: str) -> None:
        """The criterion was written before the data; arguing with it now is arguing
        with a promise the system made to itself."""
        self._criteria_fired.add(instruction_id)

    def observe_regime_break(self, regime: str, has_broken: bool) -> None:
        if has_broken:
            self._broken_regimes.add(regime)
        else:
            self._broken_regimes.discard(regime)

    def observe_regime_tag(self, instruction_id: str, regime: str | None) -> None:
        self._tags[instruction_id] = regime

    def observe_edge_half_life(
        self, instruction_id: str, half_life_trades: float, trades_so_far: int
    ) -> None:
        self._half_lives[instruction_id] = (half_life_trades, trades_so_far)

    def observe_live_versus_replay_gap(self, instruction_id: str, gap: float) -> None:
        self._gaps[instruction_id] = gap

    def check(
        self, instruction_id: str, trades: int, realised: float
    ) -> RetiredInstruction:
        self.standing.checks += 1

        if instruction_id in self._retired:
            record = self._retired[instruction_id]
            # A retired instruction whose regime has returned can come back.
            tag = self._tags.get(instruction_id)
            if record.because == REGIME_BROKE and tag is not None and tag not in self._broken_regimes:
                del self._retired[instruction_id]
                self.standing.resurrections += 1
                return self._record(
                    instruction_id, RESURRECTED, None, trades, realised, tag, False, True,
                    f"the {tag} regime has returned, so this instruction comes back with its "
                    f"record intact -- which is why retirement is not deletion",
                )
            return record

        cause = self._cause_for(instruction_id, trades)
        if cause is None:
            return self._record(
                instruction_id, STANDING, None, trades, realised,
                self._tags.get(instruction_id), False, False,
                f"nothing has fired: the falsification criterion stands, the regime holds, the "
                f"edge has not decayed past its half-life, and live matches replay",
            )

        self.standing.retirements += 1
        self.standing.by_cause[cause] = self.standing.by_cause.get(cause, 0) + 1
        if (
            self.standing.earliest_retirement_trades is None
            or trades < self.standing.earliest_retirement_trades
        ):
            self.standing.earliest_retirement_trades = trades

        record = self._record(
            instruction_id, RETIRED, cause, trades, realised,
            self._tags.get(instruction_id), True, cause == REGIME_BROKE,
            self._explain(cause, instruction_id, trades),
        )
        self._retired[instruction_id] = record
        return record

    def permit_mutation(self, instruction_id: str) -> bool:
        """Once. Thirty variants of a dead edge is a system arguing with its evidence."""
        if instruction_id not in self._retired:
            return False
        if instruction_id in self._mutated:
            self.standing.second_mutations_refused += 1
            return False
        self._mutated.add(instruction_id)
        self.standing.mutations_permitted += 1
        return True

    def _cause_for(self, instruction_id: str, trades: int) -> str | None:
        if instruction_id in self._criteria_fired:
            return CRITERION_FIRED

        tag = self._tags.get(instruction_id)
        if tag is not None and tag in self._broken_regimes:
            return REGIME_BROKE

        half_life = self._half_lives.get(instruction_id)
        if half_life is not None and half_life[0] > 0 and trades >= half_life[0]:
            return EDGE_DECAYED

        gap = self._gaps.get(instruction_id)
        if gap is not None and gap > self._gap_threshold:
            return ONLY_WORKS_IN_REPLAY

        return None

    def _explain(self, cause: str, instruction_id: str, trades: int) -> str:
        if cause == CRITERION_FIRED:
            return (
                f"its own falsification criterion fired at {trades:,} trade(s). The criterion "
                f"was written before the data, so arguing with it now is arguing with a "
                f"promise this system made to itself"
            )
        if cause == REGIME_BROKE:
            return (
                f"the {self._tags.get(instruction_id)} regime it was tagged for has broken. It "
                f"may still be true there, but there does not exist, so it cannot fire. Its "
                f"record is kept and it comes back if that regime returns"
            )
        if cause == EDGE_DECAYED:
            half_life, _ = self._half_lives[instruction_id]
            return (
                f"{trades:,} trade(s) is past its {half_life:.0f}-trade half-life. Retired "
                f"before the record turns negative, because by the time it does this has been "
                f"losing for a while"
            )
        return (
            f"replay beats live by {self._gaps.get(instruction_id):+.4%} per trade, past the "
            f"{self._gap_threshold:.4%} that says it never worked. The backtest was optimistic, "
            f"and continuing to trade it is paying to confirm that"
        )

    def _record(
        self, instruction_id, state, cause, trades, realised, tag, may_mutate, may_resurrect, reason
    ) -> RetiredInstruction:
        return RetiredInstruction(
            instruction_id=instruction_id,
            state=state,
            because=cause,
            trades_at_retirement=trades,
            realised_at_retirement=realised,
            regime_tag=tag,
            may_be_mutated=may_mutate,
            may_be_resurrected=may_resurrect,
            reason=reason,
            retired_at_ns=self._now_ns(),
        )


def describe_retirement(retirer: InstructionRetirer) -> dict:
    return {
        "part_id": PART_ID,
        "checks": retirer.standing.checks,
        "retirements": retirer.standing.retirements,
        "resurrections": retirer.standing.resurrections,
        "mutations_permitted": retirer.standing.mutations_permitted,
        "second_mutations_refused": retirer.standing.second_mutations_refused,
        "by_cause": dict(sorted(retirer.standing.by_cause.items())),
        "earliest_retirement_trades": retirer.standing.earliest_retirement_trades,
        "currently_retired": sorted(retirer._retired),
        "retirement_is_deletion": False,
    }


def run_instruction_retirer(
    retirer: InstructionRetirer, control_socket, read_scorecards, publish_retirements,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        instructions = read_scorecards(retirer)
        publish_retirements(
            tuple(
                retirer.check(instruction_id, trades, realised)
                for instruction_id, trades, realised in instructions
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

    Every scorecard is a check. Before it, the conditions are refreshed from
    what arrived: a regime-break alert, a live-versus-replay gap, an edge
    half-life, a regime tag. The falsification criterion fires here, from the
    scorecard itself -- it names a measure, a comparison, a threshold and the
    trades it needs, and a scorecard that has those trades and sits on the
    wrong side of the threshold is the promise coming due. The gap that
    retires is the one the reconciler already calls inconsistent, at the same
    tolerance it uses.
    """
    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    alerts = Batch(read=context.bus.reader("regime-break-alert"))
    gaps = Batch(read=context.bus.reader("live-vs-replay-gap"))
    half_lives = Batch(read=context.bus.reader("edge-half-life"))
    tags = Batch(read=context.bus.reader("hypothesis-regime-tag"))
    criteria_in = Batch(read=context.bus.reader("falsification-criterion"))
    publish_retirements = context.bus.publisher_for("retired-instruction")
    retirer = InstructionRetirer(replay_gap_threshold=context.number("reconcile_consistency_tolerance"))
    criteria: dict[str, object] = {}

    def measured(card, measure: str) -> float | None:
        if measure == "hit-rate":
            return card.hit_rate.value if card.hit_rate.is_fitted else None
        if measure == "expectancy":
            return card.expectancy
        if measure == "realised":
            return card.realised
        return None

    def criterion_fires(card) -> bool:
        criterion = criteria.get(card.instruction_id)
        if criterion is None or card.trades < criterion.required_trades:
            return False
        value = measured(card, criterion.measure)
        if value is None:
            return False
        if criterion.comparison == "above":
            return value <= criterion.threshold
        if criterion.comparison == "below":
            return value >= criterion.threshold
        return False

    def read_scorecards(_retirer):
        for criterion in criteria_in.payloads():
            if criterion.required_trades is not None:
                criteria[criterion.hypothesis_id] = criterion
        for alert in alerts.payloads():
            retirer.observe_regime_break(alert.regime, bool(alert.has_broken))
        for gap in gaps.payloads():
            if gap.gap is not None:
                retirer.observe_live_versus_replay_gap(gap.instruction_id, float(gap.gap))
        for tag in tags.payloads():
            fires_in = tuple(tag.regimes_it_may_fire_in)
            retirer.observe_regime_tag(tag.hypothesis_id, fires_in[0] if len(fires_in) == 1 else None)
        checks = []
        for card in scorecards.payloads():
            if not card.is_measured:
                continue
            if criterion_fires(card):
                retirer.observe_criterion_fired(card.instruction_id)
            checks.append((card.instruction_id, int(card.trades), float(card.realised)))
        for half_life in half_lives.payloads():
            if half_life.half_life_trades is not None:
                retirer.observe_edge_half_life(
                    half_life.instruction_id, float(half_life.half_life_trades), int(half_life.trades_behind_it)
                )
        return tuple(checks)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_retirements(kept)

    return run_instruction_retirer(
        retirer=retirer,
        control_socket=context.control_socket,
        read_scorecards=read_scorecards,
        publish_retirements=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
