"""exploration-pair-opener: when the honest answer is "run the experiment".

RL-047's mechanism. When two bots disagree and nothing separates them, the
arbiter stands aside -- correctly, because taking the louder one is a coin flip
with costs. But standing aside forever means the disagreement is never resolved:
neither bot produces evidence, so neither can be weighted, so the next identical
conflict is stood aside too.

This part breaks that loop by **taking both sides, small**, and letting the
market decide. It is an experiment, not a hedge, and the difference is the whole
design:

- **A hedge is meant to cancel.** A pair is meant to resolve: one leg wins, that
  leg is the evidence, and the tailgater may then add to it.
- **A hedge is sized for protection.** A pair is sized for the information it
  buys, which is much smaller -- the cost of the experiment is the spread and the
  carry on both legs, and it must be worth what it teaches.
- **A hedge has no verdict.** A pair does, and producing it is the point.

**It only opens a pair when the disagreement is genuine and unresolvable.** Two
bots pointing opposite ways where one is clearly more mature, or where the regime
belongs to one of them, is not an experiment -- it is a question already answered,
and running it anyway spends money to learn what was known.

**It never opens a pair in a symbol that already holds one**, and never more than
its own ceiling at once: an experiment budget spent on twenty simultaneous pairs
teaches twenty things badly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, SHORT
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import OPEN, RULED, TradeIntent

PART_ID = "exploration-pair-opener"

PART_DECLARATION = PartDeclaration(
    part_id="exploration-pair-opener",
    consumes=("directional-opinion", "bot-maturity", "market-regime"),
    produces=("trade-intent", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OPENED = "opened"
NOT_A_DISAGREEMENT = "the-bots-do-not-actually-disagree"
QUESTION_ALREADY_ANSWERED = "one-bot-is-clearly-more-proven-here"
ALREADY_RUNNING_HERE = "this-symbol-already-has-a-pair-open"
BUDGET_SPENT = "as-many-pairs-are-open-as-this-brain-runs-at-once"
NOT_WORTH_THE_COST = "the-experiment-costs-more-than-it-would-teach"


@dataclass(frozen=True)
class OpenPair:
    """One running experiment: two intents whose verdict is the reason they exist."""

    venue_id: str
    symbol: str
    long_bot: str
    short_bot: str
    regime: str
    intents: tuple
    cost_fraction: float
    opened_at_ns: int


@dataclass
class OpenerStanding:
    disagreements_seen: int = 0
    pairs_opened: int = 0
    pairs_closed: int = 0
    by_refusal: dict = field(default_factory=dict)
    by_regime: dict = field(default_factory=dict)
    most_expensive_experiment: float = 0.0


class ExplorationPairOpener:
    """Opens a small two-sided experiment when a disagreement cannot be resolved."""

    def __init__(
        self,
        maximum_open_pairs: int,
        maturity_gap_trades: int,
        maximum_cost_fraction: float,
        minimum_information_value: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_open_pairs < 1:
            raise ValueError(
                "a brain that may run no experiments can never resolve a disagreement"
            )
        if not 0.0 < maximum_cost_fraction < 1.0:
            raise ValueError(
                "the cost of an experiment is a fraction of notional and must be inside (0, 1)"
            )
        self._maximum_pairs = maximum_open_pairs
        self._maturity_gap = maturity_gap_trades
        self._maximum_cost = maximum_cost_fraction
        self._minimum_information = minimum_information_value
        self._now_ns = now_ns
        self._open: dict[tuple[str, str], OpenPair] = {}
        self._maturity: dict[tuple[str, str], int] = {}
        self._costs: dict[tuple[str, str], float] = {}
        self.standing = OpenerStanding()

    def observe_bot_maturity(self, bot: str, regime: str, trades_here: int) -> None:
        self._maturity[(bot, regime)] = trades_here

    def observe_round_trip_cost(self, venue_id: str, symbol: str, cost_fraction: float) -> None:
        self._costs[(venue_id, symbol)] = cost_fraction

    def information_value(self, long_bot: str, short_bot: str, regime: str) -> float:
        """How much this experiment would teach, as the evidence it is missing.

        Largest when both bots are unproven here and smallest when both have long
        records -- because a pair between two well-measured bots re-measures what
        is already known.
        """
        trades = [
            self._maturity.get((long_bot, regime), 0),
            self._maturity.get((short_bot, regime), 0),
        ]
        return 1.0 / (1.0 + min(trades))

    @property
    def open_pairs(self) -> tuple[OpenPair, ...]:
        return tuple(self._open.values())

    def close_pair(self, venue_id: str, symbol: str) -> OpenPair | None:
        """The experiment resolved. Its verdict belongs to whoever recorded it."""
        pair = self._open.pop((venue_id, symbol), None)
        if pair is not None:
            self.standing.pairs_closed += 1
        return pair

    def consider(self, opinions, regime) -> tuple[tuple, str]:
        """Two opposed opinions, judged as an experiment worth running or not."""
        acting = [opinion for opinion in opinions if opinion.is_a_call_to_act]
        longs = [opinion for opinion in acting if opinion.side == LONG]
        shorts = [opinion for opinion in acting if opinion.side == SHORT]

        if not longs or not shorts:
            return (), self._refuse(NOT_A_DISAGREEMENT)

        self.standing.disagreements_seen += 1
        venue_id, symbol = acting[0].venue_id, acting[0].symbol

        if (venue_id, symbol) in self._open:
            return (), self._refuse(ALREADY_RUNNING_HERE)

        if len(self._open) >= self._maximum_pairs:
            # An experiment budget spent on twenty simultaneous pairs teaches
            # twenty things badly.
            return (), self._refuse(BUDGET_SPENT)

        long_opinion = max(longs, key=lambda opinion: opinion.conviction.value)
        short_opinion = max(shorts, key=lambda opinion: opinion.conviction.value)

        if self._question_is_already_answered(long_opinion.bot, short_opinion.bot, regime.regime):
            return (), self._refuse(QUESTION_ALREADY_ANSWERED)

        cost = self._costs.get((venue_id, symbol))
        if cost is not None and cost * 2 > self._maximum_cost:
            # Both legs pay the spread, so the experiment costs twice a trade.
            return (), self._refuse(NOT_WORTH_THE_COST)

        value = self.information_value(long_opinion.bot, short_opinion.bot, regime.regime)
        if value < self._minimum_information:
            return (), self._refuse(NOT_WORTH_THE_COST)

        intents = (
            self._intent(long_opinion, regime, value, cost, short_opinion.bot),
            self._intent(short_opinion, regime, value, cost, long_opinion.bot),
        )
        pair = OpenPair(
            venue_id=venue_id,
            symbol=symbol,
            long_bot=long_opinion.bot,
            short_bot=short_opinion.bot,
            regime=regime.regime,
            intents=intents,
            cost_fraction=(cost or 0.0) * 2,
            opened_at_ns=self._now_ns(),
        )
        self._open[(venue_id, symbol)] = pair
        self.standing.pairs_opened += 1
        self.standing.by_regime[regime.regime] = (
            self.standing.by_regime.get(regime.regime, 0) + 1
        )
        self.standing.most_expensive_experiment = max(
            self.standing.most_expensive_experiment, pair.cost_fraction
        )
        return intents, OPENED

    def _question_is_already_answered(self, long_bot: str, short_bot: str, regime: str) -> bool:
        """One bot clearly more proven here makes this a question, not an experiment."""
        long_trades = self._maturity.get((long_bot, regime), 0)
        short_trades = self._maturity.get((short_bot, regime), 0)
        return abs(long_trades - short_trades) >= self._maturity_gap

    def _intent(self, opinion, regime, value: float, cost, opposing_bot: str) -> TradeIntent:
        return TradeIntent(
            venue_id=opinion.venue_id,
            symbol=opinion.symbol,
            side=opinion.side,
            action=OPEN,
            conviction=Estimate(
                value=opinion.conviction.value,
                is_fitted=opinion.conviction.is_fitted,
                observations=opinion.conviction.observations,
                prior=opinion.conviction.prior,
                was_clamped=False,
                bound_low=None,
                bound_high=None,
                reason="one leg of an experiment; the conviction is the bot's, not the brain's",
            ),
            horizon_seconds=(
                opinion.exit_plan.horizon_seconds if opinion.exit_plan is not None else 0.0
            ),
            stop_price=opinion.exit_plan.stop_price if opinion.exit_plan is not None else None,
            agreement=RULED,
            contributing_bots=(opinion.bot,),
            dissenting_bots=(opposing_bot,),
            opinion_weights={opinion.bot: 1.0},
            evidence={
                "is_an_exploration_leg": True,
                "opposing_bot": opposing_bot,
                "regime": regime.regime,
                "information_value": value,
                "round_trip_cost_fraction": cost,
                "bot_reason": opinion.reason,
            },
            reason=(
                f"{opinion.side} {opinion.symbol} as one leg of an experiment: {opinion.bot} and "
                f"{opposing_bot} disagree in {regime.regime} and nothing separates them, so "
                f"both sides are taken small and the market decides. This is not a hedge -- the "
                f"legs are meant to resolve, and the winning one is the evidence"
            ),
            formed_at_ns=self._now_ns(),
        )

    def _refuse(self, reason: str) -> str:
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason


def describe_exploration(opener: ExplorationPairOpener) -> dict:
    return {
        "part_id": PART_ID,
        "disagreements_seen": opener.standing.disagreements_seen,
        "pairs_opened": opener.standing.pairs_opened,
        "pairs_closed": opener.standing.pairs_closed,
        "pairs_open_now": len(opener._open),
        "refused_by_reason": dict(sorted(opener.standing.by_refusal.items())),
        "pairs_by_regime": dict(sorted(opener.standing.by_regime.items())),
        "most_expensive_experiment": opener.standing.most_expensive_experiment,
    }


def run_exploration_pair_opener(
    opener: ExplorationPairOpener, control_socket, read_opinions_and_regime, publish_intents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        intents = []
        for opinions, regime in read_opinions_and_regime(opener):
            opened, _ = opener.consider(opinions, regime)
            intents.extend(opened)
        publish_intents(tuple(intents))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
