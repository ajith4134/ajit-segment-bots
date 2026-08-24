"""tail-setup-weight-learner: how much to trust each way of finding a move.

The two directional bots weight detectors. This bot weights **sources**, and the
difference matters: its three ways of finding a move do not fail in the same way,
so one weight over all of them would be an average of three unrelated problems.

- **Scanner continuations** fail by being prints rather than moves, and by being
  late.
- **Our own winning leg** fails by concentration: the trades are good and the
  book ends up being one position.
- **Copied traders** fail by latency and by crowding, and a trader who was worth
  copying can stop being so without any of their published numbers changing.

So the weight is learned per source **and** per detector inside a source, which
is what lets the bot conclude "continuations are working but this detector's are
not" rather than turning a whole source off.

**The comparison is against this bot's own base rate**, excluding the source
being judged -- a source producing most of the follows would otherwise drag the
comparison to its own level and come out exactly average.

**A floor, not zero**, so a source that stops working keeps producing the
evidence that could clear it (RL-005).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SetupWeight
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-setup-weight-learner"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-setup-weight-learner",
    consumes=("bot-scorecard",),
    produces=("tail-setup-weight", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)


@dataclass
class LearnerStanding:
    sources_tracked: int = 0
    detectors_tracked: int = 0
    follows_learned_from: int = 0
    weights_published: int = 0
    at_the_floor: int = 0
    highest_weight: float = 0.0
    by_source: dict = field(default_factory=dict)


class TailSetupWeightLearner:
    """Weights the three ways of finding a move, and each detector inside them."""

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
                "end the evidence for a source permanently"
            )
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._minimum_weight = minimum_weight
        self._maximum_weight = maximum_weight
        self._now_ns = now_ns
        self._by_source: dict[str, RateEstimator] = {}
        self._by_detector: dict[tuple[str, str], RateEstimator] = {}
        self.standing = LearnerStanding()

    def observe_closed_follow(self, source: str, detector: str, was_profitable: bool) -> None:
        """One closed follow, attributed to both the source and the detector inside it."""
        self._source_estimator(source).observe(was_profitable)
        self._detector_estimator(source, detector).observe(was_profitable)
        self.standing.follows_learned_from += 1
        self.standing.sources_tracked = len(self._by_source)
        self.standing.detectors_tracked = len(self._by_detector)

    def observe_scorecard(self, scorecard) -> None:
        """Adopt the durable record, so a restart does not relearn from nothing."""
        for detector, record in scorecard.describe()["by_detector"].items():
            source, _, inner = detector.partition(":")
            for _ in range(record["wins"]):
                self.observe_closed_follow(source, inner or detector, True)
            for _ in range(record["trades"] - record["wins"]):
                self.observe_closed_follow(source, inner or detector, False)

    def base_rate_excluding(self, source: str) -> Estimate:
        estimates = [
            estimator.estimate(self._minimum)
            for name, estimator in self._by_source.items()
            if name != source
        ]
        observed = [estimate for estimate in estimates if estimate.observations > 0]
        if not observed:
            return Estimate(
                value=self._prior_hit_rate, is_fitted=False, observations=0,
                prior=self._prior_hit_rate, was_clamped=False, bound_low=None, bound_high=None,
                reason="no other source has a record yet, so the prior stands",
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
            reason=f"{len(observed)} other source(s) over {observations} follows",
        )

    def weight_for(self, source: str, detector: str | None = None) -> SetupWeight:
        """The weight for a source, or for one detector inside it.

        A detector's weight is its own record against its source's, so a source
        that is working overall does not carry a detector inside it that is not.
        """
        if detector is None:
            estimator = self._source_estimator(source)
            hit_rate = estimator.estimate(self._minimum)
            base = self.base_rate_excluding(source)
            against = "the rest of this bot's follows"
            name = source
        else:
            estimator = self._detector_estimator(source, detector)
            hit_rate = estimator.estimate(self._minimum)
            base = self._source_estimator(source).estimate(self._minimum)
            against = f"the {source} source overall"
            name = f"{source}:{detector}"

        if base.value <= 0:
            ratio = self._maximum_weight if hit_rate.value > 0 else 1.0
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} follows while {against} "
                f"has produced nothing"
            )
        else:
            ratio = hit_rate.value / base.value
            provenance = (
                f"{hit_rate.value:.1%} over {hit_rate.observations} follows against the "
                f"{base.value:.1%} {against} achieves"
                + ("" if hit_rate.is_fitted else ", still pulled toward the prior")
            )

        weight = min(self._maximum_weight, max(self._minimum_weight, ratio))
        if weight == self._minimum_weight:
            self.standing.at_the_floor += 1
        self.standing.highest_weight = max(self.standing.highest_weight, weight)
        self.standing.weights_published += 1
        self.standing.by_source[name] = weight

        return SetupWeight(
            bot=BOT,
            detector=name,
            weight=weight,
            hit_rate=hit_rate,
            trades_judged=hit_rate.observations,
            reason=(
                f"{name} weighted {weight:.2f}: {provenance}"
                + (
                    f"; held at the {self._minimum_weight:.2f} floor so it keeps producing the "
                    f"evidence that could clear it"
                    if weight == self._minimum_weight
                    else ""
                )
            ),
            learned_at_ns=self._now_ns(),
        )

    def all_weights(self) -> tuple[SetupWeight, ...]:
        weights = [self.weight_for(source) for source in sorted(self._by_source)]
        weights.extend(
            self.weight_for(source, detector)
            for source, detector in sorted(self._by_detector)
        )
        return tuple(weights)

    def _source_estimator(self, source: str) -> RateEstimator:
        estimator = self._by_source.get(source)
        if estimator is None:
            estimator = self._new_estimator()
            self._by_source[source] = estimator
        return estimator

    def _detector_estimator(self, source: str, detector: str) -> RateEstimator:
        key = (source, detector)
        estimator = self._by_detector.get(key)
        if estimator is None:
            estimator = self._new_estimator()
            self._by_detector[key] = estimator
        return estimator

    def _new_estimator(self) -> RateEstimator:
        return RateEstimator(
            prior=self._prior_hit_rate, prior_weight=self._prior_weight,
            half_life_observations=self._half_life,
        )


def describe_setup_weights(learner: TailSetupWeightLearner) -> dict:
    return {
        "part_id": PART_ID,
        "sources_tracked": learner.standing.sources_tracked,
        "detectors_tracked": learner.standing.detectors_tracked,
        "follows_learned_from": learner.standing.follows_learned_from,
        "weights_published": learner.standing.weights_published,
        "at_the_floor": learner.standing.at_the_floor,
        "highest_weight": learner.standing.highest_weight,
        "weights": dict(sorted(learner.standing.by_source.items())),
    }


def run_tail_setup_weight_learner(
    learner: TailSetupWeightLearner, control_socket, read_scorecard, publish_weights,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_scorecard(learner)
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
    """The one entry point every part carries (T-1)."""
    import time as _time

    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    publish_weights = context.bus.publisher_for("tail-setup-weight")
    learner = TailSetupWeightLearner(
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
        minimum_weight=context.number("tail_minimum_setup_weight"),
        maximum_weight=context.number("bull_setup_weight_maximum"),
    )
    last_publish = [float("-inf")]

    def tick() -> None:
        for scorecard in scorecards.payloads():
            if getattr(scorecard, "bot", None) == BOT:
                learner.observe_scorecard(scorecard)
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
