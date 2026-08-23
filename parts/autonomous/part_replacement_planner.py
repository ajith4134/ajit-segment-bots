"""part-replacement-planner: how to swap a broken part without a gap in the middle.

Replacing a part in a running system is not a restart. Between the old one stopping
and the new one producing there is a window where the data type that part produces
does not exist, and every consumer of it sees the same thing it would see if the
market had gone quiet. Some of them will act on that.

So a plan is a sequence, and its shape depends entirely on what the part does:

- **A part whose skipped tick merely delays** can be stopped and replaced in place.
  Consumers wait a little longer, which is exactly what the declaration says is safe.
- **A part whose skipped tick corrupts** cannot have a gap at all. The replacement is
  started first, both run briefly, and only then is the old one stopped. That costs
  double resources for a moment and is the only correct order.
- **A part holding capital-bearing state** needs its state handed over explicitly,
  and if that cannot be done the plan requires a pause with positions flat -- because
  a replacement that loses track of a position is worse than the fault it was fixing.

**Every plan is reversible or it is not a plan.** The step that puts the old part back
is written before the swap begins, not worked out afterwards under pressure. A plan
whose rollback path cannot be stated is refused, which is the same rule the admission
gate applies for the same reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import ReplacementPlan
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-replacement-planner"

PART_DECLARATION = PartDeclaration(
    part_id="part-replacement-planner",
    consumes=("part-fault", "admitted-part"),
    produces=("replacement-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PLANNED = "planned"
NO_REPLACEMENT = "nothing-has-been-admitted-to-replace-it-with"
NOT_REVERSIBLE = "the-rollback-path-cannot-be-stated"
NEEDS_A_FLAT_BOOK = "its-state-cannot-be-handed-over-so-the-book-must-be-flat"
UNKNOWN_PART = "nothing-is-declared-about-this-part"

# The two orders a swap can happen in.
STOP_THEN_START = "stop-then-start"
START_THEN_STOP = "start-then-stop"


@dataclass(frozen=True)
class PlanOutcome:
    part_id: str
    state: str
    plan: ReplacementPlan | None
    order: str | None
    reason: str
    planned_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == PLANNED and self.plan is not None


@dataclass
class PlannerStanding:
    plans_made: int = 0
    overlapping_swaps: int = 0
    in_place_swaps: int = 0
    refused_no_replacement: int = 0
    refused_not_reversible: int = 0
    plans_requiring_a_pause: int = 0
    refused_unknown_part: int = 0


class PartReplacementPlanner:
    """Writes the swap sequence the part's own declaration requires."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._declarations: dict[str, dict] = {}
        self._replacements: dict[str, str] = {}
        self._state_handover: dict[str, bool] = {}
        self._current_source: dict[str, str] = {}
        self.standing = PlannerStanding()

    def declare_part(
        self, part_id: str, skipped_tick_effect: str, holds_capital_state: bool,
        can_hand_over_state: bool, current_source: str,
    ) -> None:
        self._declarations[part_id] = {
            "skipped_tick_effect": skipped_tick_effect,
            "holds_capital_state": holds_capital_state,
        }
        self._state_handover[part_id] = can_hand_over_state
        self._current_source[part_id] = current_source

    def observe_admitted(self, admitted) -> None:
        self._replacements[admitted.part_id] = admitted.proposal_id

    def plan(self, fault) -> PlanOutcome:
        part_id = fault.part_id
        declaration = self._declarations.get(part_id)
        if declaration is None:
            self.standing.refused_unknown_part += 1
            return self._outcome(
                part_id, UNKNOWN_PART, None, None,
                f"nothing is declared about {part_id}, so the correct swap order is "
                f"unknown and guessing it is how a gap appears in the middle",
            )

        replacement = self._replacements.get(part_id)
        if replacement is None:
            self.standing.refused_no_replacement += 1
            return self._outcome(
                part_id, NO_REPLACEMENT, None, None,
                f"nothing has been admitted to replace {part_id}. A fault with no "
                f"replacement is a fault, not a plan",
            )

        before = self._current_source.get(part_id)
        if before is None:
            self.standing.refused_not_reversible += 1
            return self._outcome(
                part_id, NOT_REVERSIBLE, None, None,
                "the current source is unknown, so the rollback path cannot be stated. A "
                "plan whose rollback cannot be written before the swap is refused",
            )

        holds_capital = declaration["holds_capital_state"]
        can_hand_over = self._state_handover.get(part_id, False)
        requires_pause = holds_capital and not can_hand_over

        # A part whose skipped tick corrupts cannot have a gap at all.
        corrupts = declaration["skipped_tick_effect"] == "corrupts"
        order = START_THEN_STOP if corrupts else STOP_THEN_START

        steps: list = [f"record the rollback target: {before}"]
        if requires_pause:
            self.standing.plans_requiring_a_pause += 1
            steps.append("halt entering and wait for the book to be flat")
        if order == START_THEN_STOP:
            steps.extend(
                [
                    f"start {replacement} alongside {part_id}",
                    "wait for the replacement to produce one healthy tick",
                    f"stop {part_id}",
                ]
            )
            self.standing.overlapping_swaps += 1
        else:
            steps.extend([f"stop {part_id}", f"start {replacement}"])
            self.standing.in_place_swaps += 1
        if holds_capital and can_hand_over:
            steps.insert(-1, f"hand {part_id}'s state to {replacement}")
        steps.append("confirm the replacement is producing, or roll back")

        self.standing.plans_made += 1
        return self._outcome(
            part_id, PLANNED,
            ReplacementPlan(
                part_id=part_id,
                faulty_reason=fault.detail,
                replacement_source=replacement,
                steps=tuple(steps),
                is_reversible=True,
                requires_a_pause=requires_pause,
                reason=(
                    f"{order} because a skipped tick "
                    + (
                        "corrupts: the replacement runs alongside first, which costs "
                        "double resources for a moment and is the only correct order"
                        if corrupts
                        else "merely delays, so consumers can wait through an in-place swap"
                    )
                    + (
                        ". Its state cannot be handed over, so the plan requires a flat "
                        "book: a replacement that loses track of a position is worse than "
                        "the fault it was fixing"
                        if requires_pause
                        else ""
                    )
                    + f". The rollback to {before} is written before the swap begins"
                ),
                planned_at_ns=self._now_ns(),
            ),
            order,
            f"{len(steps)} step(s), {order}",
        )

    def _outcome(self, part_id, state, plan, order, reason) -> PlanOutcome:
        return PlanOutcome(
            part_id=part_id, state=state, plan=plan, order=order, reason=reason,
            planned_at_ns=self._now_ns(),
        )


def describe_replacement_planning(planner: PartReplacementPlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans_made": planner.standing.plans_made,
        "overlapping_swaps": planner.standing.overlapping_swaps,
        "in_place_swaps": planner.standing.in_place_swaps,
        "refused_no_replacement": planner.standing.refused_no_replacement,
        "refused_not_reversible": planner.standing.refused_not_reversible,
        "plans_requiring_a_pause": planner.standing.plans_requiring_a_pause,
        "refused_unknown_part": planner.standing.refused_unknown_part,
        "swap_orders": [STOP_THEN_START, START_THEN_STOP],
        "writes_an_irreversible_plan": False,
        "leaves_a_gap_for_a_corrupting_part": False,
    }


def run_part_replacement_planner(
    planner: PartReplacementPlanner, control_socket, read_faults, publish_plans,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for fault in read_faults():
            outcome = planner.plan(fault)
            if outcome.is_usable:
                publish_plans(outcome.plan)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
