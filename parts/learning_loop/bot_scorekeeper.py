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
    # Closed trades whose standardised outcome was exactly zero -- flat, or on a
    # symbol whose move over the horizon measured zero. Not evidence either way.
    outcomes_without_evidence: int = 0
    rewards_applied: int = 0
    rewards_uninterpretable: int = 0
    last_uninterpretable_reward: str | None = None
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
        """A positive weight, stating how much this bot's record should count."""
        if multiplier <= 0:
            raise ValueError("a non-positive multiplier would erase a bot's record")
        self._reward_multipliers[bot] = multiplier
        self.standing.rewards_applied += 1

    def note_uninterpretable_reward(self, reason: str) -> None:
        """A `learning-reward` arrived that cannot be read as a weight multiplier.

        A shaped reward is signed -- it is USDT times a set of components, and a
        losing trade shapes to a negative figure. A weight multiplier is a
        positive number around one. Turning one into the other is a decision
        nobody has made, and a wrong one would silently rescale every bot's
        record, so it is refused and counted here rather than guessed at.

        Refused rather than raised. Passing the signed figure in as a multiplier
        crashed this part 13 times on 2026-08-28 and `tail-follow-conviction-model`
        with it: the raise is right about the value and wrong about the response,
        because a losing trade is ordinary traffic and a part may not fall over on
        ordinary traffic.
        """
        self.standing.rewards_uninterpretable += 1
        self.standing.last_uninterpretable_reward = reason

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


def evidence_weight_of(significance) -> float | None:
    """How much one closed trade counts, from its outcome-significance.

    luck-skill-separator standardises the *signed* return against the symbol's own
    move, so a loss arrives negative. Whether the opinion was right already carries
    the sign; the weight is the distance from noise. Unassessed or unmeasurable
    counts as an ordinary observation, as it always has. Exactly zero -- a flat
    trade, or no measured move -- is None: not an observation either way.
    """
    if significance is None or significance.standardised is None:
        return 1.0
    weight = abs(significance.standardised)
    return weight if weight > 0 else None


def describe_scorekeeping(scorekeeper: BotScorekeeper) -> dict:
    return {
        "part_id": PART_ID,
        "opinions_recorded": scorekeeper.standing.opinions_recorded,
        "from_taken_trades": scorekeeper.standing.from_taken_trades,
        "from_counterfactuals": scorekeeper.standing.from_counterfactuals,
        "clustered_entries_collapsed": scorekeeper.standing.clustered_entries_collapsed,
        "outcomes_without_evidence": scorekeeper.standing.outcomes_without_evidence,
        "rewards_applied": scorekeeper.standing.rewards_applied,
        "rewards_uninterpretable": scorekeeper.standing.rewards_uninterpretable,
        "last_uninterpretable_reward": scorekeeper.standing.last_uninterpretable_reward,
        "bots_tracked": scorekeeper.standing.bots_tracked,
        "by_bot": dict(sorted(scorekeeper.standing.by_bot.items())),
        "scorecards": {
            bot: scorecard.describe() for bot, scorecard in sorted(scorekeeper._scorecards.items())
        },
    }


def run_bot_scorekeeper(
    scorekeeper: BotScorekeeper, control_socket, read_outcomes, publish_scorecards,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_scorekeeping(scorekeeper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    An opinion is remembered until the episode that closed the trade it led
    to arrives; the episode says what the trade realised, the pair verdict
    or counterfactual says what the passed-over opinion would have, the
    significance says how much noise the outcome carries, and the cluster
    says how many effective bets it was. Scorecards go out once per health
    interval.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey

    opinions = Batch(read=context.bus.reader("directional-opinion"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    counterfactuals = Batch(read=context.bus.reader("counterfactual-outcome"))
    rewards = Batch(read=context.bus.reader("learning-reward"))
    clusters = Batch(read=context.bus.reader("trade-cluster"))
    # Keyed by trade_id, so the key space is every trade ever closed, and none of
    # these producers restates -- each publishes once per closed trade. Bounded
    # 2026-09-04 by how long a join over one closed trade may wait for the rest of
    # its facts; since that date an expired key is dropped, not merely hidden.
    join_age = context.number("closed_trade_join_maximum_age_seconds")
    significances = LatestByKey(read=context.bus.reader("outcome-significance"), key_of=lambda s: s.trade_id, maximum_age_seconds=join_age)
    verdicts = Batch(read=context.bus.reader("pair-verdict"))
    costs = Batch(read=context.bus.reader("decision-cost"))
    publish_scorecards = context.bus.publisher_for("bot-scorecard")
    scorekeeper = BotScorekeeper(
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
    )
    # The last acting opinion per bot per symbol: what the bot said before the
    # trade the episode closes. Bounded by symbols the bots have opinions on.
    last_opinion: dict[tuple[str, str, str], object] = {}
    cluster_of: dict[str, str] = {}
    last_publish = [float("-inf")]

    def read_outcomes(_scorekeeper) -> None:
        for opinion in opinions.payloads():
            if opinion.is_a_call_to_act:
                last_opinion[(opinion.bot, opinion.venue_id, opinion.symbol)] = opinion
        for cluster in clusters.payloads():
            for trade_id in cluster.trade_ids:
                cluster_of[trade_id] = cluster.cluster_id
        for reward in rewards.payloads():
            scorekeeper.note_uninterpretable_reward(
                f"{reward.detector} sent a shaped reward of {reward.reward!r} in state "
                f"{reward.state!r}; no conversion from a shaped reward to a weight "
                f"multiplier has been decided, and a signed figure cannot be one"
            )
        counterfactuals.payloads()
        verdicts.payloads()
        costs.payloads()
        by_trade = significances.mapping()
        for episode in episodes.payloads():
            trade_id = episode.episode_id.split("-")[1] if "-" in episode.episode_id else episode.episode_id
            weight = evidence_weight_of(by_trade.get(trade_id))
            if weight is None:
                scorekeeper.standing.outcomes_without_evidence += 1
            for (bot, venue_id, symbol), opinion in list(last_opinion.items()):
                if (venue_id, symbol) != (episode.venue_id, episode.symbol):
                    continue
                if weight is None:
                    # The opinion was settled by this trade, just not as evidence;
                    # left in place it would be credited with the next trade's result.
                    del last_opinion[(bot, venue_id, symbol)]
                    continue
                scorekeeper.record_opinion_outcome(
                    bot=bot, detector=episode.detector, regime=episode.regime,
                    stated_probability=opinion.conviction.value,
                    the_opinion_was_right=episode.realised > 0,
                    realised=episode.realised,
                    significance=weight,
                    cluster_id=cluster_of.get(trade_id),
                )
                del last_opinion[(bot, venue_id, symbol)]

    def tick() -> None:
        read_outcomes(scorekeeper)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        cards = tuple(scorekeeper.scorecard_for(bot) for bot in sorted(scorekeeper._scorecards))
        if cards:
            publish_scorecards(cards)
        last_publish[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_scorekeeping(scorekeeper),
    )
