"""conservation-planner: what to stop doing when the runway shortens.

Turning things off under pressure is where a system destroys itself, because the
obvious economy is usually a protection. The risk gate costs almost nothing and stops
everything from being lost; the research agents cost a great deal and produce
something eventually. Under pressure the temptation is to keep what feels productive.

So the ordering here is explicit and its top and bottom are fixed:

- **Nothing that protects capital is ever in the list.** Risk gates, exposure views,
  halt deciders, the override reader and the ledger are named as never-stopping, and
  a plan naming any of them is refused rather than trimmed. This is a hard rule, not
  a heuristic ordering.
- **Discretionary work goes first**, ordered by resource cost per unit of value it
  has actually produced -- measured, not assumed. Research that has not produced a
  usable finding in a week is the cheapest thing to lose.
- **Trading is stopped before the machinery that watches trades.** A system with
  positions open and no monitoring is worse than a system with no positions, so
  entering stops first and everything that watches an open position stops last.

Each tier's plan is a superset of the tier above it, so deepening the cut never
resurrects something that was already stopped -- a plan that oscillates a part on and
off costs more than either state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import (
    COMFORTABLE, ConservationPlan, CRITICAL, FRUGAL, SHUTDOWN, SURVIVAL_TIERS,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "conservation-planner"

PART_DECLARATION = PartDeclaration(
    part_id="conservation-planner",
    consumes=("survival-tier",),
    produces=("conservation-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PLANNED = "planned"
NOTHING_TO_CUT = "this-tier-requires-no-cut"
REFUSED_PROTECTS_CAPITAL = "a-part-that-protects-capital-was-named-for-stopping"
UNKNOWN_TIER = "not-a-tier-this-planner-knows"

# Ordered classes of work, cheapest to lose first. Trading is stopped before the
# machinery that watches trades.
DISCRETIONARY_RESEARCH = "discretionary-research"
MODEL_TRAINING = "model-training"
NARRATIVE_AND_EXPLANATION = "narrative-and-explanation"
OPPORTUNITY_SCANNING = "opportunity-scanning"
NEW_POSITION_ENTRY = "new-position-entry"
POSITION_MANAGEMENT = "position-management"

CUT_ORDER = (
    DISCRETIONARY_RESEARCH, MODEL_TRAINING, NARRATIVE_AND_EXPLANATION,
    OPPORTUNITY_SCANNING, NEW_POSITION_ENTRY, POSITION_MANAGEMENT,
)

# How deep each tier cuts into that order.
CUT_DEPTH = {COMFORTABLE: 0, FRUGAL: 2, CRITICAL: 4, SHUTDOWN: 5}


@dataclass(frozen=True)
class PlanOutcome:
    tier: str
    state: str
    plan: ConservationPlan | None
    refused_parts: tuple
    reason: str
    planned_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.plan is not None


@dataclass
class PlannerStanding:
    plans_made: int = 0
    parts_stopped: int = 0
    refusals: int = 0
    protected_parts_declared: int = 0
    times_a_protection_was_proposed_for_stopping: int = 0


class ConservationPlanner:
    """Orders what to stop, and refuses to stop anything that protects capital."""

    def __init__(self, protected_parts, now_ns=time.time_ns) -> None:
        if not protected_parts:
            raise ValueError(
                "some parts must never stop; a planner with none can shut off the risk "
                "gate to save quota"
            )
        self._protected = set(protected_parts)
        self._now_ns = now_ns
        self._classes: dict[str, str] = {}
        self._value: dict[str, float] = {}
        self._cost: dict[str, float] = {}
        self.standing = PlannerStanding()
        self.standing.protected_parts_declared = len(self._protected)

    def declare_part(self, part_id: str, work_class: str) -> None:
        if work_class not in CUT_ORDER:
            raise ValueError(
                f"{work_class!r} is not a class of work this planner orders. An "
                f"unclassified part would be cut in an arbitrary place"
            )
        self._classes[part_id] = work_class

    def observe_value(self, part_id: str, value_produced: float, resource_cost: float) -> None:
        """Measured, not assumed: research that produced nothing is cheapest to lose."""
        self._value[part_id] = value_produced
        self._cost[part_id] = resource_cost

    def value_per_resource(self, part_id: str) -> float:
        cost = self._cost.get(part_id, 0.0)
        if cost <= 0:
            return float("inf")
        return self._value.get(part_id, 0.0) / cost

    def parts_in_class(self, work_class: str) -> tuple:
        """Protected parts are not filtered out here on purpose.

        Filtering them silently would make the refusal below unreachable, and an
        unreachable guard is one nobody finds out is broken. A protected part
        classified as cuttable is a mistake somebody made, and the plan says so.
        """
        return tuple(
            sorted(
                (
                    part_id
                    for part_id, name in self._classes.items()
                    if name == work_class
                ),
                key=self.value_per_resource,
            )
        )

    def plan(self, tier: str) -> PlanOutcome:
        if tier not in CUT_DEPTH:
            return self._outcome(
                tier, UNKNOWN_TIER, None, (),
                f"{tier!r} is not a tier this planner knows",
            )

        depth = CUT_DEPTH[tier]
        if depth == 0:
            return self._outcome(
                tier, NOTHING_TO_CUT,
                ConservationPlan(
                    tier=tier, parts_to_stop=(),
                    parts_that_never_stop=tuple(sorted(self._protected)),
                    expected_saving_fraction=0.0,
                    reason="this tier requires no cut",
                    planned_at_ns=self._now_ns(),
                ),
                (),
                "this tier requires no cut",
            )

        to_stop: list = []
        for work_class in CUT_ORDER[:depth]:
            to_stop.extend(self.parts_in_class(work_class))

        protected_named = tuple(part_id for part_id in to_stop if part_id in self._protected)
        if protected_named:
            self.standing.refusals += 1
            self.standing.times_a_protection_was_proposed_for_stopping += len(protected_named)
            return self._outcome(
                tier, REFUSED_PROTECTS_CAPITAL, None, protected_named,
                f"{', '.join(protected_named)} protect(s) capital and was named for "
                f"stopping. The plan is refused rather than trimmed: this is a hard rule, "
                f"not a heuristic ordering",
            )

        total_cost = sum(self._cost.get(part_id, 0.0) for part_id in self._classes)
        saved = sum(self._cost.get(part_id, 0.0) for part_id in to_stop)
        fraction = saved / total_cost if total_cost > 0 else 0.0

        self.standing.plans_made += 1
        self.standing.parts_stopped += len(to_stop)

        return self._outcome(
            tier, PLANNED,
            ConservationPlan(
                tier=tier,
                parts_to_stop=tuple(to_stop),
                parts_that_never_stop=tuple(sorted(self._protected)),
                expected_saving_fraction=fraction,
                reason=(
                    f"{len(to_stop)} part(s) across {depth} class(es) of work, saving "
                    f"about {fraction:.0%} of the measured cost. The cheapest work to "
                    f"lose goes first, by value actually produced per unit of resource, "
                    f"and everything that watches an open position stops last"
                ),
                planned_at_ns=self._now_ns(),
            ),
            (),
            f"{len(to_stop)} part(s) to stop at tier {tier}",
        )

    def plans_are_nested(self) -> bool:
        """Deepening the cut never resurrects something already stopped."""
        previous: set = set()
        for tier in SURVIVAL_TIERS:
            outcome = self.plan(tier)
            if outcome.plan is None:
                continue
            current = set(outcome.plan.parts_to_stop)
            if not previous <= current:
                return False
            previous = current
        return True

    def _outcome(self, tier, state, plan, refused, reason) -> PlanOutcome:
        return PlanOutcome(
            tier=tier, state=state, plan=plan, refused_parts=refused, reason=reason,
            planned_at_ns=self._now_ns(),
        )


def describe_conservation(planner: ConservationPlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans_made": planner.standing.plans_made,
        "parts_stopped": planner.standing.parts_stopped,
        "refusals": planner.standing.refusals,
        "protected_parts": sorted(planner._protected),
        "times_a_protection_was_proposed_for_stopping": (
            planner.standing.times_a_protection_was_proposed_for_stopping
        ),
        "cut_order": list(CUT_ORDER),
        "can_stop_a_part_that_protects_capital": False,
        "plans_are_nested": planner.plans_are_nested(),
    }


def run_conservation_planner(
    planner: ConservationPlanner, control_socket, read_tier, publish_plans,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for tier in read_tier():
            outcome = planner.plan(tier)
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


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Which parts a cut may reach is computed from the blueprint: each class of
    work names the blocks whose parts it covers, in a setting the operator
    owns, and a block named in no class is simply never in a plan -- the tape,
    the ledger, the risk gates and this block itself are protected by absence
    as well as by the protected list. Value per resource is unmeasured in
    phase 1, so within a class the cut order falls back to name order; that is
    a measurement gap the plan's saving fraction reports as zero rather than
    a number anyone invented.
    """
    from runtime.input_assembly import Batch
    from runtime.wiring_plan import load_blueprint

    tiers = Batch(read=context.bus.reader("survival-tier"))
    publish_plans = context.bus.publisher_for("conservation-plan")

    planner = ConservationPlanner(
        protected_parts=tuple(
            str(part) for part in context.setting("conservation_protected_parts").value
        ),
    )
    classes = {
        DISCRETIONARY_RESEARCH: "conservation_discretionary_research_blocks",
        MODEL_TRAINING: "conservation_model_training_blocks",
        NARRATIVE_AND_EXPLANATION: "conservation_narrative_blocks",
        OPPORTUNITY_SCANNING: "conservation_opportunity_scanning_blocks",
        NEW_POSITION_ENTRY: "conservation_new_position_entry_blocks",
        POSITION_MANAGEMENT: "conservation_position_management_blocks",
    }
    block_class = {}
    for work_class, setting_name in classes.items():
        for block in context.setting(setting_name).value:
            block_class[str(block)] = work_class
    for feature in load_blueprint()["features"]:
        work_class = block_class.get(feature["category"])
        if work_class is not None:
            planner.declare_part(feature["id"], work_class)

    def read_tier():
        return tuple(tier.tier for tier in tiers.payloads())

    return run_conservation_planner(
        planner=planner,
        control_socket=context.control_socket,
        read_tier=read_tier,
        publish_plans=lambda plan: publish_plans((plan,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
