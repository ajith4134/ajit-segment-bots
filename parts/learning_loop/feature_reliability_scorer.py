"""feature-reliability-scorer: which features actually preceded profit, and which just correlated.

Feature attribution says what a model weighted. This says whether weighting it was
right -- and the two come apart constantly, because a model will happily weight a
feature that correlates with the outcome for a reason that will not repeat.

The measurement is per feature and per regime, and it separates three things a
single "importance" number conflates:

- **Directional reliability.** When this feature was extreme, did price go the
  way the model expected? That is what a conviction model needs and it is the
  cheapest to measure.
- **Stability across regimes.** A feature reliable in a trend and useless in a
  chop is not an unreliable feature -- it is a regime-conditional one, and the fix
  is to condition on regime rather than to drop it.
- **Availability.** A feature that is reliable and missing half the time is worth
  less than one slightly weaker that is always there, and a scorer that ignored
  availability would keep recommending features the builder cannot produce.

**A feature is scored against what the model would have done without it.** Raw
correlation with profit is dominated by whatever the market did; the useful
question is whether the feature changed the decision for the better.

**A feature with too few extreme observations is unmeasured.** Most of a
feature's information is in its tails, and a scorer that averaged over the middle
would find every feature mildly useful.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "feature-reliability-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="feature-reliability-scorer",
    consumes=("trade-episode", "vol-feature-set", "feature-attribution"),
    produces=("feature-reliability", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RELIABLE = "reliable"
UNRELIABLE = "unreliable"
REGIME_CONDITIONAL = "reliable-in-some-regimes-and-not-others"
NOT_MEASURED = "too-few-extreme-observations-to-say"


@dataclass(frozen=True)
class FeatureReliability:
    """Whether weighting one feature has been right, per regime."""

    feature: str
    regime: str
    state: str
    directional_reliability: Estimate
    availability: float
    lift_over_no_feature: float | None
    extreme_observations: int
    regimes_where_it_works: tuple
    reason: str
    scored_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state != NOT_MEASURED

    @property
    def is_worth_weighting(self) -> bool:
        return self.state in (RELIABLE, REGIME_CONDITIONAL)


@dataclass
class ScorerStanding:
    observations: int = 0
    extreme_observations: int = 0
    features_tracked: int = 0
    scores_produced: int = 0
    reliable: int = 0
    regime_conditional: int = 0
    unmeasured: int = 0
    by_feature: dict = field(default_factory=dict)


class FeatureReliabilityScorer:
    """Scores each feature on whether weighting it was right, not on how it correlated."""

    def __init__(
        self,
        extreme_deviation: float,
        minimum_extreme_observations: int,
        reliability_threshold: float,
        prior_reliability: float,
        prior_weight: float,
        half_life_observations: float,
        moments_half_life: float,
        minimum_moment_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if extreme_deviation <= 0:
            raise ValueError(
                "most of a feature's information is in its tails; without a threshold this "
                "averages over the middle and finds every feature mildly useful"
            )
        if not 0.0 < reliability_threshold < 1.0:
            raise ValueError("reliability is a hit rate and its bar must be inside (0, 1)")
        self._extreme = extreme_deviation
        self._minimum_extreme = minimum_extreme_observations
        self._threshold = reliability_threshold
        self._prior_reliability = prior_reliability
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._moments_half_life = moments_half_life
        self._minimum_moments = minimum_moment_observations
        self._now_ns = now_ns
        self._moments: dict[str, RunningMoments] = {}
        self._reliability: dict[tuple[str, str], RateEstimator] = {}
        self._extreme_counts: dict[tuple[str, str], int] = {}
        self._seen: dict[str, int] = {}
        self._vectors: int = 0
        self._with_feature: dict[tuple[str, str], list] = {}
        self._without_feature: dict[tuple[str, str], list] = {}
        self.standing = ScorerStanding()

    def observe_vector(self, features: dict) -> None:
        """One feature vector, so availability can be measured against what was built."""
        self._vectors += 1
        for name, value in features.items():
            self._moment_for(name).observe(value)
            self._seen[name] = self._seen.get(name, 0) + 1
        self.standing.features_tracked = len(self._moments)

    def observe_outcome(
        self, feature: str, value: float, regime: str, the_model_was_right: bool,
        realised_with: float | None = None, realised_without: float | None = None,
    ) -> None:
        """One resolved trade, counted only when this feature was extreme.

        The tails are where a feature's information is; the middle would make
        every feature look mildly useful.
        """
        self.standing.observations += 1
        moments = self._moments.get(feature)
        standardised = (
            None if moments is None else moments.standardise(value, self._minimum_moments)
        )
        if standardised is None or abs(standardised) < self._extreme:
            return

        self.standing.extreme_observations += 1
        key = (feature, regime)
        self._reliability_for(key).observe(the_model_was_right)
        self._extreme_counts[key] = self._extreme_counts.get(key, 0) + 1
        self.standing.by_feature[feature] = self.standing.by_feature.get(feature, 0) + 1

        # Against what the model would have done without it: raw correlation
        # with profit is dominated by whatever the market did.
        if realised_with is not None:
            self._with_feature.setdefault(key, []).append(realised_with)
        if realised_without is not None:
            self._without_feature.setdefault(key, []).append(realised_without)

    def availability(self, feature: str) -> float:
        """How often the builder actually produced this feature."""
        if self._vectors == 0:
            return 0.0
        return self._seen.get(feature, 0) / self._vectors

    def lift(self, feature: str, regime: str) -> float | None:
        key = (feature, regime)
        with_it = self._with_feature.get(key)
        without_it = self._without_feature.get(key)
        if not with_it or not without_it:
            return None
        return sum(with_it) / len(with_it) - sum(without_it) / len(without_it)

    def score(self, feature: str, regime: str) -> FeatureReliability:
        self.standing.scores_produced += 1
        key = (feature, regime)
        extreme = self._extreme_counts.get(key, 0)
        reliability = self._reliability_for(key).estimate(self._minimum_extreme)
        availability = self.availability(feature)
        lift = self.lift(feature, regime)

        working = tuple(
            sorted(
                kept_regime
                for (kept_feature, kept_regime), count in self._extreme_counts.items()
                if kept_feature == feature
                and count >= self._minimum_extreme
                and self._reliability_for((kept_feature, kept_regime))
                .estimate(self._minimum_extreme)
                .value
                >= self._threshold
            )
        )

        if extreme < self._minimum_extreme:
            self.standing.unmeasured += 1
            state = NOT_MEASURED
            reason = (
                f"{extreme} extreme observation(s) of the {self._minimum_extreme} needed. Most "
                f"of a feature's information is in its tails, and averaging over the middle "
                f"finds every feature mildly useful"
            )
        elif reliability.value >= self._threshold:
            state = RELIABLE
            self.standing.reliable += 1
            reason = (
                f"when {feature} was {self._extreme:.1f}+ deviations out in {regime}, the "
                f"model was right {reliability.value:.0%} of the time over {extreme} "
                f"observation(s)"
                + (
                    f", and trades that used it made {lift:+.4%} more per trade than those "
                    f"that did not"
                    if lift is not None
                    else ""
                )
                + f". Available in {availability:.0%} of vectors"
            )
        elif len(working) > 0:
            state = REGIME_CONDITIONAL
            self.standing.regime_conditional += 1
            reason = (
                f"{feature} is only {reliability.value:.0%} reliable in {regime} but works in "
                f"{', '.join(working)}. That is not an unreliable feature, it is a "
                f"regime-conditional one, and the fix is to condition on regime rather than "
                f"to drop it"
            )
        else:
            state = UNRELIABLE
            reason = (
                f"{reliability.value:.0%} reliable over {extreme} extreme observation(s) in "
                f"{regime}, below the {self._threshold:.0%} bar, and it works in no regime "
                f"measured so far"
            )

        return FeatureReliability(
            feature=feature,
            regime=regime,
            state=state,
            directional_reliability=reliability,
            availability=availability,
            lift_over_no_feature=lift,
            extreme_observations=extreme,
            regimes_where_it_works=working,
            reason=reason,
            scored_at_ns=self._now_ns(),
        )

    def score_all(self) -> tuple:
        return tuple(
            self.score(feature, regime) for feature, regime in sorted(self._extreme_counts)
        )

    def _moment_for(self, feature: str) -> RunningMoments:
        moments = self._moments.get(feature)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._moments_half_life)
            self._moments[feature] = moments
        return moments

    def _reliability_for(self, key) -> RateEstimator:
        estimator = self._reliability.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_reliability, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._reliability[key] = estimator
        return estimator


def describe_feature_reliability(scorer: FeatureReliabilityScorer) -> dict:
    return {
        "part_id": PART_ID,
        "observations": scorer.standing.observations,
        "extreme_observations": scorer.standing.extreme_observations,
        "features_tracked": scorer.standing.features_tracked,
        "scores_produced": scorer.standing.scores_produced,
        "reliable": scorer.standing.reliable,
        "regime_conditional": scorer.standing.regime_conditional,
        "unmeasured": scorer.standing.unmeasured,
        "by_feature": dict(sorted(scorer.standing.by_feature.items())),
        "availability": {
            feature: scorer.availability(feature) for feature in sorted(scorer._moments)
        },
    }


def run_feature_reliability_scorer(
    scorer: FeatureReliabilityScorer, control_socket, read_episodes, publish_reliability,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_episodes(scorer)
        publish_reliability(scorer.score_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
