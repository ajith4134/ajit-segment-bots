"""Estimators a judging part fits from its own recorded outcomes (RL-060).

A part that judges must learn; a part that transports must not. The difference is
whether the part's answer could be wrong in a way the world reveals later. A
stream reader writing bytes to a tape cannot be wrong about them. A classifier
saying "this rejection was transient" can, and the venue tells it so within
seconds -- so it has no excuse for using a number somebody typed.

What lives here is the machinery, not the judgements. Each estimator:

- **starts unfitted and says so.** An estimator with no observations returns its
  prior and reports `is_fitted` false, so a caller can tell a learned answer from
  a starting assumption. This is Rule 8 applied to a model: no evidence renders
  as no evidence, never as a confident number.
- **is bounded by settings.** RL-061 makes settings hard bounds on what
  estimation may produce. An estimator that learned a 40-second timeout is
  clamped by the operator's ceiling, and the clamp is reported rather than
  hidden -- learning something out of bounds is a fact worth seeing.
- **forgets.** Markets and venues change, so every estimator here weights recent
  observations more heavily than old ones. An estimator with infinite memory is
  fitting a venue that no longer exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Estimate:
    """One estimated number, and everything needed to distrust it."""

    value: float
    is_fitted: bool
    observations: int
    prior: float
    was_clamped: bool
    bound_low: float | None
    bound_high: float | None
    reason: str


class RateEstimator:
    """The probability of an outcome, learned from successes and failures.

    Beta-Bernoulli with a decaying count. The prior is what the operator believes
    before any evidence; each observation moves the estimate toward what actually
    happened, and old observations fade so a venue that used to reject everything
    stops being held against it forever.

    Used for questions of the form "how often does this work" -- whether a
    resubmitted order fills, whether a repriced limit gets taken.
    """

    def __init__(self, prior: float, prior_weight: float, half_life_observations: float) -> None:
        if not 0.0 <= prior <= 1.0:
            raise ValueError(f"a prior probability must be in [0, 1]; got {prior}")
        if prior_weight <= 0:
            raise ValueError("prior_weight must be positive: it is how many observations the prior is worth")
        if half_life_observations <= 0:
            raise ValueError("half_life_observations must be positive")
        self._prior = prior
        self._prior_weight = prior_weight
        # The decay applied to accumulated evidence on each new observation, set
        # so evidence loses half its weight after `half_life_observations`.
        self._decay = math.pow(0.5, 1.0 / half_life_observations)
        self._successes = 0.0
        self._trials = 0.0
        self.observations = 0

    def observe(self, succeeded: bool) -> None:
        self._successes = self._successes * self._decay + (1.0 if succeeded else 0.0)
        self._trials = self._trials * self._decay + 1.0
        self.observations += 1

    def estimate(self, minimum_observations: int) -> Estimate:
        """The learned rate, or the prior when there is not enough evidence yet."""
        if self.observations < minimum_observations:
            return Estimate(
                value=self._prior,
                is_fitted=False,
                observations=self.observations,
                prior=self._prior,
                was_clamped=False,
                bound_low=0.0,
                bound_high=1.0,
                reason=f"{self.observations} of {minimum_observations} observations needed",
            )
        weighted = (self._successes + self._prior * self._prior_weight) / (
            self._trials + self._prior_weight
        )
        return Estimate(
            value=weighted,
            is_fitted=True,
            observations=self.observations,
            prior=self._prior,
            was_clamped=False,
            bound_low=0.0,
            bound_high=1.0,
            reason=f"fitted from {self.observations} observations, recent ones weighted more",
        )


class QuantileEstimator:
    """A quantile of an observed distribution, kept over a bounded window.

    Used for questions of the form "how long does this usually take" or "how far
    does this usually move" -- an order's time to fill, a symbol's ordinary gap
    between one candle's close and the next one's open.

    A quantile rather than a mean, because these distributions have long tails
    and the number that matters is "unusual", not "typical". A mean fill time on
    a venue where one order in fifty hangs for a minute is a number that
    describes no order at all.
    """

    def __init__(self, window: int, prior: float) -> None:
        if window < 1:
            raise ValueError("window must hold at least one observation")
        self._window = window
        self._prior = prior
        self._samples: list[float] = []

    @property
    def observations(self) -> int:
        return len(self._samples)

    def observe(self, value: float) -> None:
        self._samples.append(float(value))
        del self._samples[: max(0, len(self._samples) - self._window)]

    def estimate(
        self,
        quantile: float,
        minimum_observations: int,
        bound_low: float | None = None,
        bound_high: float | None = None,
    ) -> Estimate:
        """The quantile of what has been seen, clamped to the operator's bounds."""
        if not 0.0 < quantile < 1.0:
            raise ValueError(f"quantile must be in (0, 1); got {quantile}")
        if len(self._samples) < minimum_observations:
            value, fitted, reason = (
                self._prior,
                False,
                f"{len(self._samples)} of {minimum_observations} observations needed",
            )
        else:
            ordered = sorted(self._samples)
            # Nearest-rank, so the answer is always a value that was actually
            # observed rather than an interpolation between two that were not.
            index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
            value = ordered[index]
            fitted = True
            reason = f"the {quantile:.0%} quantile of {len(ordered)} observations"

        clamped = value
        if bound_low is not None:
            clamped = max(clamped, bound_low)
        if bound_high is not None:
            clamped = min(clamped, bound_high)
        was_clamped = clamped != value
        if was_clamped:
            reason += f"; clamped from {value:g} to the operator's bound"
        return Estimate(
            value=clamped,
            is_fitted=fitted,
            observations=len(self._samples),
            prior=self._prior,
            was_clamped=was_clamped,
            bound_low=bound_low,
            bound_high=bound_high,
            reason=reason,
        )


@dataclass
class OutcomeCounter:
    """How often each labelled outcome has been seen, with decay.

    The evidence behind a classifier that learns which venue messages mean which
    thing. Kept separate from the classifier itself so that what it learned can
    be inspected, journalled and argued with.
    """

    half_life_observations: float
    counts: dict[str, float] = field(default_factory=dict)
    total_observations: int = 0

    def observe(self, label: str) -> None:
        decay = math.pow(0.5, 1.0 / self.half_life_observations)
        for key in self.counts:
            self.counts[key] *= decay
        self.counts[label] = self.counts.get(label, 0.0) + 1.0
        self.total_observations += 1

    def share_of(self, label: str) -> float:
        total = sum(self.counts.values())
        return (self.counts.get(label, 0.0) / total) if total else 0.0

    def most_common(self) -> str | None:
        if not self.counts:
            return None
        return max(self.counts.items(), key=lambda item: item[1])[0]
