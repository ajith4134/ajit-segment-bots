"""bot-weight-sampler: how much each bot's opinion counts, and why it keeps sampling losers.

The arbiter needs a number per bot and that number must not be typed by anybody
(RL-061). This is where it comes from -- and the harder half of the problem is
not measuring which bot is right, it is not stopping the measurement.

A bot weighted to zero produces no more trades, so it produces no more evidence,
so its weight can never change. That is a trap the system cannot get out of by
itself, and it is why this part **samples** rather than simply ranking:

- **Regret, not just hit rate.** A bot whose opinions were passed over and would
  have made money carries regret, and regret is what says a bot was ignored
  wrongly rather than merely wrong. Without it a bot that is right about trades
  nobody took looks identical to one that is right about nothing.
- **Weights are floored**, so every bot keeps getting a share of the decisions
  and keeps producing the record that could clear it (RL-005).
- **Exploration is deliberate and marked.** A weight raised to keep sampling a
  bot is flagged as such, so a trade taken for exploration is not later read as
  a trade the system was confident about.

**Per regime.** A bot that is right in a trend and wrong in a chop has two
records; one weight over both describes neither and would keep the trend bot in
the chop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import BotWeight

PART_ID = "bot-weight-sampler"

PART_DECLARATION = PartDeclaration(
    part_id="bot-weight-sampler",
    consumes=("bot-regret", "bot-scorecard", "market-regime"),
    produces=("bot-weight", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass
class BotRecord:
    hit_rate: RateEstimator
    regret: float = 0.0
    trades: int = 0
    opinions_passed_over: int = 0


@dataclass
class SamplerStanding:
    bots_tracked: int = 0
    regimes_tracked: int = 0
    trades_learned_from: int = 0
    weights_published: int = 0
    exploration_raises: int = 0
    at_the_floor: int = 0
    highest_weight: float = 0.0
    by_bot: dict = field(default_factory=dict)


class BotWeightSampler:
    """Weights each bot per regime, and keeps sampling the ones it has doubts about."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        minimum_weight: float,
        maximum_weight: float,
        regret_weight: float,
        exploration_floor_observations: int,
        exploration_weight: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_weight < maximum_weight:
            raise ValueError(
                "the floor must be positive and below the cap: a bot weighted to zero "
                "produces no evidence, so its weight could never change again"
            )
        if exploration_weight < minimum_weight:
            raise ValueError(
                "the exploration weight is what keeps an unproven bot being sampled; below "
                "the floor it would do nothing"
            )
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._minimum_weight = minimum_weight
        self._maximum_weight = maximum_weight
        self._regret_weight = regret_weight
        self._exploration_floor = exploration_floor_observations
        self._exploration_weight = exploration_weight
        self._now_ns = now_ns
        self._records: dict[tuple[str, str], BotRecord] = {}
        self.standing = SamplerStanding()

    def observe_closed_trade(self, bot: str, regime: str, was_win: bool) -> None:
        record = self._record_for(bot, regime)
        record.hit_rate.observe(was_win)
        record.trades += 1
        self.standing.trades_learned_from += 1
        self._recount()

    def observe_regret(self, bot: str, regime: str, regret: float) -> None:
        """What passing over this bot's opinion would have made, or cost.

        Positive regret means the bot was right and was not acted on. Without
        this a bot that is right about trades nobody took is indistinguishable
        from one that is right about nothing.
        """
        record = self._record_for(bot, regime)
        record.regret += regret
        record.opinions_passed_over += 1
        self._recount()

    def observe_scorecard(self, bot: str, scorecard) -> None:
        """Adopt a bot's durable record, so a restart does not relearn from nothing."""
        for regime, record in scorecard.describe()["by_regime"].items():
            for _ in range(record["wins"]):
                self.observe_closed_trade(bot, regime, True)
            for _ in range(record["trades"] - record["wins"]):
                self.observe_closed_trade(bot, regime, False)

    def weight_for(self, bot: str, regime: str) -> BotWeight:
        record = self._record_for(bot, regime)
        hit_rate = record.hit_rate.estimate(self._minimum)
        base = self._base_rate_excluding(bot, regime)

        exploring = record.trades < self._exploration_floor
        if exploring:
            # Not yet enough evidence to rank this bot, so it is sampled on
            # purpose rather than ranked on noise -- and the flag travels, so a
            # trade taken to learn is never read later as a trade taken from
            # confidence.
            weight = self._exploration_weight
            self.standing.exploration_raises += 1
            provenance = (
                f"{record.trades} closed trade(s) of the {self._exploration_floor} needed to "
                f"rank it in {regime}, so it is being sampled deliberately rather than ranked "
                f"on noise"
            )
        else:
            ratio = 1.0 if base.value <= 0 else hit_rate.value / base.value
            # Regret per passed-over opinion, so a bot with a hundred ignored
            # good calls is not read the same as one with a single lucky one.
            regret_per_opinion = (
                record.regret / record.opinions_passed_over
                if record.opinions_passed_over
                else 0.0
            )
            ratio += self._regret_weight * regret_per_opinion
            weight = min(self._maximum_weight, max(self._minimum_weight, ratio))
            provenance = (
                f"{hit_rate.value:.1%} over {record.trades} closed trade(s) in {regime} "
                f"against the {base.value:.1%} the other bots achieve"
                + (
                    f", raised for {regret_per_opinion:+.4f} of regret per passed-over opinion "
                    f"over {record.opinions_passed_over} of them"
                    if record.opinions_passed_over
                    else ""
                )
            )

        if weight == self._minimum_weight:
            self.standing.at_the_floor += 1
        self.standing.highest_weight = max(self.standing.highest_weight, weight)
        self.standing.weights_published += 1
        self.standing.by_bot[f"{bot}:{regime}"] = weight

        return BotWeight(
            bot=bot,
            regime=regime,
            weight=weight,
            hit_rate=hit_rate,
            regret=record.regret,
            trades_judged=record.trades,
            is_exploring=exploring,
            reason=(
                f"{bot} weighted {weight:.2f} in {regime}: {provenance}"
                + (
                    f"; held at the {self._minimum_weight:.2f} floor so it keeps producing the "
                    f"evidence that could clear it"
                    if weight == self._minimum_weight
                    else ""
                )
            ),
            weighed_at_ns=self._now_ns(),
        )

    def weights_in(self, regime: str) -> tuple[BotWeight, ...]:
        bots = sorted({bot for bot, kept in self._records if kept == regime})
        return tuple(self.weight_for(bot, regime) for bot in bots)

    def _base_rate_excluding(self, bot: str, regime: str) -> Estimate:
        """What the other bots achieve in this regime, which is what "better" means."""
        estimates = [
            record.hit_rate.estimate(self._minimum)
            for (other, kept), record in self._records.items()
            if kept == regime and other != bot and record.trades > 0
        ]
        if not estimates:
            return Estimate(
                value=self._prior_hit_rate, is_fitted=False, observations=0,
                prior=self._prior_hit_rate, was_clamped=False, bound_low=None,
                bound_high=None, reason=f"no other bot has traded in {regime} yet",
            )
        observations = sum(estimate.observations for estimate in estimates)
        return Estimate(
            value=sum(estimate.value * estimate.observations for estimate in estimates)
            / observations,
            is_fitted=all(estimate.is_fitted for estimate in estimates),
            observations=observations,
            prior=self._prior_hit_rate,
            was_clamped=False,
            bound_low=None,
            bound_high=None,
            reason=f"{len(estimates)} other bot(s) over {observations} trades in {regime}",
        )

    def _record_for(self, bot: str, regime: str) -> BotRecord:
        key = (bot, regime)
        record = self._records.get(key)
        if record is None:
            record = BotRecord(
                hit_rate=RateEstimator(
                    prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                )
            )
            self._records[key] = record
            self._recount()
        return record

    def _recount(self) -> None:
        self.standing.bots_tracked = len({bot for bot, _ in self._records})
        self.standing.regimes_tracked = len({regime for _, regime in self._records})


def describe_bot_weights(sampler: BotWeightSampler) -> dict:
    return {
        "part_id": PART_ID,
        "bots_tracked": sampler.standing.bots_tracked,
        "regimes_tracked": sampler.standing.regimes_tracked,
        "trades_learned_from": sampler.standing.trades_learned_from,
        "weights_published": sampler.standing.weights_published,
        "weights_raised_for_exploration": sampler.standing.exploration_raises,
        "weights_at_the_floor": sampler.standing.at_the_floor,
        "highest_weight": sampler.standing.highest_weight,
        "weights": dict(sorted(sampler.standing.by_bot.items())),
    }


def run_bot_weight_sampler(
    sampler: BotWeightSampler, control_socket, read_records_and_regime, publish_weights,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        regime = read_records_and_regime(sampler)
        publish_weights(sampler.weights_in(regime))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_bot_weights(sampler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Scorecards carry each bot's record per regime and are adopted whole;
    regret per bot per regime is observed as it is tracked; the weights for
    the regime the market is in now go out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey

    regrets = Batch(read=context.bus.reader("bot-regret"))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    publish_weights = context.bus.publisher_for("bot-weight")
    sampler = BotWeightSampler(
        prior_hit_rate=context.number("brain_prior_hit_rate"),
        prior_weight=context.number("brain_prior_weight"),
        half_life_observations=context.number("brain_half_life_observations"),
        minimum_observations=int(context.number("brain_minimum_observations")),
        minimum_weight=context.number("bot_weight_minimum"),
        maximum_weight=context.number("bot_weight_maximum"),
        regret_weight=context.number("bot_weight_regret_weight"),
        exploration_floor_observations=int(context.number("bot_weight_exploration_floor_observations")),
        exploration_weight=context.number("bot_weight_exploration_weight"),
    )
    last_publish = [float("-inf")]

    def tick() -> None:
        for scorecard in scorecards.payloads():
            sampler.observe_scorecard(scorecard.bot, scorecard)
        for regret in regrets.payloads():
            if regret.median_regret is not None:
                sampler.observe_regret(regret.bot, regret.regime, regret.median_regret)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        # One weight set per regime currently observed on any symbol.
        seen = {r.regime for r in regimes.mapping().values() if r.is_classified}
        weights = tuple(weight for regime in sorted(seen) for weight in sampler.weights_in(regime))
        if weights:
            publish_weights(weights)
        last_publish[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_bot_weights(sampler),
    )
