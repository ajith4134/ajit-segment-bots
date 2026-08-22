"""Rolling statistics over a bounded window, for parts that judge a price series.

Substrate, not a part. Several detectors need the same measurements and none may
import another (T-4), so they live here.

Bounded by construction: every window has a fixed length and drops what falls out
of it. A detector accumulating an unbounded series would grow without limit and
would also be fitting a market that no longer exists.

**Nothing here returns a number it cannot support.** A statistic asked for before
enough observations exist returns None, never zero and never a value computed
from two points. A z-score from three samples is not a small z-score; it is not a
z-score, and a detector treating it as one fires on noise.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RollingWindow:
    """The last N observations of one series, and what can be said about them."""

    length: int
    values: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.length < 2:
            raise ValueError("a window shorter than two observations describes nothing")
        self.values = deque(self.values, maxlen=self.length)

    def observe(self, value: float) -> None:
        self.values.append(float(value))

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def is_full(self) -> bool:
        return len(self.values) == self.length

    @property
    def latest(self) -> float | None:
        return self.values[-1] if self.values else None

    def mean(self, minimum_observations: int) -> float | None:
        if len(self.values) < minimum_observations:
            return None
        return sum(self.values) / len(self.values)

    def standard_deviation(self, minimum_observations: int) -> float | None:
        """The sample standard deviation, or None below the minimum.

        Sample rather than population: these are observations drawn from an
        ongoing process, not the whole of it, and the population form
        understates the spread on exactly the short windows detectors use.
        """
        if len(self.values) < max(2, minimum_observations):
            return None
        mean = sum(self.values) / len(self.values)
        variance = sum((value - mean) ** 2 for value in self.values) / (len(self.values) - 1)
        return math.sqrt(variance)

    def z_score(self, value: float, minimum_observations: int) -> float | None:
        """How many standard deviations `value` is from the window's mean.

        None when the window is too short, and None when the series has not
        moved at all -- a zero deviation makes every value infinitely unusual,
        which is arithmetic rather than information.
        """
        mean = self.mean(minimum_observations)
        deviation = self.standard_deviation(minimum_observations)
        if mean is None or deviation is None or deviation == 0:
            return None
        return (value - mean) / deviation

    def quantile(self, quantile: float, minimum_observations: int) -> float | None:
        """Nearest-rank, so the answer is a value that was actually observed."""
        if not 0.0 < quantile < 1.0:
            raise ValueError(f"quantile must be in (0, 1); got {quantile}")
        if len(self.values) < minimum_observations:
            return None
        ordered = sorted(self.values)
        index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
        return ordered[index]

    def returns(self) -> list[float]:
        """Fractional changes between consecutive observations."""
        series = list(self.values)
        return [
            (later - earlier) / earlier
            for earlier, later in zip(series, series[1:])
            if earlier != 0
        ]


def hurst_exponent(series: list[float], minimum_observations: int) -> float | None:
    """A rescaled-range estimate of whether a series trends or reverts.

    Above 0.5 the series persists -- moves tend to continue. Below 0.5 it
    reverts -- moves tend to be given back. Around 0.5 it is a random walk, and
    both a trend follower and a mean reverter will lose money on it slowly.

    Rescaled range rather than a variance ratio because it needs no assumption
    about the distribution, which crypto returns comprehensively violate.

    None below the minimum: the estimator is noisy on short series and a value
    that swings between 0.3 and 0.7 on the same data is worse than no value.
    """
    if len(series) < max(20, minimum_observations):
        return None

    changes = [
        (later - earlier) / earlier
        for earlier, later in zip(series, series[1:])
        if earlier != 0
    ]
    if len(changes) < 10:
        return None

    # Rescaled range across a few window sizes, then the slope of log R/S
    # against log n -- which is the Hurst exponent by definition.
    sizes = [size for size in (8, 16, 32, 64, 128) if size <= len(changes)]
    if len(sizes) < 2:
        return None

    points = []
    for size in sizes:
        rescaled = _rescaled_range(changes, size)
        if rescaled is not None and rescaled > 0:
            points.append((math.log(size), math.log(rescaled)))
    if len(points) < 2:
        return None

    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if variance == 0:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return covariance / variance


def _rescaled_range(changes: list[float], size: int) -> float | None:
    """The mean R/S statistic over non-overlapping chunks of one size."""
    chunks = [changes[start : start + size] for start in range(0, len(changes) - size + 1, size)]
    ratios = []
    for chunk in chunks:
        if len(chunk) < size:
            continue
        mean = sum(chunk) / len(chunk)
        deviations = []
        running = 0.0
        for value in chunk:
            running += value - mean
            deviations.append(running)
        spread = max(deviations) - min(deviations)
        variance = sum((value - mean) ** 2 for value in chunk) / (len(chunk) - 1)
        deviation = math.sqrt(variance)
        if deviation > 0:
            ratios.append(spread / deviation)
    return sum(ratios) / len(ratios) if ratios else None


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares slope and intercept, or None when x does not vary."""
    if len(points) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if variance == 0:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = covariance / variance
    return slope, mean_y - slope * mean_x


def correlation(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation, or None when either series is flat or too short."""
    if len(left) != len(right) or len(left) < 3:
        return None
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if spread_left == 0 or spread_right == 0:
        return None
    return covariance / (spread_left * spread_right)
