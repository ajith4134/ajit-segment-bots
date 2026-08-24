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
    """The last N observations of one series, and what can be said about them.

    **A window may be told what a hole in the series looks like.** Given
    `maximum_gap_seconds`, an observation arriving longer than that after the last
    one clears everything before it: the stretch of market the window described has
    a hole in it, and statistics computed across the hole are not conservative,
    they are wrong. The clearing is what makes them safe -- every statistic returns
    None again until the window refills, so a detector's existing
    minimum-observations guard is what declines to fire, with no detector needing
    to grow a gap check of its own.

    The failure this exists to prevent is specific. The bus drops rather than
    blocks, so under load a symbol's prints stop and later resume. To a window with
    no sense of time the first print after the hole sits directly beside the last
    one before it, and the difference between them reads as a move that happened in
    an instant -- which is precisely the shape a momentum-burst or a
    liquidation-cascade detector exists to fire on.

    Told nothing about time, a window keeps none: not every series is a price, and
    a window over model error or restart counts has no clock to be judged against.
    """

    length: int
    maximum_gap_seconds: float | None = None
    # Optional patience for a series that is ordinarily slow. When set alongside
    # maximum_gap_seconds, the bound a gap is judged against becomes
    # max(maximum_gap_seconds, gap_patience_multiple x this series' own p99
    # inter-arrival gap) -- estimated from the gaps this window has itself
    # observed, never from a table. The floor keeps the proven bound for a
    # liquid series; the multiple keeps an illiquid one, whose ordinary quiet is
    # minutes, from clearing its window on every pause and staying blind
    # forever. The 2026-08-23 measurement behind the floor found ordinary p99
    # gaps of 3.0s (median symbol) to 42.3s (worst) against a real outage of
    # ~1031s -- an outage clears any plausible bound either way.
    gap_patience_multiple: float | None = None
    values: deque = field(default_factory=deque)
    _last_observed_at_ns: int | None = None
    _last_observed_value: float | None = None
    _series_breaks: int = 0
    _redelivered_skipped: int = 0
    _recent_gaps_seconds: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.length < 2:
            raise ValueError("a window shorter than two observations describes nothing")
        if self.maximum_gap_seconds is not None and not self.maximum_gap_seconds > 0:
            raise ValueError(
                "the gap bound is a positive number of seconds, or None for a window with no "
                f"clock; got {self.maximum_gap_seconds!r}"
            )
        if self.gap_patience_multiple is not None:
            if self.maximum_gap_seconds is None:
                raise ValueError(
                    "gap patience multiplies a gap bound; without maximum_gap_seconds there "
                    "is nothing for it to be patient about"
                )
            if not self.gap_patience_multiple > 0:
                raise ValueError(
                    f"the gap patience is a positive multiple of this series' own p99 gap; "
                    f"got {self.gap_patience_multiple!r}"
                )
        self.values = deque(self.values, maxlen=self.length)
        # As many gaps as values: the p99 of the gaps should describe the same
        # stretch of series the window itself does.
        self._recent_gaps_seconds = deque(self._recent_gaps_seconds, maxlen=self.length)

    def observe(self, value: float, at_ns: int | None = None) -> None:
        if (
            at_ns is not None
            and at_ns == self._last_observed_at_ns
            and self._last_observed_value == float(value)
        ):
            # The same value at the same moment is the same fact re-delivered,
            # not a new observation. Since 2026-08-24 a price travels in a frame
            # published four times a second, so a symbol quiet between frames
            # arrives again with the same print time and price -- appended,
            # those repeats fill the window with a flat run the market never
            # traded. The value is part of the test on purpose: venues stamp at
            # millisecond precision, and two real prints in one millisecond are
            # two facts, not one.
            self._redelivered_skipped += 1
            return
        if self.maximum_gap_seconds is not None:
            if at_ns is None:
                raise ValueError(
                    "this window was given a gap bound, so every observation must carry when "
                    "it happened; a value with no time would assert a continuity nothing checked"
                )
            if self._last_observed_at_ns is not None:
                gap_seconds = (at_ns - self._last_observed_at_ns) / 1e9
                # The floor first: a gap inside it can never break the series,
                # and the p99 estimate is only worth computing past it.
                if gap_seconds > self.maximum_gap_seconds and gap_seconds > self._gap_bound_seconds():
                    self.values.clear()
                    self._series_breaks += 1
                self._recent_gaps_seconds.append(gap_seconds)
            self._last_observed_at_ns = at_ns
        elif at_ns is not None:
            self._last_observed_at_ns = at_ns
        self._last_observed_value = float(value)
        self.values.append(float(value))

    def _gap_bound_seconds(self) -> float:
        """What counts as a hole for this series, right now.

        The stated bound alone until this series has shown enough of its own
        rhythm to be measured against it -- an estimate from a handful of gaps
        would let one early pause set the patience.
        """
        if self.gap_patience_multiple is None:
            return self.maximum_gap_seconds
        gaps = self._recent_gaps_seconds
        if len(gaps) < max(2, self.length // 2):
            return self.maximum_gap_seconds
        ordered = sorted(gaps)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return max(self.maximum_gap_seconds, self.gap_patience_multiple * p99)

    @property
    def redelivered_skipped(self) -> int:
        """Observations carrying the same time as the last, dropped unappended."""
        return self._redelivered_skipped

    @property
    def series_breaks(self) -> int:
        """How many times a hole in the series emptied this window.

        Counted rather than merely acted on: a window clearing repeatedly is a feed
        that keeps stopping, and a detector that quietly never fires looks exactly
        like a market in which nothing is happening.
        """
        return self._series_breaks

    def seconds_since_last_observation(self, now_ns: int) -> float | None:
        """How long this series has been silent, or None if it never spoke."""
        if self._last_observed_at_ns is None:
            return None
        return (now_ns - self._last_observed_at_ns) / 1e9

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
