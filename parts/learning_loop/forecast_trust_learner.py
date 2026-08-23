"""forecast-trust-learner: how much each forecaster has earned the right to be heard.

The forecast scorer says how accurate a forecaster has been. This says how much
that accuracy is worth to a decision, and the two differ because accuracy is
measured against the forecast's own claim and trust must be measured against what
using it produced.

- **Trust is conditioned on the situation.** A forecaster accurate in a trend and
  useless in a chop has two trust numbers, and one over both imports the trend's
  accuracy into the chop -- which is exactly when the decision it feeds is most
  expensive.
- **Directional accuracy is not enough.** A forecaster right on direction 60% of
  the time whose misses are twice the size of its hits is not trustworthy, so
  trust is learned from what following it actually produced.
- **Trust decays.** A forecaster's record from a regime that ended describes a
  model that no longer exists, and a trust number without decay keeps authorising
  it.
- **A forecaster with no record has zero trust, not average trust.** Average is
  the number that lets an untested model into every decision.

**Trust is bounded above.** No forecaster becomes the decision: the ensemble
weights by trust, and an unbounded weight would let one member's failure be the
system's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forecast-trust-learner"

PART_DECLARATION = PartDeclaration(
    part_id="forecast-trust-learner",
    consumes=("forecast-accuracy", "trade-episode"),
    produces=("forecast-trust", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TRUSTED = "trusted"
NOT_TRUSTED = "measured-and-not-trusted"
NOT_MEASURED = "no-record-in-this-situation"


@dataclass(frozen=True)
class ForecastTrust:
    """How much one forecaster's view should count, in one situation."""

    forecaster: str
    situation: str
    state: str
    trust: float
    directional_accuracy: Estimate
    realised_per_follow: float | None
    follows_recorded: int
    was_capped: bool
    reason: str
    learned_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state != NOT_MEASURED

    @property
    def is_trusted(self) -> bool:
        return self.state == TRUSTED


@dataclass
class LearnerStanding:
    follows_recorded: int = 0
    forecasters_tracked: int = 0
    situations_tracked: int = 0
    trusted: int = 0
    untrusted: int = 0
    unmeasured: int = 0
    capped: int = 0
    highest_trust_seen: float = 0.0


class ForecastTrustLearner:
    """Learns what following each forecaster has produced, per situation."""

    def __init__(
        self,
        minimum_follows: int,
        trust_threshold: float,
        maximum_trust: float,
        prior_accuracy: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < trust_threshold < 1.0:
            raise ValueError("the trust bar is a hit rate and must be inside (0, 1)")
        if not 0.0 < maximum_trust <= 1.0:
            raise ValueError(
                "no forecaster becomes the decision; an unbounded weight would let one "
                "member's failure be the system's"
            )
        self._minimum = minimum_follows
        self._threshold = trust_threshold
        self._maximum = maximum_trust
        self._prior_accuracy = prior_accuracy
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._accuracy: dict[tuple[str, str], RateEstimator] = {}
        self._realised: dict[tuple[str, str], list] = {}
        self.standing = LearnerStanding()

    def observe_follow(
        self, forecaster: str, situation: str, direction_was_right: bool, realised: float
    ) -> None:
        """One decision that followed this forecaster, and what it produced.

        Realised as well as direction, because a forecaster right 60% of the time
        whose misses are twice the size of its hits is not trustworthy.
        """
        key = (forecaster, situation)
        self._accuracy_for(key).observe(direction_was_right)
        self._realised.setdefault(key, []).append(realised)
        self.standing.follows_recorded += 1
        self.standing.forecasters_tracked = len({name for name, _ in self._accuracy})
        self.standing.situations_tracked = len({kept for _, kept in self._accuracy})

    def realised_per_follow(self, forecaster: str, situation: str) -> float | None:
        realised = self._realised.get((forecaster, situation))
        if not realised:
            return None
        return sum(realised) / len(realised)

    def trust_in(self, forecaster: str, situation: str) -> ForecastTrust:
        key = (forecaster, situation)
        accuracy = self._accuracy_for(key).estimate(self._minimum)
        realised = self.realised_per_follow(forecaster, situation)
        follows = len(self._realised.get(key, []))

        if not accuracy.is_fitted:
            # Zero, not average. Average is the number that lets an untested
            # model into every decision.
            self.standing.unmeasured += 1
            return self._trust(
                forecaster, situation, NOT_MEASURED, 0.0, accuracy, realised, follows, False,
                f"{follows} follow(s) of the {self._minimum} needed in {situation}. An "
                f"unmeasured forecaster has zero trust rather than average trust, because "
                f"average is what lets an untested model into every decision",
            )

        if accuracy.value < self._threshold or (realised is not None and realised <= 0):
            self.standing.untrusted += 1
            return self._trust(
                forecaster, situation, NOT_TRUSTED, 0.0, accuracy, realised, follows, False,
                f"{accuracy.value:.0%} directional accuracy over {follows} follow(s) in "
                f"{situation}"
                + (
                    f", producing {realised:+.4%} per follow"
                    if realised is not None
                    else ""
                )
                + f" -- below the {self._threshold:.0%} bar, or losing money while being "
                f"directionally right, which is the case a hit rate alone cannot see",
            )

        trust = accuracy.value
        capped = trust > self._maximum
        if capped:
            trust = self._maximum
            self.standing.capped += 1

        self.standing.trusted += 1
        self.standing.highest_trust_seen = max(self.standing.highest_trust_seen, trust)
        return self._trust(
            forecaster, situation, TRUSTED, trust, accuracy, realised, follows, capped,
            f"{accuracy.value:.0%} right over {follows} follow(s) in {situation}"
            + (f", producing {realised:+.4%} per follow" if realised is not None else "")
            + f", so trust is {trust:.2f}"
            + (
                f", capped at {self._maximum:.2f} so no forecaster becomes the decision"
                if capped
                else ""
            )
            + ". Conditioned on the situation, because a forecaster accurate in a trend and "
            "useless in a chop has two records and one over both would import the trend's "
            "accuracy into exactly the case where the decision is most expensive",
        )

    def all_trust(self) -> tuple:
        return tuple(
            self.trust_in(forecaster, situation)
            for forecaster, situation in sorted(self._accuracy)
        )

    def _accuracy_for(self, key) -> RateEstimator:
        estimator = self._accuracy.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_accuracy, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._accuracy[key] = estimator
        return estimator

    def _trust(
        self, forecaster, situation, state, trust, accuracy, realised, follows, capped, reason
    ) -> ForecastTrust:
        return ForecastTrust(
            forecaster=forecaster,
            situation=situation,
            state=state,
            trust=trust,
            directional_accuracy=accuracy,
            realised_per_follow=realised,
            follows_recorded=follows,
            was_capped=capped,
            reason=reason,
            learned_at_ns=self._now_ns(),
        )


def describe_forecast_trust(learner: ForecastTrustLearner) -> dict:
    return {
        "part_id": PART_ID,
        "follows_recorded": learner.standing.follows_recorded,
        "forecasters_tracked": learner.standing.forecasters_tracked,
        "situations_tracked": learner.standing.situations_tracked,
        "trusted": learner.standing.trusted,
        "untrusted": learner.standing.untrusted,
        "unmeasured": learner.standing.unmeasured,
        "capped_at_the_ceiling": learner.standing.capped,
        "highest_trust_seen": learner.standing.highest_trust_seen,
        "unmeasured_trust_value": 0.0,
    }


def run_forecast_trust_learner(
    learner: ForecastTrustLearner, control_socket, read_follows, publish_trust,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_follows(learner)
        publish_trust(learner.all_trust())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
