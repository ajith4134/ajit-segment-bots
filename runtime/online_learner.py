"""Online learning for parts that judge: a model that trains as the market runs.

Substrate, not a part. Three bots and several brain parts need the same learner
and none may import another (T-4), so it lives here.

Everything here is **online**: it learns from one observation at a time and never
holds the history it learned from. That is a requirement rather than a style. A
part that accumulated its training set would grow without bound (T-3 says an off
part releases its memory, so a part that cannot bound its memory cannot be turned
off honestly), and it would be fitting a market that has already changed.

Three pieces, and they answer three different questions:

- **`RunningMoments`** -- what is normal for this feature? Features arrive on
  wildly different scales (a z-score near 1, a notional near 10^7), and a
  gradient step on raw values is dominated by whichever feature happens to be
  measured in large units. Standardising is not cosmetic; without it the model
  learns the units.
- **`OnlineLogisticModel`** -- given these features, how likely is the outcome?
  Logistic rather than a tree ensemble because it updates from a single example
  in constant time and memory, which is what an always-on part can afford, and
  because its coefficients can be read: a part that cannot say why it believes
  something cannot be reviewed.
- **`ProbabilityCalibrator`** -- when this model says 0.8, how often is it right?
  A model's output is a score, not a frequency. Isotonic regression over bounded
  bins maps score to observed frequency without assuming the shape of the error,
  and reports **unfitted** until enough outcomes exist to say anything.

**Nothing here reports a fitted number before it has one.** Every estimate
carries `is_fitted`, and a caller that ignores it is trading its own prior with
the authority of a measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate

# A logistic saturates numerically well before it saturates mathematically. The
# bound keeps exp() from overflowing and keeps a confident model's gradient from
# vanishing to exactly zero, which would freeze a coefficient permanently.
LOGIT_BOUND = 35.0


def logistic(value: float) -> float:
    """The logistic function, bounded so an extreme score cannot overflow."""
    if value >= LOGIT_BOUND:
        return 1.0 - 1e-15
    if value <= -LOGIT_BOUND:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-value))


def log_odds(probability: float) -> float:
    """The inverse of `logistic`, clamped away from the two impossible ends."""
    bounded = min(1.0 - 1e-12, max(1e-12, probability))
    return math.log(bounded / (1.0 - bounded))


@dataclass
class RunningMoments:
    """Mean and spread of one feature, with older observations decaying out.

    Exponentially weighted rather than a plain running mean: a feature's normal
    range shifts with the market, and a mean over all history describes a
    volatility regime that ended months ago.
    """

    half_life_observations: float
    count: int = 0
    _weight: float = 0.0
    _mean: float = 0.0
    _sum_squares: float = 0.0

    def __post_init__(self) -> None:
        if self.half_life_observations <= 0:
            raise ValueError("a half-life of zero or less would discard every observation")
        self._decay = 0.5 ** (1.0 / self.half_life_observations)

    def observe(self, value: float) -> None:
        value = float(value)
        self.count += 1
        self._weight = self._weight * self._decay + 1.0
        self._sum_squares *= self._decay
        delta = value - self._mean
        self._mean += delta / self._weight
        self._sum_squares += delta * (value - self._mean)

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def deviation(self) -> float:
        """The weighted standard deviation, or 0.0 when the feature has not moved."""
        if self._weight <= 1.0:
            return 0.0
        return math.sqrt(max(0.0, self._sum_squares / self._weight))

    def standardise(self, value: float, minimum_observations: int) -> float | None:
        """`value` in standard deviations, or None when that cannot be said.

        None rather than the raw value: a feature standardised on two
        observations, or one that has not moved, contributes noise scaled up to
        look like signal.
        """
        if self.count < minimum_observations:
            return None
        deviation = self.deviation
        if deviation <= 0.0:
            return None
        return (value - self._mean) / deviation


@dataclass(frozen=True)
class ModelBelief:
    """What the model thinks, and everything needed to argue with it."""

    probability: float
    score: float
    contributions: dict
    features_used: int
    features_unusable: tuple
    observations_trained_on: int
    is_fitted: bool
    reason: str

    @property
    def strongest_reason(self) -> tuple[str, float] | None:
        """The feature that moved the score furthest, in either direction."""
        if not self.contributions:
            return None
        name = max(self.contributions, key=lambda key: abs(self.contributions[key]))
        return name, self.contributions[name]


@dataclass
class OnlineLogisticModel:
    """Logistic regression trained one example at a time, with readable weights.

    The regularisation is applied as decay toward zero on every update rather
    than added to the gradient of the seen features only. A feature that stops
    appearing keeps its weight forever under the naive form, so a model would
    carry a coefficient learned in a regime that has ended.
    """

    learning_rate: float
    l2_regularisation: float
    feature_half_life_observations: float
    minimum_feature_observations: int
    minimum_training_observations: int
    _weights: dict = field(default_factory=dict)
    _moments: dict = field(default_factory=dict)
    _bias: float = 0.0
    _observations: int = 0
    _positives: int = 0

    def __post_init__(self) -> None:
        if not 0.0 < self.learning_rate <= 1.0:
            raise ValueError("a learning rate outside (0, 1] either does not learn or diverges")
        if self.l2_regularisation < 0.0:
            raise ValueError("negative regularisation grows weights without bound")

    @property
    def observations(self) -> int:
        return self._observations

    @property
    def is_fitted(self) -> bool:
        """Whether both classes have been seen often enough to have learned anything.

        Both classes, not just enough rows: a model trained on 500 wins and no
        losses has learned that everything wins.
        """
        losses = self._observations - self._positives
        return (
            self._observations >= self.minimum_training_observations
            and self._positives > 0
            and losses > 0
        )

    @property
    def weights(self) -> dict:
        """The learned coefficients, in standardised feature units."""
        return dict(self._weights)

    @property
    def bias(self) -> float:
        return self._bias

    def believe(self, features: dict) -> ModelBelief:
        """The model's probability for one feature vector, and what drove it."""
        contributions = {}
        unusable = []
        score = self._bias
        for name, value in sorted(features.items()):
            standardised = self._standardise(name, value)
            if standardised is None:
                unusable.append(name)
                continue
            contribution = self._weights.get(name, 0.0) * standardised
            contributions[name] = contribution
            score += contribution

        probability = logistic(score)
        if not self.is_fitted:
            reason = (
                f"{self._observations} outcomes trained on ({self._positives} of one class); "
                f"{self.minimum_training_observations} of each class are needed before this "
                f"probability is a measurement rather than a starting point"
            )
        else:
            reason = (
                f"{len(contributions)} feature(s) over {self._observations} trained outcomes "
                f"put the log-odds at {score:.3f}"
            )
            if unusable:
                reason += f"; {len(unusable)} feature(s) not yet standardisable and left out"

        return ModelBelief(
            probability=probability,
            score=score,
            contributions=contributions,
            features_used=len(contributions),
            features_unusable=tuple(unusable),
            observations_trained_on=self._observations,
            is_fitted=self.is_fitted,
            reason=reason,
        )

    def train(self, features: dict, outcome: bool, sample_weight: float = 1.0) -> float:
        """One gradient step. Returns the error it corrected, for the caller to record.

        The features are observed into their moments *before* the step, so a
        feature seen for the first time is standardisable on the example that
        introduced it rather than one example later.
        """
        if sample_weight <= 0.0:
            raise ValueError("a non-positive sample weight would unlearn or ignore the example")

        for name, value in features.items():
            self._moment_for(name).observe(value)

        standardised = {}
        for name, value in features.items():
            value = self._standardise(name, value)
            if value is not None:
                standardised[name] = value

        score = self._bias + sum(
            self._weights.get(name, 0.0) * value for name, value in standardised.items()
        )
        predicted = logistic(score)
        error = (1.0 if outcome else 0.0) - predicted
        step = self.learning_rate * error * sample_weight

        # Decay every weight, then step the ones this example spoke to. Decaying
        # only the seen features would let a coefficient from a dead regime sit
        # untouched forever.
        if self.l2_regularisation:
            decay = 1.0 - self.learning_rate * self.l2_regularisation
            for name in self._weights:
                self._weights[name] *= decay

        for name, value in standardised.items():
            self._weights[name] = self._weights.get(name, 0.0) + step * value
        self._bias += step

        self._observations += 1
        if outcome:
            self._positives += 1
        return error

    def _moment_for(self, name: str) -> RunningMoments:
        moments = self._moments.get(name)
        if moments is None:
            moments = RunningMoments(half_life_observations=self.feature_half_life_observations)
            self._moments[name] = moments
        return moments

    def _standardise(self, name: str, value: float) -> float | None:
        moments = self._moments.get(name)
        if moments is None:
            return None
        return moments.standardise(value, self.minimum_feature_observations)

    def describe(self) -> dict:
        return {
            "observations": self._observations,
            "positives": self._positives,
            "is_fitted": self.is_fitted,
            "bias": self._bias,
            "weights": dict(sorted(self._weights.items())),
            "features_tracked": len(self._moments),
        }


@dataclass
class ProbabilityCalibrator:
    """What a model's stated probability has actually meant (RL-060).

    A model that says 0.8 and is right half the time is not broken -- it is
    uncalibrated, and the two need different fixes. Isotonic regression over
    bounded bins learns the mapping from stated to observed without assuming its
    shape, which Platt scaling does assume and which a model trained through a
    regime change violates.

    Pool-adjacent-violators enforces the one thing that must hold: a higher
    stated probability may not map to a lower observed frequency. Without it the
    calibration is noise fitted per bin, and it will happily invert the model.
    """

    bin_count: int
    minimum_observations: int
    half_life_observations: float
    _bins: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.bin_count < 2:
            raise ValueError("a single bin cannot express a mapping")
        self._decay = 0.5 ** (1.0 / self.half_life_observations)
        self._bins = [[0.0, 0.0] for _ in range(self.bin_count)]

    def observe_outcome(self, stated_probability: float, outcome: bool) -> None:
        index = self._index_of(stated_probability)
        for bin_ in self._bins:
            bin_[0] *= self._decay
            bin_[1] *= self._decay
        self._bins[index][0] += 1.0
        if outcome:
            self._bins[index][1] += 1.0

    @property
    def observations(self) -> float:
        return sum(count for count, _ in self._bins)

    @property
    def is_fitted(self) -> bool:
        return self.observations >= self.minimum_observations

    def calibrate(self, stated_probability: float) -> Estimate:
        """The frequency this stated probability has actually been followed by."""
        if not self.is_fitted:
            return Estimate(
                value=stated_probability,
                observations=int(self.observations),
                is_fitted=False,
                reason=(
                    f"{self.observations:.0f} outcomes of the {self.minimum_observations} "
                    f"needed; until then the model's own number is passed through unchanged "
                    f"and must not be read as a measured frequency"
                ),
            )

        fitted = self._monotone_frequencies()
        index = self._index_of(stated_probability)
        value = fitted[index]
        count = self._bins[index][0]
        return Estimate(
            value=value,
            observations=int(self.observations),
            is_fitted=True,
            reason=(
                f"stated {stated_probability:.2f} has been followed by the expected outcome "
                f"{value:.0%} of the time over {count:.0f} weighted observations in that band"
            ),
        )

    def reliability(self) -> tuple:
        """Stated band against observed frequency -- what a reliability diagram plots."""
        fitted = self._monotone_frequencies()
        width = 1.0 / self.bin_count
        return tuple(
            {
                "band": (index * width, (index + 1) * width),
                "observations": self._bins[index][0],
                "observed_frequency": fitted[index],
            }
            for index in range(self.bin_count)
        )

    def _index_of(self, probability: float) -> int:
        bounded = min(1.0, max(0.0, probability))
        return min(self.bin_count - 1, int(bounded * self.bin_count))

    def _monotone_frequencies(self) -> list:
        """Pool-adjacent-violators: the closest non-decreasing fit to the bins.

        Empty bins are carried by their neighbours rather than counted as zero,
        because a band nothing landed in says nothing about that band.
        """
        blocks = [
            [count, hits, index, index]
            for index, (count, hits) in enumerate(self._bins)
            if count > 0
        ]
        if not blocks:
            return [0.5] * self.bin_count

        merged = True
        while merged:
            merged = False
            for position in range(len(blocks) - 1):
                left, right = blocks[position], blocks[position + 1]
                if left[1] / left[0] > right[1] / right[0]:
                    blocks[position : position + 2] = [
                        [left[0] + right[0], left[1] + right[1], left[2], right[3]]
                    ]
                    merged = True
                    break

        frequencies = [None] * self.bin_count
        for count, hits, first, last in blocks:
            for index in range(first, last + 1):
                frequencies[index] = hits / count

        # Carry the nearest fitted value into bands nothing landed in.
        last_seen = None
        for index in range(self.bin_count):
            if frequencies[index] is None:
                frequencies[index] = last_seen
            else:
                last_seen = frequencies[index]
        next_seen = None
        for index in reversed(range(self.bin_count)):
            if frequencies[index] is None:
                frequencies[index] = next_seen
            else:
                next_seen = frequencies[index]
        return [0.5 if value is None else value for value in frequencies]

    def describe(self) -> dict:
        return {
            "observations": self.observations,
            "is_fitted": self.is_fitted,
            "bins": self.bin_count,
            "reliability": self.reliability(),
        }
