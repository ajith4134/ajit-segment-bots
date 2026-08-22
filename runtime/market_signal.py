"""`entry-candidate`: a detector's claim that something is worth looking at.

Eight scanner parts produce these and none may import another (T-4), so the shape
lives here.

A candidate is **not** an instruction to trade. It is one detector saying "this
looks like the setup I watch for", and the bots and the brain decide what, if
anything, follows. Keeping that distinction in the type is what stops a detector
from quietly becoming a strategy.

Every candidate carries a **calibrated confidence** (RL-060). A detector's raw
signal strength -- how many standard deviations, how large a jump -- says how
unusual the observation is, not how often it is followed by the move the detector
expects. Only the record of its own past calls can say that, and a detector that
reported strength as though it were probability would be systematically
overconfident in exactly the regimes where it stops working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator

LONG = "long"
SHORT = "short"

# What a detector is claiming will happen, so a bot knows what it is being
# offered rather than inferring it from the detector's name.
REVERSION = "reversion"
CONTINUATION = "continuation"
BREAKOUT = "breakout"
UNWIND = "unwind"


@dataclass(frozen=True)
class EntryCandidate:
    """One detector's claim, with how unusual it is and how often it has been right."""

    detector: str
    venue_id: str
    symbol: str
    direction: str
    expectation: str
    signal_strength: float
    confidence: Estimate
    horizon_seconds: float
    evidence: dict
    reason: str
    detected_at_ns: int

    @property
    def is_calibrated(self) -> bool:
        """Whether the confidence is learned or still the detector's prior."""
        return self.confidence.is_fitted


@dataclass
class SignalCalibrator:
    """What actually happened after this detector's past calls (RL-060).

    Kept per detector and per regime, because a detector that works in a trending
    market and fails in a choppy one has two different hit rates, and one number
    over both describes neither.

    The outcome that trains it is whether the expected move happened within the
    horizon -- not whether a trade made money, which depends on sizing, stops and
    timing that this detector had no part in.
    """

    prior_hit_rate: float
    prior_weight: float
    half_life_observations: float
    minimum_observations: int
    _rates: dict = field(default_factory=dict)

    def observe_outcome(self, detector: str, regime: str, was_right: bool) -> None:
        self._estimator_for(detector, regime).observe(was_right)

    def confidence(self, detector: str, regime: str) -> Estimate:
        return self._estimator_for(detector, regime).estimate(self.minimum_observations)

    def _estimator_for(self, detector: str, regime: str) -> RateEstimator:
        key = (detector, regime)
        estimator = self._rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self.prior_hit_rate,
                prior_weight=self.prior_weight,
                half_life_observations=self.half_life_observations,
            )
            self._rates[key] = estimator
        return estimator

    def learned(self) -> dict:
        return {
            f"{detector}:{regime}": estimator.estimate(self.minimum_observations)
            for (detector, regime), estimator in sorted(self._rates.items())
        }


def make_candidate(
    detector: str,
    venue_id: str,
    symbol: str,
    direction: str,
    expectation: str,
    signal_strength: float,
    confidence: Estimate,
    horizon_seconds: float,
    evidence: dict,
    reason: str,
    now_ns=time.time_ns,
) -> EntryCandidate:
    """One candidate, with its strength and its calibrated confidence kept apart.

    Apart deliberately: strength says how unusual the observation is and
    confidence says how often that has meant anything. Multiplying them into one
    score would hide which of the two a weak candidate is weak on.
    """
    if direction not in (LONG, SHORT):
        raise ValueError(f"{direction!r} is not a direction a candidate can point in")
    return EntryCandidate(
        detector=detector,
        venue_id=venue_id,
        symbol=symbol,
        direction=direction,
        expectation=expectation,
        signal_strength=signal_strength,
        confidence=confidence,
        horizon_seconds=horizon_seconds,
        evidence=dict(evidence),
        reason=reason,
        detected_at_ns=now_ns(),
    )
