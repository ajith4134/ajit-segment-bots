"""bull-setup-weight-learner: how much this bot should trust each detector, from results.

The setup filter needs a number per detector and that number must not be typed by
anybody (RL-061). This is where it comes from: the bot's own scorecard on restart
(a durable record adopted whole so a restart does not relearn from nothing), the
instruction scorecard for detectors that came from a learned instruction, and --
since 2026-08-30 -- `training-label`'s own `THE_SETUP_WAS_RIGHT` component going
forward, so the detector is trusted for calling the setup right, not for whatever
this bot's own entry timing, exit timing or sizing did to the trade afterwards.
The scorecard blends all four into one win/loss and stays only as the prior a
restart needs before enough decomposed labels have arrived to outweigh it.

**Long results only.** A detector that is right about shorts and wrong about
longs must be weighted low *here* while the bear bot weights it high, and a
single number over both sides would let each bot inherit the other's edge. The
scorecard this part reads is the bull bot's own.

The weight is a **Beta-Bernoulli posterior against the bot's own base rate**,
which is the honest form of "better than usual":

- A detector with three wins out of three is not three times as good as the base
  rate. The prior pulls it back toward it, and the pull weakens as evidence
  accumulates -- which is exactly the behaviour that stops a bot piling into a
  detector that has had a good morning.
- A detector below the base rate is discounted rather than deleted. Deleting it
  would end the evidence, and a detector that stops being sampled can never be
  found to have started working again.

**A floor, not zero.** The floor is what keeps exploration alive; RL-005 is
explicit that the paper stage experiments without restriction, and a bot that
weighted a losing detector to zero would stop learning about it permanently.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, SetupWeight
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-setup-weight-learner"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-setup-weight-learner",
    consumes=("bot-scorecard", "instruction-scorecard", "training-label"),
    produces=("bull-setup-weight", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)


def setup_outcome_for(label, direction: str) -> bool | None:
    """This label's `THE_SETUP_WAS_RIGHT` verdict, or None if it does not apply.

    A single detector fires both directions, and `TrainingLabel` carries no
    notion of which bot's trade it was labelling beyond `direction` itself --
    so a label for the other side is not this bot's evidence, and a label
    whose setup component was never judged (a claim-based label the labeller
    has not yet resolved) is not evidence at all.
    """
    if label.direction != direction:
        return None
    return label.label_for(THE_SETUP_WAS_RIGHT)


@dataclass
class LearnerStanding:
    detectors_tracked: int = 0
    weights_published: int = 0
    trades_learned_from: int = 0
    detectors_at_the_floor: int = 0
    highest_weight: float = 0.0
    by_detector: dict = field(default_factory=dict)


class BullSetupWeightLearner:
    """Turns closed long trades into a per-detector weight the filter can apply."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        minimum_weight: float,
        maximum_weight: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= minimum_weight < maximum_weight:
            raise ValueError(
                "the floor must sit below the cap and at or above zero; a floor of zero would "
                "end the evidence for a detector permanently"
            )
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._minimum_weight = minimum_weight
        self._maximum_weight = maximum_weight
        self._now_ns = now_ns
        self._by_detector: dict[str, RateEstimator] = {}
        self._instruction_of: dict[str, str] = {}
        self.standing = LearnerStanding()

    def observe_closed_trade(self, detector: str, was_win: bool) -> None:
        """One closed long trade attributed to the detector that proposed it."""
        self._estimator_for(detector).observe(was_win)
        self.standing.trades_learned_from += 1
        self.standing.detectors_tracked = len(self._by_detector)

    def base_rate_excluding(self, detector: str) -> Estimate:
        """What the rest of the bot achieves, which is what "better" is measured against.

        Excluding the detector itself, because a detector that produces most of
        the bot's trades drags the overall rate toward its own and would come
        out at exactly average however good or bad it is -- and the detector
        that trades most is the one it matters most to judge.
        """
        estimates = [
            self._by_detector[other].estimate(self._minimum)
            for other in self._by_detector
            if other != detector
        ]
        weighted = [estimate for estimate in estimates if estimate.observations > 0]
        if not weighted:
            return Estimate(
                value=self._prior_hit_rate,
                is_fitted=False,
                observations=0,
                prior=self._prior_hit_rate,
                was_clamped=False,
                bound_low=None,
                bound_high=None,
                reason=(
                    "no other detector has a record yet, so there is nothing to be better "
                    "than and the prior stands"
                ),
            )
        observations = sum(estimate.observations for estimate in weighted)
        value = sum(
            estimate.value * estimate.observations for estimate in weighted
        ) / observations
        return Estimate(
            value=value,
            is_fitted=all(estimate.is_fitted for estimate in weighted),
            observations=observations,
            prior=self._prior_hit_rate,
            was_clamped=False,
            bound_low=None,
            bound_high=None,
            reason=(
                f"{len(weighted)} other detector(s) over {observations} long trades"
            ),
        )

    def observe_scorecard(self, scorecard) -> None:
        """Adopt the bot's durable record, so a restart does not relearn from nothing."""
        for detector, record in scorecard.describe()["by_detector"].items():
            for _ in range(record["wins"]):
                self.observe_closed_trade(detector, True)
            for _ in range(record["trades"] - record["wins"]):
                self.observe_closed_trade(detector, False)

    def observe_instruction_scorecard(self, instruction_id: str, detector: str, wins: int, trades: int) -> None:
        """A detector that came from a learned instruction carries that instruction's record.

        Without this a newly compiled instruction would start at the base rate
        even though the hypothesis behind it was already tested -- and the
        evidence that justified compiling it would be thrown away.
        """
        if trades < 0 or not 0 <= wins <= trades:
            raise ValueError("an instruction cannot have won more trades than it took")
        self._instruction_of[detector] = instruction_id
        for _ in range(wins):
            self.observe_closed_trade(detector, True)
        for _ in range(trades - wins):
            self.observe_closed_trade(detector, False)

    def weight_for(self, detector: str) -> SetupWeight:
        """This detector's hit rate against the bot's own, floored and capped."""
        estimator = self._estimator_for(detector)
        hit_rate = estimator.estimate(self._minimum)
        base = self.base_rate_excluding(detector)

        if base.value <= 0:
            ratio = self._maximum_weight if hit_rate.value > 0 else 1.0
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} long trades while every "
                f"other detector this bot has tried has won nothing"
            )
        else:
            ratio = hit_rate.value / base.value
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} long trades against the "
                f"{base.value:.1%} the rest of this bot achieves ({base.reason})"
                + ("" if hit_rate.is_fitted else ", still pulled toward the prior")
            )

        weight = min(self._maximum_weight, max(self._minimum_weight, ratio))
        if weight == self._minimum_weight:
            self.standing.detectors_at_the_floor += 1
        self.standing.highest_weight = max(self.standing.highest_weight, weight)
        self.standing.weights_published += 1
        self.standing.by_detector[detector] = weight

        instruction = self._instruction_of.get(detector)
        return SetupWeight(
            bot=BOT,
            detector=detector,
            weight=weight,
            hit_rate=hit_rate,
            trades_judged=hit_rate.observations,
            reason=(
                f"{detector} weighted {weight:.2f}: {provenance}"
                + (f"; carries the record of instruction {instruction}" if instruction else "")
                + (
                    f"; held at the {self._minimum_weight:.2f} floor so it keeps being sampled "
                    f"and can be found to work again"
                    if weight == self._minimum_weight
                    else ""
                )
            ),
            learned_at_ns=self._now_ns(),
        )

    def all_weights(self) -> tuple[SetupWeight, ...]:
        return tuple(self.weight_for(detector) for detector in sorted(self._by_detector))

    @property
    def base_rate(self) -> Estimate:
        """The bot's own hit rate over every detector, for reporting."""
        estimates = [
            estimator.estimate(self._minimum) for estimator in self._by_detector.values()
        ]
        observed = [estimate for estimate in estimates if estimate.observations > 0]
        if not observed:
            return Estimate(
                value=self._prior_hit_rate, is_fitted=False, observations=0,
                prior=self._prior_hit_rate, was_clamped=False, bound_low=None,
                bound_high=None, reason="no long trade has closed yet",
            )
        observations = sum(estimate.observations for estimate in observed)
        return Estimate(
            value=sum(estimate.value * estimate.observations for estimate in observed) / observations,
            is_fitted=all(estimate.is_fitted for estimate in observed),
            observations=observations,
            prior=self._prior_hit_rate,
            was_clamped=False,
            bound_low=None,
            bound_high=None,
            reason=f"{len(observed)} detector(s) over {observations} long trades",
        )

    def _estimator_for(self, detector: str) -> RateEstimator:
        estimator = self._by_detector.get(detector)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._by_detector[detector] = estimator
        return estimator


def describe_setup_weights(learner: BullSetupWeightLearner) -> dict:
    base = learner.base_rate
    return {
        "part_id": PART_ID,
        "detectors_tracked": learner.standing.detectors_tracked,
        "trades_learned_from": learner.standing.trades_learned_from,
        "weights_published": learner.standing.weights_published,
        "base_hit_rate": base.value,
        "base_hit_rate_is_measured": base.is_fitted,
        "highest_weight": learner.standing.highest_weight,
        "weights": dict(sorted(learner.standing.by_detector.items())),
    }


def run_bull_setup_weight_learner(
    learner: BullSetupWeightLearner, control_socket, read_scorecards, publish_weights,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_scorecards(learner)
        publish_weights(learner.all_weights())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_setup_weights(learner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The bot's scorecard carries each detector's wins and trades and is
    adopted whole; an instruction scorecard carries a learned instruction's
    record, which is attributed to the detector named for that instruction.
    A training-label's `THE_SETUP_WAS_RIGHT` component is the decomposed
    signal going forward -- filtered to `direction == LONG` because a single
    detector fires both directions and a label carries no notion of which
    bot's trade it was labelling other than that. Weights go out once per
    health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    instruction_cards = Batch(read=context.bus.reader("instruction-scorecard"))
    labels = Batch(read=context.bus.reader("training-label"))
    publish_weights = context.bus.publisher_for("bull-setup-weight")
    learner = BullSetupWeightLearner(
        prior_hit_rate=context.number("bull_setup_weight_prior_hit_rate"),
        prior_weight=context.number("bull_setup_weight_prior_weight"),
        half_life_observations=context.number("bull_feature_half_life_observations"),
        minimum_observations=int(context.number("bull_setup_weight_minimum_observations")),
        minimum_weight=context.number("bull_setup_weight_minimum"),
        maximum_weight=context.number("bull_setup_weight_maximum"),
    )
    last_publish = [float("-inf")]

    def read_scorecards(_learner) -> None:
        for scorecard in scorecards.payloads():
            if getattr(scorecard, "bot", None) == BOT:
                learner.observe_scorecard(scorecard)
        for card in instruction_cards.payloads():
            learner.observe_instruction_scorecard(card.instruction_id, card.instruction_id, card.wins, card.trades)
        for label in labels.payloads():
            outcome = setup_outcome_for(label, LONG)
            if outcome is None:
                continue
            learner.observe_closed_trade(label.detector, outcome)

    def tick() -> None:
        read_scorecards(learner)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        weights = learner.all_weights()
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
        read_standing=lambda: describe_setup_weights(learner),
    )
