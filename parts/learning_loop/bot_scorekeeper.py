"""bot-scorekeeper: the record every bot learns from, kept honestly.

Every weight, every calibration and every graduation decision in this system
reads from this scorecard. That makes the ways it can quietly lie the most
important thing about it:

- **A bot is credited for the opinions it gave, not for the trades that
  happened.** The arbiter may have overruled it, sized it differently or acted
  late. Attributing the result of a modified trade to the bot's opinion makes the
  bot's record a measure of the whole system, and the bot then learns from noise
  it did not create.
- **An opinion the arbiter did not act on still gets a record**, from the
  counterfactual. Without that, a bot whose good calls were all overruled looks
  identical to one with no good calls -- and the fix for each is opposite.
- **Outcome significance scales the entry.** A trade that made money on one tick
  in a thin book is weaker evidence than one that worked through a whole session,
  and counting them equally lets luck accumulate into a reputation.
- **Clustered trades count once.** Ten simultaneous entries on correlated symbols
  in the same setup are one bet; recording ten wins turns a single lucky call into
  a track record.

**Split by regime and by detector.** A bot with one number describes neither of
the markets it works in.

**Every entry is stamped and decayed.** A scorecard without decay reports a bot
that was excellent last year as excellent now.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import BotScorecard
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bot-scorekeeper"

PART_DECLARATION = PartDeclaration(
    part_id="bot-scorekeeper",
    consumes=(
        "directional-opinion", "trade-episode", "counterfactual-outcome", "learning-reward",
        "trade-cluster", "outcome-significance", "pair-verdict", "decision-cost",
    ),
    produces=("bot-scorecard", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FROM_A_TAKEN_TRADE = "the-arbiter-acted-on-this-opinion"
FROM_A_COUNTERFACTUAL = "the-opinion-was-not-acted-on-and-was-replayed"
CLUSTERED = "one-of-several-correlated-entries-in-the-same-setup"


@dataclass
class ScorekeeperStanding:
    opinions_recorded: int = 0
    from_taken_trades: int = 0
    from_counterfactuals: int = 0
    clustered_entries_collapsed: int = 0
    rewards_applied: int = 0
    bots_tracked: int = 0
    by_bot: dict = field(default_factory=dict)


class BotScorekeeper:
    """Keeps each bot's record of its own opinions, separately from what was traded."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._scorecards: dict[str, BotScorecard] = {}
        self._rates: dict[tuple[str, str, str], RateEstimator] = {}
        self._reward_multipliers: dict[str, float] = {}
        self._clusters_seen: set[str] = set()
        self.standing = ScorekeeperStanding()

    def observe_learning_reward(self, bot: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("a non-positive multiplier would erase a bot's record")
        self._reward_multipliers[bot] = multiplier
        self.standing.rewards_applied += 1

    def record_opinion_outcome(
        self,
        bot: str,
        detector: str,
        regime: str,
        stated_probability: float,
        the_opinion_was_right: bool,
        realised: float,
        source: str = FROM_A_TAKEN_TRADE,
        significance: float = 1.0,
        cluster_id: str | None = None,
    ) -> str:
        """One opinion's outcome, credited to the bot that gave it.

        `the_opinion_was_right` is about the opinion, not about the trade: the
        arbiter may have sized it differently or acted late, and attributing a
        modified trade's result to the opinion makes the bot's record a measure
        of the whole system.
        """
        if cluster_id is not None:
            if cluster_id in self._clusters_seen:
                # Ten simultaneous entries on correlated symbols in the same
                # setup are one bet. Recording ten wins turns one lucky call
                # into a track record.
                self.standing.clustered_entries_collapsed += 1
                return CLUSTERED
            self._clusters_seen.add(cluster_id)

        if significance <= 0:
            raise ValueError("an outcome with no significance is not an observation")

        self.standing.opinions_recorded += 1
        if source == FROM_A_COUNTERFACTUAL:
            self.standing.from_counterfactuals += 1
        else:
            self.standing.from_taken_trades += 1

        scorecard = self._scorecard_for(bot)
        scorecard.record_closed_trade(
            detector=detector, regime=regime, stated_probability=stated_probability,
            was_win=the_opinion_was_right, realised=realised,
        )

        # Significance and the learning reward scale how much this observation
        # moves the estimate, so a tick in a thin book cannot accumulate into a
        # reputation.
        weight = significance * self._reward_multipliers.get(bot, 1.0)
        estimator = self._rate_for(bot, detector, regime)
        whole = max(1, int(round(weight)))
        for _ in range(whole):
            estimator.observe(the_opinion_was_right)

        self.standing.bots_tracked = len(self._scorecards)
        self.standing.by_bot[bot] = self.standing.by_bot.get(bot, 0) + 1
        return source

    def scorecard_for(self, bot: str) -> BotScorecard:
        return self._scorecard_for(bot)

    def hit_rate(self, bot: str, detector: str, regime: str) -> Estimate:
        return self._rate_for(bot, detector, regime).estimate(self._minimum)

    def _scorecard_for(self, bot: str) -> BotScorecard:
        scorecard = self._scorecards.get(bot)
        if scorecard is None:
            scorecard = BotScorecard(bot=bot)
            self._scorecards[bot] = scorecard
        return scorecard

    def _rate_for(self, bot: str, detector: str, regime: str) -> RateEstimator:
        key = (bot, detector, regime)
        estimator = self._rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._rates[key] = estimator
        return estimator


def describe_scorekeeping(scorekeeper: BotScorekeeper) -> dict:
    return {
        "part_id": PART_ID,
        "opinions_recorded": scorekeeper.standing.opinions_recorded,
        "from_taken_trades": scorekeeper.standing.from_taken_trades,
        "from_counterfactuals": scorekeeper.standing.from_counterfactuals,
        "clustered_entries_collapsed": scorekeeper.standing.clustered_entries_collapsed,
        "rewards_applied": scorekeeper.standing.rewards_applied,
        "bots_tracked": scorekeeper.standing.bots_tracked,
        "by_bot": dict(sorted(scorekeeper.standing.by_bot.items())),
        "scorecards": {
            bot: scorecard.describe() for bot, scorecard in sorted(scorekeeper._scorecards.items())
        },
    }


def run_bot_scorekeeper(
    scorekeeper: BotScorekeeper, control_socket, read_outcomes, publish_scorecards,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_outcomes(scorekeeper)
        publish_scorecards(
            tuple(scorekeeper.scorecard_for(bot) for bot in sorted(scorekeeper._scorecards))
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
