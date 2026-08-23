"""tail-opinion-composer: the tailgater's answer, and the one it is not allowed to give.

Assembles the calibrated conviction, the trailing plan and the follow candidate
into one opinion, or refuses and names what failed -- the same job as the two
directional composers, with one prohibition that is structural rather than a
threshold.

**It can never propose a reversal.** The blueprint says this bot always trails
and never targets, and never proposes a reversal. That is enforced here rather
than assumed: the opinion's direction must equal the direction of the move being
joined, and an opinion that would point the other way is refused as a defect
rather than published as a contrarian call. Without that check this bot would be
a third directional opinion reaching the arbiter with none of a directional bot's
outlier rejection, exit planning or invalidation watching.

**It publishes no target either.** The plan it carries has a trail as its only
exit, and the composer refuses a plan that names anything else -- because a target
arriving from a replaced planner would silently turn this into a momentum bot.

**Every stand-down is published**, so a move nobody joined is distinguishable
from a move nobody looked at (Rule 8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CONVICTION_TOO_LOW, ENTER_NOW, NO_EXIT_PLAN, STAND_DOWN, DirectionalOpinion, stand_down,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-opinion-composer"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-opinion-composer",
    consumes=("tail-calibrated-conviction", "tail-exit-plan", "follow-candidate"),
    produces=("directional-opinion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WOULD_BE_A_REVERSAL = "this-bot-may-never-propose-a-reversal"
PLAN_NAMES_A_TARGET = "this-bot-may-never-name-a-target"
PLAN_IS_FOR_ANOTHER_MOVE = "the-plan-does-not-match-the-move-being-joined"


@dataclass
class ComposerStanding:
    opinions_composed: int = 0
    calls_to_act: int = 0
    stood_down: int = 0
    reversals_refused: int = 0
    targets_refused: int = 0
    by_refusal: dict = field(default_factory=dict)
    strongest_conviction_acted_on: float = 0.0


class TailOpinionComposer:
    """Assembles one follow, and refuses anything that would make this a directional bot."""

    def __init__(
        self,
        conviction_floor: ConvictionFloor,
        require_trained_model: bool,
        now_ns=time.time_ns,
    ) -> None:
        self._floor = conviction_floor
        self._require_trained_model = require_trained_model
        self._now_ns = now_ns
        self.standing = ComposerStanding()

    def compose(self, candidate, conviction, exit_plan) -> DirectionalOpinion:
        self.standing.opinions_composed += 1
        venue_id, symbol = candidate.venue_id, candidate.symbol

        if conviction.side != candidate.direction:
            # Structural, not a threshold: an opinion pointing away from the move
            # being joined is a reversal, and this bot has none of a directional
            # bot's checks behind it.
            self.standing.reversals_refused += 1
            return self._stand_down(
                venue_id, symbol, candidate.direction, WOULD_BE_A_REVERSAL,
                f"the conviction is for {conviction.side} while the move being joined is "
                f"{candidate.direction}; this bot follows and never reverses, so this is "
                f"refused as a defect rather than published as a contrarian call",
                conviction.calibrated,
            )

        plan_floor = (
            self._floor.for_plan(exit_plan.reward_to_risk, exit_plan.risk_fraction)
            if exit_plan is not None and exit_plan.is_complete else self._floor.before_any_plan()
        )
        floor, floor_reason = plan_floor
        if conviction.probability < floor:
            return self._stand_down(
                venue_id, symbol, candidate.direction, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%} against a floor of "
                f"{floor:.1%} ({floor_reason})",
                conviction.calibrated,
            )

        if self._require_trained_model and not conviction.model_is_trained:
            return self._stand_down(
                venue_id, symbol, candidate.direction, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%} from a model that has trained on "
                f"{conviction.model_observations} outcome(s) of follows from {candidate.source}, "
                f"and this bot is configured to act only on a model that has seen enough of both "
                f"to be fitted",
                conviction.calibrated,
            )

        if exit_plan is None:
            return self._stand_down(
                venue_id, symbol, candidate.direction, NO_EXIT_PLAN,
                "no trailing plan could be built, so there is nothing to hold this follow with",
                conviction.calibrated,
            )

        if exit_plan.venue_id != venue_id or exit_plan.symbol != symbol:
            return self._stand_down(
                venue_id, symbol, candidate.direction, PLAN_IS_FOR_ANOTHER_MOVE,
                f"the plan is for {exit_plan.symbol} and the move being joined is {symbol}",
                conviction.calibrated,
            )

        if not self._is_trail_only(exit_plan):
            self.standing.targets_refused += 1
            return self._stand_down(
                venue_id, symbol, candidate.direction, PLAN_NAMES_A_TARGET,
                f"the plan names {len(exit_plan.targets)} exit(s) that are not the trail; this "
                f"bot joined a move whose size it cannot know, so a target would be a claim it "
                f"has no basis for",
                conviction.calibrated,
            )

        self.standing.calls_to_act += 1
        self.standing.strongest_conviction_acted_on = max(
            self.standing.strongest_conviction_acted_on, conviction.probability
        )

        return DirectionalOpinion(
            bot=BOT,
            side=candidate.direction,
            venue_id=venue_id,
            symbol=symbol,
            action=ENTER_NOW,
            conviction=conviction.calibrated,
            timing=None,
            exit_plan=exit_plan,
            features_summary={
                "source": candidate.source,
                "detector": candidate.detector,
                "move_so_far": candidate.move_so_far,
                "move_normal": candidate.move_normal,
                "fraction_of_a_normal_move_done": candidate.fraction_of_a_normal_move_done,
                "observations_in_move": candidate.observations_in_move,
                "evidence": dict(candidate.evidence),
            },
            refusal=None,
            reason=(
                f"follow {symbol} {candidate.direction}: {candidate.reason}. "
                f"{conviction.reason}. {exit_plan.reason}"
            ),
            formed_at_ns=self._now_ns(),
        )

    def _is_trail_only(self, exit_plan) -> bool:
        """The plan's only exit must be the trail itself, at the stop price."""
        return (
            len(exit_plan.targets) == 1
            and exit_plan.targets[0].price == exit_plan.stop_price
            and exit_plan.targets[0].fraction == 1.0
        )

    def _stand_down(
        self, venue_id: str, symbol: str, side: str, refusal: str, reason: str, conviction: Estimate
    ) -> DirectionalOpinion:
        self.standing.stood_down += 1
        self.standing.by_refusal[refusal] = self.standing.by_refusal.get(refusal, 0) + 1
        return stand_down(
            bot=BOT, side=side, venue_id=venue_id, symbol=symbol,
            refusal=refusal, reason=reason, conviction=conviction, now_ns=self._now_ns,
        )


def describe_opinions(composer: TailOpinionComposer) -> dict:
    return {
        "part_id": PART_ID,
        "opinions_composed": composer.standing.opinions_composed,
        "calls_to_act": composer.standing.calls_to_act,
        "stood_down": composer.standing.stood_down,
        "reversals_refused": composer.standing.reversals_refused,
        "plans_naming_a_target_refused": composer.standing.targets_refused,
        "stood_down_by_reason": dict(composer.standing.by_refusal),
        "strongest_conviction_acted_on": composer.standing.strongest_conviction_acted_on,
    }


def run_tail_opinion_composer(
    composer: TailOpinionComposer, control_socket, read_judgements, publish_opinions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_opinions(
            tuple(
                composer.compose(candidate, conviction, exit_plan)
                for candidate, conviction, exit_plan in read_judgements()
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
    """The one entry point every part carries (T-1)."""
    from runtime.edge_arithmetic import ConvictionFloor
    from runtime.input_assembly import Batch, LatestByKey

    convictions = Batch(read=context.bus.reader("tail-calibrated-conviction"))
    plans = LatestByKey(read=context.bus.reader("tail-exit-plan"), key_of=lambda p: (p.venue_id, p.symbol))
    candidates = LatestByKey(read=context.bus.reader("follow-candidate"), key_of=lambda c: (c.venue_id, c.symbol))
    publish_opinions = context.bus.publisher_for("directional-opinion")
    composer = TailOpinionComposer(
        conviction_floor=ConvictionFloor(
            fee_rate=context.number("taker_fee_rate"),
            margin=context.number("bull_conviction_margin_over_break_even"),
            fallback_reward_to_risk=context.number("bull_exit_minimum_reward_to_risk"),
        ),
        require_trained_model=bool(context.setting("bull_opinion_require_trained_model").value),
    )

    def read_judgements():
        plan_by_symbol = plans.mapping()
        candidate_by_symbol = candidates.mapping()
        jobs = []
        for conviction in convictions.payloads():
            key = (conviction.venue_id, conviction.symbol)
            candidate = candidate_by_symbol.get(key)
            if candidate is None:
                continue
            jobs.append((candidate, conviction, plan_by_symbol.get(key)))
        return tuple(jobs)

    def publish(items) -> None:
        if items:
            publish_opinions(items)

    return run_tail_opinion_composer(
        composer=composer,
        control_socket=context.control_socket,
        read_judgements=read_judgements,
        publish_opinions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
