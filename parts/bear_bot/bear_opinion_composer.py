"""bear-opinion-composer: the short bot's whole answer, and its extra requirement.

Assembles conviction, timing, exit plan and features into one opinion, or refuses
and names which of them failed -- the same job as its bull counterpart, with one
requirement the bull does not have.

**A short must have a bounded stop before it is an opinion.** The bull composer
requires a complete exit plan; this one additionally requires that the plan's
risk fraction is one the bot can carry, because a short's loss has no ceiling and
"we have a stop" is not the same claim when the stop is 60% away. The exit
proposer already refuses those, and this is the second gate on the same fact:
the requirement is stated where the opinion is formed, so that replacing the
proposer cannot quietly remove it.

**Every requirement is refused by name and every stand-down is published.** A
symbol nothing was said about looks identical to a symbol nobody looked at, and
for the short book that distinction is the difference between "we chose not to"
and "the scanner stopped feeding us".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CONVICTION_TOO_LOW, ENTER_NOW, FEATURES_INCOMPLETE, NO_EXIT_PLAN, SHORT,
    STAND_DOWN, TIMING_REFUSED, WAIT_FOR_TRIGGER, DirectionalOpinion, stand_down,
)
from runtime.edge_arithmetic import ConvictionFloor
from runtime.symbol_round_trip_cost import SymbolRoundTripCost
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-opinion-composer"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-opinion-composer",
    consumes=(
        "bear-calibrated-conviction", "bear-entry-timing",
        "bear-exit-plan", "bear-feature-vector",
        # What a round trip in the plan's own symbol costs, measured from its
        # own book -- runtime/symbol_round_trip_cost.py.
        "liquidity-grade",
    ),
    produces=("directional-opinion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RISK_UNBOUNDED = "stop-too-far-for-a-position-whose-loss-has-no-ceiling"


@dataclass
class ComposerStanding:
    opinions_composed: int = 0
    calls_to_act: int = 0
    stood_down: int = 0
    by_refusal: dict = field(default_factory=dict)
    strongest_conviction_acted_on: float = 0.0
    last_floor: float | None = None
    widest_risk_accepted: float = 0.0


class BearOpinionComposer:
    """Assembles one short answer, and will not call one whose risk it cannot bound."""

    def __init__(
        self,
        conviction_floor: ConvictionFloor,
        round_trip_cost: SymbolRoundTripCost,
        maximum_missing_features: int,
        maximum_risk_fraction: float,
        require_trained_model: bool,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_risk_fraction < 1.0:
            raise ValueError(
                "a short's risk ceiling is a fraction of entry price and must be inside (0, 1); "
                "without one this part restates the proposer instead of checking it"
            )
        if maximum_missing_features < 0:
            raise ValueError("a negative allowance for missing features means nothing")
        # The plan's own break-even plus the operator's margin, as for the bull
        # bot: a literal floor was the off switch (runtime/edge_arithmetic.py).
        self._floor = conviction_floor
        self._round_trip_cost = round_trip_cost
        self._maximum_missing = maximum_missing_features
        self._maximum_risk = maximum_risk_fraction
        self._require_trained_model = require_trained_model
        self._now_ns = now_ns
        self.standing = ComposerStanding()

    def observe_liquidity_grade(self, grade) -> None:
        """One symbol's measured book cost, held as the level it is."""
        self._round_trip_cost.observe_liquidity_grade(grade)

    def describe_round_trip_costs(self) -> dict:
        """What the floors were charged with, so a board can tell measured from fallback."""
        return self._round_trip_cost.describe()

    def compose(self, vector, conviction, timing, exit_plan) -> DirectionalOpinion:
        self.standing.opinions_composed += 1
        venue_id, symbol = conviction.venue_id, conviction.symbol

        if len(vector.missing) > self._maximum_missing:
            return self._stand_down(
                venue_id, symbol, FEATURES_INCOMPLETE,
                f"{len(vector.missing)} feature(s) could not be measured "
                f"({', '.join(sorted(vector.missing))}), past the {self._maximum_missing} this "
                f"bot will short on; the model would be weighing absences",
                conviction.calibrated,
            )

        if exit_plan is None or not exit_plan.is_complete:
            return self._stand_down(
                venue_id, symbol, NO_EXIT_PLAN,
                f"conviction {conviction.probability:.1%}, but no exit plan could be built, so "
                f"there is nowhere this short is wrong, nowhere it is finished, and no "
                f"break-even it has to clear",
                conviction.calibrated,
            )

        floor, floor_reason = self._floor.for_plan(
            exit_plan.reward_to_risk,
            exit_plan.risk_fraction,
            self._round_trip_cost.for_symbol(venue_id, symbol),
        )
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
                f"{conviction.model_observations} outcome(s), and this bot is configured to "
                f"short only on a model that has seen enough of both to be fitted",
                conviction.calibrated,
            )

        if timing is None or timing.action == STAND_DOWN:
            return self._stand_down(
                venue_id, symbol, TIMING_REFUSED,
                f"the setup is believed at {conviction.probability:.1%} but the moment is not: "
                + (timing.reason if timing is not None else "no timing was produced"),
                conviction.calibrated,
            )

        if exit_plan.risk_fraction > self._maximum_risk:
            # A short's loss has no ceiling, so a stop too far is not a stop. The
            # requirement lives where the opinion is formed, so replacing the
            # proposer cannot quietly remove it.
            return self._stand_down(
                venue_id, symbol, RISK_UNBOUNDED,
                f"the plan's stop is {exit_plan.risk_fraction:.1%} away, past the "
                f"{self._maximum_risk:.1%} this bot will carry on a position whose loss has no "
                f"ceiling; having a stop is not the same claim when the stop is that far",
                conviction.calibrated,
            )

        self.standing.calls_to_act += 1
        self.standing.strongest_conviction_acted_on = max(
            self.standing.strongest_conviction_acted_on, conviction.probability
        )
        self.standing.widest_risk_accepted = max(
            self.standing.widest_risk_accepted, exit_plan.risk_fraction
        )

        action = ENTER_NOW if timing.action == ENTER_NOW else WAIT_FOR_TRIGGER
        return DirectionalOpinion(
            bot=BOT,
            side=SHORT,
            venue_id=venue_id,
            symbol=symbol,
            action=action,
            conviction=conviction.calibrated,
            timing=timing,
            exit_plan=exit_plan,
            features_summary=self._summarise(vector),
            refusal=None,
            reason=f"short {symbol}: {conviction.reason}. {timing.reason}. {exit_plan.reason}",
            formed_at_ns=self._now_ns(),
        )

    def _summarise(self, vector) -> dict:
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
            bot=BOT, side=SHORT, venue_id=venue_id, symbol=symbol,
            refusal=refusal, reason=reason, conviction=conviction, now_ns=self._now_ns,
        )


def describe_opinions(composer: BearOpinionComposer) -> dict:
    return {
        "part_id": PART_ID,
        "opinions_composed": composer.standing.opinions_composed,
        "calls_to_act": composer.standing.calls_to_act,
        "stood_down": composer.standing.stood_down,
        "stood_down_by_reason": dict(composer.standing.by_refusal),
        "strongest_conviction_acted_on": composer.standing.strongest_conviction_acted_on,
        "widest_risk_accepted": composer.standing.widest_risk_accepted,
        "last_floor": composer.standing.last_floor,
        # Rule 8: a floor from a symbol's own measured book and one from the
        # fallback rate are different claims, and a board must not merge them.
        **composer.describe_round_trip_costs(),
    }


def run_bear_opinion_composer(
    composer: BearOpinionComposer, control_socket, read_judgements, publish_opinions,
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

    vectors = Batch(read=context.bus.reader("bear-feature-vector"))

    def by_symbol(data_type: str) -> LatestByKey:
        return LatestByKey(
            read=context.bus.reader(data_type),
            key_of=lambda payload: (payload.venue_id, payload.symbol),
        )

    convictions = by_symbol("bear-calibrated-conviction")
    timings = by_symbol("bear-entry-timing")
    exit_plans = by_symbol("bear-exit-plan")
    # Read as a Batch and held with the grade's own `graded_at_ns` rather than as
    # a LatestByKey: what makes a grade go away is its age.
    grades = Batch(read=context.bus.reader("liquidity-grade"))
    publish_opinions = context.bus.publisher_for("directional-opinion")

    composer = BearOpinionComposer(
        round_trip_cost=SymbolRoundTripCost(
            charge_stack_round_trip_fraction=context.number(
                "broker_charge_stack_round_trip_fraction"
            ),
            maximum_age_seconds=context.number("liquidity_grade_maximum_age_seconds"),
        ),
        conviction_floor=ConvictionFloor(
            # The fallback rate, for a symbol nothing recent has graded. Every
            # caller passed taker_fee_rate -- Bybit's perpetual rate -- until
            # 2026-09-07, eight times light; since 2026-09-08 a graded symbol is
            # charged its own measured round trip instead of any global rate.
            fee_rate=context.number("per_side_trading_cost_fraction"),
            margin=context.number("bear_conviction_margin_over_break_even"),
            fallback_reward_to_risk=context.number("bear_exit_minimum_reward_to_risk"),
        ),
        maximum_missing_features=int(context.number("bear_opinion_maximum_missing_features")),
        maximum_risk_fraction=context.number("risk_maximum_stop_fraction"),
        require_trained_model=bool(
            context.setting("bear_opinion_require_trained_model").value
        ),
    )

    def read_judgements():
        for grade in grades.payloads():
            composer.observe_liquidity_grade(grade)
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

    return run_bear_opinion_composer(
        composer=composer,
        control_socket=context.control_socket,
        read_judgements=read_judgements,
        publish_opinions=publish_opinions,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
