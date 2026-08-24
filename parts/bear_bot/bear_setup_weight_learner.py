"""bear-setup-weight-learner: how much this bot should trust each detector's shorts.

The bull's counterpart weights a detector by its hit rate against the rest of the
bot. That is right for a book whose losses are bounded. For a short book it is
not sufficient on its own, and this part weights on **two** things:

1. **Hit rate against the rest of this bot's short book**, excluding the detector
   itself -- a detector producing most of the trades would otherwise drag the
   comparison to its own level and come out exactly average however good it is.
2. **The shape of its losses.** A detector that wins seven shorts in ten and
   loses the other three to squeezes is not a good detector at a 70% hit rate.
   The short side's losing tail is unbounded, so a detector whose worst loss
   dwarfs its median win is discounted for that, separately from how often it
   wins. Hit rate alone cannot see the difference, and by the time it can the
   money is gone.

**A floor, not zero**: RL-005 keeps the paper stage experimenting, and a detector
weighted to zero stops producing the evidence that could clear it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SetupWeight
from runtime.learned_estimator import Estimate, QuantileEstimator, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-setup-weight-learner"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-setup-weight-learner",
    consumes=("bot-scorecard", "instruction-scorecard"),
    produces=("bear-setup-weight", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)


@dataclass
class DetectorRecord:
    hit_rate: RateEstimator
    losses: QuantileEstimator
    wins: QuantileEstimator
    trades: int = 0


@dataclass
class LearnerStanding:
    detectors_tracked: int = 0
    weights_published: int = 0
    trades_learned_from: int = 0
    detectors_at_the_floor: int = 0
    detectors_discounted_for_tail: int = 0
    highest_weight: float = 0.0
    by_detector: dict = field(default_factory=dict)


class BearSetupWeightLearner:
    """Weights a detector's shorts by how often it wins and by how badly it loses."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        minimum_weight: float,
        maximum_weight: float,
        loss_window: int,
        prior_loss_fraction: float,
        prior_win_fraction: float,
        tail_quantile: float,
        tolerated_tail_ratio: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= minimum_weight < maximum_weight:
            raise ValueError(
                "the floor must sit below the cap and at or above zero; a floor of zero would "
                "end the evidence for a detector permanently"
            )
        if tolerated_tail_ratio <= 0:
            raise ValueError(
                "the tolerated ratio is how many median wins the worst loss may be worth; "
                "zero would discount every detector including the good ones"
            )
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._minimum_weight = minimum_weight
        self._maximum_weight = maximum_weight
        self._loss_window = loss_window
        self._prior_loss_fraction = prior_loss_fraction
        self._prior_win_fraction = prior_win_fraction
        self._tail_quantile = tail_quantile
        self._tolerated_tail_ratio = tolerated_tail_ratio
        self._now_ns = now_ns
        self._records: dict[str, DetectorRecord] = {}
        self._instruction_of: dict[str, str] = {}
        self.standing = LearnerStanding()

    def observe_closed_trade(self, detector: str, was_win: bool, realised_fraction: float) -> None:
        """One closed short: whether it worked, and by how much either way.

        The magnitude is not optional here. A short book's risk lives in the size
        of its losses rather than their count, and a learner given only the count
        cannot tell a steady detector from one that is short volatility.
        """
        record = self._record_for(detector)
        record.hit_rate.observe(was_win)
        record.trades += 1
        if was_win:
            record.wins.observe(abs(realised_fraction))
        else:
            record.losses.observe(abs(realised_fraction))
        self.standing.trades_learned_from += 1
        self.standing.detectors_tracked = len(self._records)

    def observe_scorecard(self, scorecard) -> None:
        """Adopt this bot's durable record, so a restart does not relearn from nothing."""
        for detector, record in scorecard.describe()["by_detector"].items():
            trades = record["trades"]
            wins = record["wins"]
            if not trades:
                continue
            # The scorecard keeps totals rather than per-trade magnitudes, so the
            # average is what it can honestly supply; the per-trade record
            # rebuilds as new trades close.
            average = abs(record["realised"]) / trades
            for _ in range(wins):
                self.observe_closed_trade(detector, True, average)
            for _ in range(trades - wins):
                self.observe_closed_trade(detector, False, average)

    def observe_instruction_scorecard(
        self, instruction_id: str, detector: str, wins: int, trades: int, average_fraction: float
    ) -> None:
        if trades < 0 or not 0 <= wins <= trades:
            raise ValueError("an instruction cannot have won more trades than it took")
        self._instruction_of[detector] = instruction_id
        for _ in range(wins):
            self.observe_closed_trade(detector, True, average_fraction)
        for _ in range(trades - wins):
            self.observe_closed_trade(detector, False, average_fraction)

    def base_rate_excluding(self, detector: str) -> Estimate:
        """What the rest of this bot's short book achieves."""
        estimates = [
            record.hit_rate.estimate(self._minimum)
            for name, record in self._records.items()
            if name != detector and record.trades > 0
        ]
        if not estimates:
            return Estimate(
                value=self._prior_hit_rate, is_fitted=False, observations=0,
                prior=self._prior_hit_rate, was_clamped=False, bound_low=None, bound_high=None,
                reason="no other detector has a short record yet, so the prior stands",
            )
        observations = sum(estimate.observations for estimate in estimates)
        value = sum(
            estimate.value * estimate.observations for estimate in estimates
        ) / observations if observations else self._prior_hit_rate
        return Estimate(
            value=value,
            is_fitted=all(estimate.is_fitted for estimate in estimates),
            observations=observations,
            prior=self._prior_hit_rate,
            was_clamped=False,
            bound_low=None,
            bound_high=None,
            reason=f"{len(estimates)} other detector(s) over {observations} short trades",
        )

    def tail_ratio(self, detector: str) -> tuple[float | None, bool]:
        """How many median wins this detector's worst loss is worth."""
        record = self._records.get(detector)
        if record is None:
            return None, False
        worst_loss = record.losses.estimate(self._tail_quantile, self._minimum)
        median_win = record.wins.estimate(0.5, self._minimum)
        if median_win.value <= 0:
            return None, False
        return worst_loss.value / median_win.value, worst_loss.is_fitted and median_win.is_fitted

    def weight_for(self, detector: str) -> SetupWeight:
        record = self._record_for(detector)
        hit_rate = record.hit_rate.estimate(self._minimum)
        base = self.base_rate_excluding(detector)

        if base.value <= 0:
            ratio = self._maximum_weight if hit_rate.value > 0 else 1.0
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} short trades while every "
                f"other detector this bot has tried has won nothing"
            )
        else:
            ratio = hit_rate.value / base.value
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} short trades against the "
                f"{base.value:.1%} the rest of this bot's short book achieves"
                + ("" if hit_rate.is_fitted else ", still pulled toward the prior")
            )

        tail, tail_is_measured = self.tail_ratio(detector)
        tail_note = ""
        if tail is not None and tail > self._tolerated_tail_ratio:
            discount = self._tolerated_tail_ratio / tail
            ratio *= discount
            self.standing.detectors_discounted_for_tail += 1
            tail_note = (
                f"; discounted by {1 - discount:.0%} because its worst loss is worth {tail:.1f} "
                f"median wins, past the {self._tolerated_tail_ratio:.1f} this bot tolerates -- a "
                f"detector can win most of its shorts and still be short volatility"
                + ("" if tail_is_measured else ", though that tail is not yet measured")
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
            trades_judged=record.trades,
            reason=(
                f"{detector} weighted {weight:.2f}: {provenance}{tail_note}"
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
        return tuple(self.weight_for(detector) for detector in sorted(self._records))

    def _record_for(self, detector: str) -> DetectorRecord:
        record = self._records.get(detector)
        if record is None:
            record = DetectorRecord(
                hit_rate=RateEstimator(
                    prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                ),
                losses=QuantileEstimator(window=self._loss_window, prior=self._prior_loss_fraction),
                wins=QuantileEstimator(window=self._loss_window, prior=self._prior_win_fraction),
            )
            self._records[detector] = record
        return record


def describe_setup_weights(learner: BearSetupWeightLearner) -> dict:
    return {
        "part_id": PART_ID,
        "detectors_tracked": learner.standing.detectors_tracked,
        "trades_learned_from": learner.standing.trades_learned_from,
        "weights_published": learner.standing.weights_published,
        "detectors_discounted_for_their_losing_tail": learner.standing.detectors_discounted_for_tail,
        "detectors_at_the_floor": learner.standing.detectors_at_the_floor,
        "highest_weight": learner.standing.highest_weight,
        "weights": dict(sorted(learner.standing.by_detector.items())),
        "tail_ratios": {
            detector: learner.tail_ratio(detector)[0] for detector in sorted(learner._records)
        },
    }


def run_bear_setup_weight_learner(
    learner: BearSetupWeightLearner, control_socket, read_scorecards, publish_weights,
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
    Weights go out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    instruction_cards = Batch(read=context.bus.reader("instruction-scorecard"))
    publish_weights = context.bus.publisher_for("bear-setup-weight")
    learner = BearSetupWeightLearner(
        prior_hit_rate=context.number("bear_setup_weight_prior_hit_rate"),
        prior_weight=context.number("bear_setup_weight_prior_weight"),
        half_life_observations=context.number("bear_feature_half_life_observations"),
        minimum_observations=int(context.number("bear_setup_weight_minimum_observations")),
        minimum_weight=context.number("bear_setup_weight_minimum"),
        maximum_weight=context.number("bear_setup_weight_maximum"),
        loss_window=int(context.number("bear_setup_weight_loss_window")),
        prior_loss_fraction=context.number("bear_setup_weight_prior_loss_fraction"),
        prior_win_fraction=context.number("bear_setup_weight_prior_win_fraction"),
        tail_quantile=context.number("bear_setup_weight_tail_quantile"),
        tolerated_tail_ratio=context.number("bear_setup_weight_tolerated_tail_ratio"),
    )
    last_publish = [float("-inf")]

    def read_scorecards(_learner) -> None:
        for scorecard in scorecards.payloads():
            if getattr(scorecard, "bot", None) == BOT:
                learner.observe_scorecard(scorecard)
        for card in instruction_cards.payloads():
            learner.observe_instruction_scorecard(card.instruction_id, card.instruction_id, card.wins, card.trades)

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
