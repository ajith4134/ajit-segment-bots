"""bull-opinion-composer: the bot's whole answer, including the answer "nothing".

The four judgements above -- conviction, timing, exit plan, features -- arrive
separately and mean nothing separately. A high conviction with no exit plan is
not a trade; a perfect entry moment on a setup the bot does not believe in is
not a trade either. This is where they either become one opinion or become a
refusal with a reason.

**Every requirement is refused by name.** `bull-opinion-composer` never returns
an opinion with a hole in it and never fills a hole with a default. The refusals
are the point of the part: "conviction 0.71 but no exit plan could be built for
ADAUSDT" is something a person can act on, and a silently dropped candidate is
not.

**A stand-down is published, not discarded.** A symbol nothing was said about
looks identical to a symbol nobody looked at, and Rule 8 applies to what a bot
reports as much as to a dashboard: absence must render as its own state.

The opinion carries a **summary of the features that produced it**, so a trade
can be argued with months later without the model that formed it still existing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CONVICTION_TOO_LOW, ENTER_NOW, FEATURES_INCOMPLETE, LONG, NO_EXIT_PLAN,
    STAND_DOWN, TIMING_REFUSED, WAIT_FOR_TRIGGER, DirectionalOpinion, stand_down,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-opinion-composer"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-opinion-composer",
    consumes=(
        "bull-calibrated-conviction", "bull-entry-timing",
        "bull-exit-plan", "bull-feature-vector",
    ),
    produces=("directional-opinion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass
class ComposerStanding:
    opinions_composed: int = 0
    calls_to_act: int = 0
    stood_down: int = 0
    by_refusal: dict = field(default_factory=dict)
    strongest_conviction_acted_on: float = 0.0
    # The floor the last judged plan had to clear, so the board can show what the
    # bot is measuring its conviction against rather than a number it once read.
    last_floor: float | None = None


class BullOpinionComposer:
    """Assembles one answer from four judgements, or refuses and says which failed."""

    def __init__(
        self,
        conviction_floor: ConvictionFloor,
        maximum_missing_features: int,
        require_trained_model: bool,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_missing_features < 0:
            raise ValueError("a negative allowance for missing features means nothing")
        # The floor is the plan's own break-even plus the operator's margin, not a
        # literal: 0.55 refused 325 of 327 intents on 2026-08-23 against a model
        # whose measured output never left 0.44-0.50 (runtime/edge_arithmetic.py).
        self._floor = conviction_floor
        self._maximum_missing = maximum_missing_features
        self._require_trained_model = require_trained_model
        self._now_ns = now_ns
        self.standing = ComposerStanding()

    def compose(self, vector, conviction, timing, exit_plan) -> DirectionalOpinion:
        self.standing.opinions_composed += 1
        venue_id, symbol = conviction.venue_id, conviction.symbol

        if len(vector.missing) > self._maximum_missing:
            return self._stand_down(
                venue_id, symbol, FEATURES_INCOMPLETE,
                f"{len(vector.missing)} feature(s) could not be measured "
                f"({', '.join(sorted(vector.missing))}), past the {self._maximum_missing} this "
                f"bot will form an opinion with; the model would be weighing absences",
                conviction.calibrated,
            )

        # The floor needs the plan, so a missing plan is refused here rather than
        # after the conviction check: an entry with no exit is not a trade, it is
        # an exposure, and there is no break-even for an exposure.
        if exit_plan is None or not exit_plan.is_complete:
            return self._stand_down(
                venue_id, symbol, NO_EXIT_PLAN,
                f"conviction {conviction.probability:.1%}, but no exit plan could be built, so "
                f"there is nowhere this trade is wrong, nowhere it is finished, and no "
                f"break-even it has to clear",
                conviction.calibrated,
            )

        floor, floor_reason = self._floor.for_plan(exit_plan.reward_to_risk, exit_plan.risk_fraction)
        self.standing.last_floor = floor
        if conviction.probability < floor:
            return self._stand_down(
                venue_id, symbol, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%} against a floor of "
                f"{floor:.1%} ({floor_reason})",
                conviction.calibrated,
            )

        if self._require_trained_model and not conviction.model_is_trained:
            return self._stand_down(
                venue_id, symbol, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%} from a model that has trained on "
                f"{conviction.model_observations} outcome(s), and this bot is configured to act "
                f"only on a model that has seen enough of both to be fitted",
                conviction.calibrated,
            )

        if timing is None or timing.action == STAND_DOWN:
            return self._stand_down(
                venue_id, symbol, TIMING_REFUSED,
                f"the setup is believed at {conviction.probability:.1%} but the moment is not: "
                + (timing.reason if timing is not None else "no timing was produced"),
                conviction.calibrated,
            )

        self.standing.calls_to_act += 1
        self.standing.strongest_conviction_acted_on = max(
            self.standing.strongest_conviction_acted_on, conviction.probability
        )

        action = ENTER_NOW if timing.action == ENTER_NOW else WAIT_FOR_TRIGGER
        return DirectionalOpinion(
            bot=BOT,
            side=LONG,
            venue_id=venue_id,
            symbol=symbol,
            action=action,
            conviction=conviction.calibrated,
            timing=timing,
            exit_plan=exit_plan,
            features_summary=self._summarise(vector),
            refusal=None,
            reason=(
                f"long {symbol}: {conviction.reason}. {timing.reason}. {exit_plan.reason}"
            ),
            formed_at_ns=self._now_ns(),
        )

    def _summarise(self, vector) -> dict:
        """Enough of the vector to argue with the trade after the model has changed."""
        return {
            "features": dict(sorted(vector.features.items())),
            "missing": list(vector.missing),
            "sources": dict(sorted(vector.sources.items())),
            "built_at_ns": vector.built_at_ns,
        }

    def _stand_down(
        self, venue_id: str, symbol: str, refusal: str, reason: str, conviction: Estimate
    ) -> DirectionalOpinion:
        self.standing.stood_down += 1
        self.standing.by_refusal[refusal] = self.standing.by_refusal.get(refusal, 0) + 1
        return stand_down(
            bot=BOT, side=LONG, venue_id=venue_id, symbol=symbol,
            refusal=refusal, reason=reason, conviction=conviction, now_ns=self._now_ns,
        )


def describe_opinions(composer: BullOpinionComposer) -> dict:
    return {
        "part_id": PART_ID,
        "opinions_composed": composer.standing.opinions_composed,
        "calls_to_act": composer.standing.calls_to_act,
        "stood_down": composer.standing.stood_down,
        "stood_down_by_reason": dict(composer.standing.by_refusal),
        "strongest_conviction_acted_on": composer.standing.strongest_conviction_acted_on,
        "last_floor": composer.standing.last_floor,
    }


def run_bull_opinion_composer(
    composer: BullOpinionComposer, control_socket, read_judgements, publish_opinions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_opinions(
            tuple(
                composer.compose(vector, conviction, timing, exit_plan)
                for vector, conviction, timing, exit_plan in read_judgements()
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
        read_standing=lambda: describe_opinions(composer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Four judgements have to be about the same symbol before an opinion exists: the
    vector it was judged on, the conviction, the entry timing and the exit plan.
    Three of them are levels kept per symbol and the fourth -- the vector -- is the
    event that asks for the opinion, because a vector is what every one of the other
    three was derived from.

    A symbol missing any of the four gets no opinion at all. Composing one from
    three would be an opinion with a hole where a decision should be, and the
    arbiter downstream cannot see which part is missing.
    """
    from runtime.input_assembly import Batch, LatestByKey

    vectors = Batch(read=context.bus.reader("bull-feature-vector"))

    def by_symbol(data_type: str) -> LatestByKey:
        return LatestByKey(
            read=context.bus.reader(data_type),
            key_of=lambda payload: (payload.venue_id, payload.symbol),
        )

    convictions = by_symbol("bull-calibrated-conviction")
    timings = by_symbol("bull-entry-timing")
    exit_plans = by_symbol("bull-exit-plan")
    publish_opinions = context.bus.publisher_for("directional-opinion")

    def read_judgements():
        belief = convictions.mapping()
        timing_by_symbol = timings.mapping()
        plan_by_symbol = exit_plans.mapping()
        judgements = []
        for vector in vectors.payloads():
            key = (vector.venue_id, vector.symbol)
            conviction = belief.get(key)
            timing = timing_by_symbol.get(key)
            exit_plan = plan_by_symbol.get(key)
            if conviction is None or timing is None or exit_plan is None:
                continue
            judgements.append((vector, conviction, timing, exit_plan))
        return tuple(judgements)

    return run_bull_opinion_composer(
        composer=BullOpinionComposer(
            conviction_floor=ConvictionFloor(
                fee_rate=context.number("taker_fee_rate"),
                margin=context.number("bull_conviction_margin_over_break_even"),
                fallback_reward_to_risk=context.number("bull_exit_minimum_reward_to_risk"),
            ),
            maximum_missing_features=int(context.number("bull_opinion_maximum_missing_features")),
            require_trained_model=bool(
                context.setting("bull_opinion_require_trained_model").value
            ),
        ),
        control_socket=context.control_socket,
        read_judgements=read_judgements,
        publish_opinions=publish_opinions,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
