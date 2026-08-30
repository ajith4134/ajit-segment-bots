"""opinion-arbiter: three opinions in, one intent out. The decision the system makes.

Everything upstream measures and judges; this is where the system commits. Three
bots have said what they would do, and exactly one thing can happen, so the
arbiter's job is to turn disagreement, weights and evidence into a single intent
that can be defended afterwards.

**It weighs; it does not average.** Averaging two opposite opinions produces a
third that nobody holds, sized as though it were agreed. So opinions on the same
side are combined and opposite sides are a conflict, ruled on by a part whose
whole job that is -- and the ruling arrives here as evidence rather than being
re-derived.

What raises and lowers conviction, and why each is separate:

- **Agreement.** Two bots reaching the same conclusion from different features is
  stronger evidence than one bot being sure, and one bot alone is weaker than
  either. That is a property of the ensemble, not of any opinion in it.
- **Bot weight in this regime.** A bot that is right in a trend and wrong in a
  chop counts differently in each.
- **The forecast bias**, which can only *shade* the decision. A forecast that
  could overturn the bots would make the bots decoration.
- **The counter-argument**, which can veto. If the case against would reverse the
  decision, the decision does not get made -- because the argument was
  constructed to be the strongest one available, and dismissing it here would
  make writing it pointless.
- **The strategy review**, which can only discount. An LLM's read on whether a
  bot's own record shows it working is not evidence about this specific setup,
  so it can make the arbiter trust that bot less across every symbol, never
  more -- the same asymmetry as the counter-argument's veto, sized smaller.

**Coverage and competence are refusals, not adjustments.** A symbol outside the
system's measured competence is not a low-conviction trade, it is a trade about
which the system has no basis for a conviction at all.

**Standing aside is published.** A symbol the brain declined must be
distinguishable from one it never saw (Rule 8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import CLOSE_POSITION, LONG, REDUCE_POSITION, SHORT
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import (
    ADD_TO, CLOSE, MAJORITY, NO_OPINION, OPEN, REDUCE, RULED, SOLE_OPINION, UNANIMOUS,
    UNDERPERFORMING, TradeIntent, no_intent,
)

PART_ID = "opinion-arbiter"

PART_DECLARATION = PartDeclaration(
    part_id="opinion-arbiter",
    consumes=(
        "directional-opinion", "market-regime", "bot-maturity", "regime-break-alert",
        "forecast-bias", "competence-map", "coverage-report", "conflict-ruling",
        "bot-weight", "counter-argument", "regime-memory", "strategy-review",
    ),
    produces=("trade-intent", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NOBODY_WANTS_TO_ACT = "no-bot-wants-to-act"
OUTSIDE_COMPETENCE = "this-symbol-is-outside-the-system's-measured-competence"
COVERAGE_TOO_THIN = "too-little-of-this-symbol-has-been-observed-to-judge-it"
CONFLICT_UNRESOLVED = "the-bots-disagreed-and-the-conflict-was-not-resolvable"
REGIME_BROKEN = "the-regime-these-opinions-were-formed-in-has-broken"
COUNTER_ARGUMENT_STANDS = "the-case-against-would-reverse-this-decision"
CONVICTION_TOO_LOW = "weighted-conviction-below-the-floor"


@dataclass
class ArbiterStanding:
    symbols_arbitrated: int = 0
    intents_formed: int = 0
    stood_aside: int = 0
    vetoed_by_counter_argument: int = 0
    by_refusal: dict = field(default_factory=dict)
    by_agreement: dict = field(default_factory=dict)
    strongest_conviction: float = 0.0


class OpinionArbiter:
    """Turns three bots' opinions into the one intent that leaves the segment bot."""

    def __init__(
        self,
        conviction_floor: ConvictionFloor,
        agreement_bonus: float,
        sole_opinion_penalty: float,
        maximum_forecast_shade: float,
        minimum_competence: float,
        minimum_coverage: float,
        strategy_review_distrust_discount: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= maximum_forecast_shade < 0.5:
            raise ValueError(
                "the forecast may shade the decision, never make it; a shade of half the "
                "probability range would let it overturn the bots"
            )
        if not 0.0 <= sole_opinion_penalty < 1.0:
            raise ValueError("the penalty scales conviction and must be inside [0, 1)")
        if not 0.0 <= strategy_review_distrust_discount < 1.0:
            raise ValueError("the discount scales a bot's weight and must be inside [0, 1)")
        # The floor is each acting opinion's own break-even, and the brain clears
        # the highest of them: an intent acts on every plan behind it, so it must
        # be worth taking against the most demanding one (runtime/edge_arithmetic.py).
        self._floor = conviction_floor
        self._agreement_bonus = agreement_bonus
        self._sole_penalty = sole_opinion_penalty
        self._maximum_shade = maximum_forecast_shade
        self._minimum_competence = minimum_competence
        self._minimum_coverage = minimum_coverage
        self._strategy_review_discount = strategy_review_distrust_discount
        self._now_ns = now_ns
        self._weights: dict[tuple[str, str], float] = {}
        self._competence: dict[tuple[str, str], float] = {}
        self._coverage: dict[tuple[str, str], float] = {}
        self._forecast_bias: dict[tuple[str, str], float] = {}
        self._broken_regimes: set[str] = set()
        self._strategy_reviews: dict[str, object] = {}
        self.standing = ArbiterStanding()

    def observe_bot_weight(self, weight) -> None:
        self._weights[(weight.bot, weight.regime)] = weight.weight

    def observe_competence(self, venue_id: str, symbol: str, competence: float) -> None:
        """How well the system has been shown to understand this symbol."""
        self._competence[(venue_id, symbol)] = competence

    def observe_coverage(self, venue_id: str, symbol: str, coverage: float) -> None:
        """How much of this symbol has actually been observed, as a fraction."""
        self._coverage[(venue_id, symbol)] = coverage

    def observe_forecast_bias(self, bias) -> None:
        self._forecast_bias[(bias.venue_id, bias.symbol)] = bias.bias if bias.is_trusted else 0.0

    def observe_strategy_review(self, review) -> None:
        self._strategy_reviews[review.bot] = review

    def trust_multiplier(self, bot: str) -> float:
        """How far a bot's weight is discounted by its own strategy review.

        Discount only, and only once the review is fitted: an unfitted or
        working review changes nothing, because this is a read on the bot's
        general standing, not evidence about the setup in front of it.
        """
        review = self._strategy_reviews.get(bot)
        if review is None or not review.confidence.is_fitted:
            return 1.0
        if review.assessment != UNDERPERFORMING:
            return 1.0
        return 1.0 - self._strategy_review_discount

    def observe_regime_break(self, regime: str, has_broken: bool) -> None:
        if has_broken:
            self._broken_regimes.add(regime)
        else:
            self._broken_regimes.discard(regime)

    def weight_of(self, bot: str, regime: str) -> float:
        """A bot's weight in this regime, or one when nothing has been learned yet.

        One rather than zero: an unweighted bot is unproven, not disproven, and
        starting it at zero would keep it from ever producing the record that
        would weight it.
        """
        return self._weights.get((bot, regime), 1.0)

    def arbitrate(self, opinions, regime, ruling=None, counter_argument=None) -> TradeIntent:
        """One symbol's opinions, weighed into one intent."""
        self.standing.symbols_arbitrated += 1
        if not opinions:
            return self._stand_aside("", "", NO_OPINION, NOBODY_WANTS_TO_ACT)

        venue_id, symbol = opinions[0].venue_id, opinions[0].symbol
        acting = [opinion for opinion in opinions if opinion.is_a_call_to_act]

        if not acting:
            return self._stand_aside(
                venue_id, symbol, NO_OPINION,
                f"all {len(opinions)} bot(s) stood down: "
                + "; ".join(f"{o.bot} -- {o.refusal}" for o in opinions),
            )

        competence = self._competence.get((venue_id, symbol))
        if competence is not None and competence < self._minimum_competence:
            # A refusal, not a discount. Outside its competence the system has no
            # basis for a conviction at all, and a small position taken on one is
            # the same mistake in smaller size.
            return self._stand_aside(
                venue_id, symbol, NO_OPINION,
                f"measured competence in {symbol} is {competence:.0%}, below the "
                f"{self._minimum_competence:.0%} this brain acts within",
                OUTSIDE_COMPETENCE,
            )

        coverage = self._coverage.get((venue_id, symbol))
        if coverage is not None and coverage < self._minimum_coverage:
            return self._stand_aside(
                venue_id, symbol, NO_OPINION,
                f"only {coverage:.0%} of {symbol} has been observed, below the "
                f"{self._minimum_coverage:.0%} needed to judge it",
                COVERAGE_TOO_THIN,
            )

        if regime.is_classified and regime.regime in self._broken_regimes:
            return self._stand_aside(
                venue_id, symbol, NO_OPINION,
                f"every one of these opinions was formed in the {regime.regime} regime and "
                f"that regime has broken; the models behind them were fitted on a market that "
                f"is no longer this one",
                REGIME_BROKEN,
            )

        sides = {opinion.side for opinion in acting}
        if len(sides) > 1:
            acting, agreement = self._apply_ruling(acting, ruling)
            if not acting:
                return self._stand_aside(
                    venue_id, symbol, RULED,
                    f"the bots disagreed and the ruling was to stand aside: "
                    + (ruling.grounds if ruling is not None else "no ruling was produced"),
                    CONFLICT_UNRESOLVED,
                )
        elif len(acting) == 1:
            agreement = SOLE_OPINION
        elif len(acting) == len(opinions):
            agreement = UNANIMOUS
        else:
            agreement = MAJORITY

        side = acting[0].side
        weights = {
            opinion.bot: self.weight_of(opinion.bot, regime.regime) * self.trust_multiplier(opinion.bot)
            for opinion in acting
        }
        conviction = self._weighted_conviction(acting, weights, agreement)
        shade = self._forecast_shade(venue_id, symbol, side)
        conviction = min(0.999, max(0.001, conviction + shade))

        if counter_argument is not None and counter_argument.would_reverse_the_decision:
            # A veto rather than a discount: the argument was constructed to be
            # the strongest available, and dismissing it here would make writing
            # it pointless.
            self.standing.vetoed_by_counter_argument += 1
            return self._stand_aside(
                venue_id, symbol, agreement,
                f"the case against would reverse this decision: "
                f"{counter_argument.strongest_objection}",
                COUNTER_ARGUMENT_STANDS,
            )

        floor, floor_reason = self._floor_for(acting)
        if conviction < floor:
            return self._stand_aside(
                venue_id, symbol, agreement,
                f"weighted conviction is {conviction:.1%} across "
                f"{len(acting)} bot(s) ({agreement}), below the "
                f"{floor:.1%} this brain acts on ({floor_reason})",
                CONVICTION_TOO_LOW,
            )

        action = self._action_for(acting)
        dissenting = tuple(
            sorted(
                opinion.bot
                for opinion in opinions
                if opinion.is_a_call_to_act and opinion not in acting
            )
        )

        self.standing.intents_formed += 1
        self.standing.by_agreement[agreement] = self.standing.by_agreement.get(agreement, 0) + 1
        self.standing.strongest_conviction = max(self.standing.strongest_conviction, conviction)

        best = max(acting, key=lambda opinion: opinion.conviction.value)
        return TradeIntent(
            venue_id=venue_id,
            symbol=symbol,
            side=side,
            action=action,
            conviction=Estimate(
                value=conviction,
                is_fitted=all(opinion.conviction.is_fitted for opinion in acting),
                observations=sum(opinion.conviction.observations for opinion in acting),
                prior=floor,
                was_clamped=False,
                bound_low=None,
                bound_high=None,
                reason=f"{len(acting)} bot(s), {agreement}",
            ),
            horizon_seconds=(
                best.exit_plan.horizon_seconds if best.exit_plan is not None else 0.0
            ),
            stop_price=best.exit_plan.stop_price if best.exit_plan is not None else None,
            agreement=agreement,
            contributing_bots=tuple(sorted(opinion.bot for opinion in acting)),
            dissenting_bots=dissenting,
            opinion_weights=weights,
            evidence={
                "per_bot": {
                    opinion.bot: {
                        "conviction": opinion.conviction.value,
                        "is_measured": opinion.conviction.is_fitted,
                        "weight": weights[opinion.bot],
                        "strategy_trust": self.trust_multiplier(opinion.bot),
                        "reason": opinion.reason,
                    }
                    for opinion in acting
                },
                "regime": regime.regime,
                "forecast_shade": shade,
                "ruling": None if ruling is None else ruling.ruling,
            },
            reason=(
                f"{side} {symbol}: {len(acting)} bot(s) agree ({agreement}) at a weighted "
                f"{conviction:.1%} in {regime.regime}"
                + (f", shaded {shade:+.1%} by the forecast" if shade else "")
                + (f"; {', '.join(dissenting)} dissent" if dissenting else "")
                + f". Strongest case: {best.reason}"
            ),
            formed_at_ns=self._now_ns(),
        )

    def _apply_ruling(self, acting, ruling) -> tuple[list, str]:
        """Keep only the bots the ruling favoured; the ruling is not re-derived here."""
        if ruling is None or ruling.favoured_bot is None:
            return [], RULED
        kept = [opinion for opinion in acting if opinion.bot == ruling.favoured_bot]
        return kept, RULED

    def _floor_for(self, acting) -> tuple[float, str]:
        """The highest break-even among the acting opinions' plans, with its reason."""
        floors = []
        for opinion in acting:
            plan = opinion.exit_plan
            if plan is None:
                floors.append(self._floor.before_any_plan())
            else:
                floors.append(self._floor.for_plan(plan.reward_to_risk, plan.risk_fraction))
        return max(floors, key=lambda pair: pair[0])

    def _weighted_conviction(self, acting, weights: dict, agreement: str) -> float:
        """Weighted mean conviction, adjusted for what the ensemble itself says.

        Agreement is a bonus and a lone opinion is a penalty because they are
        properties of the ensemble rather than of any opinion in it: two bots
        reaching the same conclusion from different features is stronger
        evidence than one bot being sure.
        """
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return 0.0
        weighted = (
            sum(opinion.conviction.value * weights[opinion.bot] for opinion in acting)
            / total_weight
        )
        if agreement == UNANIMOUS:
            weighted += self._agreement_bonus
        elif agreement == SOLE_OPINION:
            weighted *= 1.0 - self._sole_penalty
        return weighted

    def _forecast_shade(self, venue_id: str, symbol: str, side: str) -> float:
        """How far the forecast may move the conviction, bounded so it cannot decide."""
        bias = self._forecast_bias.get((venue_id, symbol), 0.0)
        signed = bias if side == LONG else -bias
        return max(-self._maximum_shade, min(self._maximum_shade, signed))

    def _action_for(self, acting) -> str:
        """What the intent asks for, from what the bots were actually saying."""
        actions = {opinion.action for opinion in acting}
        if CLOSE_POSITION in actions:
            return CLOSE
        if REDUCE_POSITION in actions:
            return REDUCE
        if any(opinion.is_about_an_open_position for opinion in acting):
            return ADD_TO
        return OPEN

    def _stand_aside(self, venue_id, symbol, agreement, reason, refusal=None) -> TradeIntent:
        self.standing.stood_aside += 1
        if refusal is not None:
            self.standing.by_refusal[refusal] = self.standing.by_refusal.get(refusal, 0) + 1
        return no_intent(
            venue_id=venue_id, symbol=symbol, agreement=agreement,
            reason=reason, now_ns=self._now_ns,
        )


def describe_arbitration(arbiter: OpinionArbiter) -> dict:
    return {
        "part_id": PART_ID,
        "symbols_arbitrated": arbiter.standing.symbols_arbitrated,
        "intents_formed": arbiter.standing.intents_formed,
        "stood_aside": arbiter.standing.stood_aside,
        "vetoed_by_the_counter_argument": arbiter.standing.vetoed_by_counter_argument,
        "stood_aside_by_reason": dict(sorted(arbiter.standing.by_refusal.items())),
        "intents_by_agreement": dict(sorted(arbiter.standing.by_agreement.items())),
        "strongest_conviction": arbiter.standing.strongest_conviction,
        "bot_weights_held": len(arbiter._weights),
        "strategy_reviews_held": len(arbiter._strategy_reviews),
    }


def run_opinion_arbiter(
    arbiter: OpinionArbiter, control_socket, read_opinions_and_context, publish_intents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_intents(
            tuple(
                arbiter.arbitrate(opinions, regime, ruling, counter)
                for opinions, regime, ruling, counter in read_opinions_and_context(arbiter)
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
        read_standing=lambda: describe_arbitration(arbiter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The arbiter judges a symbol at a time: every opinion currently held about it,
    against the regime it is in, any ruling that has been made about the conflict,
    and any counter-argument raised. Opinions are levels per (bot, symbol) -- a bot
    holds its view until it changes it -- and the arbitration is driven by whichever
    symbols had an opinion arrive this tick.

    In the first runs only the bull bot is on, so every opinion is sole and every
    one is discounted by the sole-opinion penalty. That is correct: an unopposed
    view is weaker evidence than an agreed one, and with the floor at 0.55 and the
    penalty at 0.05 a lone conviction must reach 0.60 to act.
    """
    from runtime.input_assembly import Batch, LatestByKey

    opinions = Batch(read=context.bus.reader("directional-opinion"))
    regimes = LatestByKey(
        read=context.bus.reader("market-regime"),
        key_of=lambda regime: (regime.venue_id, regime.symbol),
    )
    rulings = LatestByKey(
        read=context.bus.reader("conflict-ruling"),
        key_of=lambda ruling: (ruling.venue_id, ruling.symbol),
    )
    counters = LatestByKey(
        read=context.bus.reader("counter-argument"),
        key_of=lambda counter: (counter.venue_id, counter.symbol),
    )
    weights = Batch(read=context.bus.reader("bot-weight"))
    reviews = Batch(read=context.bus.reader("strategy-review"))
    biases = Batch(read=context.bus.reader("forecast-bias"))
    breaks = Batch(read=context.bus.reader("regime-break-alert"))
    competences = Batch(read=context.bus.reader("competence-map"))
    coverages = Batch(read=context.bus.reader("coverage-report"))
    publish_intents = context.bus.publisher_for("trade-intent")

    # Every opinion currently held, by symbol and then by bot. Held across ticks
    # because an opinion is a level: the bull bot does not restate its view every
    # tick, and an arbiter that only saw this tick's arrivals would arbitrate one
    # bot against silence rather than against the others' current positions.
    held: dict[tuple[str, str], dict[str, object]] = {}

    def read_opinions_and_context(arbiter):
        for weight in weights.payloads():
            arbiter.observe_bot_weight(weight)
        for review in reviews.payloads():
            arbiter.observe_strategy_review(review)
        for bias in biases.payloads():
            arbiter.observe_forecast_bias(bias)
        for alert in breaks.payloads():
            arbiter.observe_regime_break(alert.regime, alert.has_broken)
        # A competence-map is the whole map, not one entry, and each entry carries
        # its own measured number. Both of these read the payload as though it
        # were the number itself until 2026-08-25: the map crashed this part on
        # the first one that arrived, and the coverage report -- an object, always
        # truthy, never a fraction -- would have compared as one against the
        # minimum and refused every symbol the auditor had ever reported on.
        for mapped in competences.payloads():
            for entry in mapped.entries:
                if entry.competence is not None:
                    arbiter.observe_competence(entry.venue_id, entry.symbol, entry.competence)
        for report in coverages.payloads():
            if report.coverage is not None:
                arbiter.observe_coverage(report.venue_id, report.symbol, report.coverage)

        regime_by_symbol = regimes.mapping()
        ruling_by_symbol = rulings.mapping()
        counter_by_symbol = counters.mapping()

        arriving = set()
        for opinion in opinions.payloads():
            key = (opinion.venue_id, opinion.symbol)
            held.setdefault(key, {})[opinion.bot] = opinion
            arriving.add(key)

        return tuple(
            (
                tuple(held[key].values()),
                regime_by_symbol.get(key),
                ruling_by_symbol.get(key),
                counter_by_symbol.get(key),
            )
            for key in sorted(arriving)
        )

    return run_opinion_arbiter(
        arbiter=OpinionArbiter(
            conviction_floor=ConvictionFloor(
                fee_rate=context.number("taker_fee_rate"),
                margin=context.number("arbiter_conviction_margin_over_break_even"),
                fallback_reward_to_risk=context.number("bull_exit_minimum_reward_to_risk"),
            ),
            agreement_bonus=context.number("arbiter_agreement_bonus"),
            sole_opinion_penalty=context.number("arbiter_sole_opinion_penalty"),
            maximum_forecast_shade=context.number("arbiter_maximum_forecast_shade"),
            minimum_competence=context.number("arbiter_minimum_competence"),
            minimum_coverage=context.number("arbiter_minimum_coverage"),
            strategy_review_distrust_discount=context.number(
                "arbiter_strategy_review_distrust_discount"
            ),
        ),
        control_socket=context.control_socket,
        read_opinions_and_context=read_opinions_and_context,
        publish_intents=publish_intents,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
