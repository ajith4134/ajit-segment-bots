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
    # The key this detector's own calibrator is segmented by, stated by the claim
    # rather than implied by whichever string the detector happened to pass.
    #
    # `SignalCalibrator._estimator_for(detector, regime)` names its second slot
    # `regime`, and three of the nine detectors correctly put something else in
    # it: `whale-flow-detector` segments by direction, `universal-symbol-sweeper`
    # by watch condition, and `sentiment-shift-detector` compares its leading
    # record against its contrarian one. So an outcome cannot be routed back by
    # regime, and until this field existed the key was written down nowhere at
    # all -- which is one of the reasons no outcome ever reached any of the nine
    # (docs/proposals/nine-detectors-that-never-learn-whether-they-were-right.md).
    calibration_key: str = ""

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


def settle_claims_from(labels, detector, part_id: str) -> int:
    """Hand a detector every settled claim of its own. Returns how many it took.

    The other half of `SignalCalibrator`. Nine scanner detectors defined
    `observe_outcome` and nothing called any of them: none declared an input
    carrying an outcome, so no `start_part` had anything to call it with. All
    nine reported `outcomes_learned: 0`, `EntryCandidate.confidence` was the
    prior on every candidate ever raised, and `detector_hit_rate` was missing on
    100% of every feature vector either bot had ever built
    (docs/proposals/nine-detectors-that-never-learn-whether-they-were-right.md).

    Here rather than nine times over, because the reading is identical in all
    nine and a detector copying it is a detector made cleverer rather than the
    vocabulary made to say more (T-6). It takes the detector as an argument and
    names no part: what it knows is the shape of a claim and the shape of a
    label, which is what `runtime/` is for.

    Three filters, and each one is load-bearing:

    * **the label names this detector.** `training-label` is one wire carrying
      every detector's record; a part learning from another's would be learning
      about a claim it never made.
    * **the label carries a calibration key.** `label-builder` publishes on the
      same wire, from a closed trade after costs, and sets `THE_SETUP_WAS_RIGHT`
      as well -- but `SignalCalibrator`'s own docstring rules that out: *"not
      whether a trade made money, which depends on sizing, stops and timing that
      this detector had no part in."* A trade sized badly or stopped early is not
      evidence about the setup. `label-builder` has no candidate and so leaves
      the key empty, which makes the filter say what it means rather than lean on
      `claimed_at_ns` being non-zero.
    * **the component is present.** `label_for` returns None for a component the
      label does not speak to, and training on None as False would teach the
      calibrator that silence is a wrong call.
    """
    from runtime.learning_types import THE_SETUP_WAS_RIGHT

    settled = 0
    for label in labels:
        if getattr(label, "detector", "") != part_id:
            continue
        calibration_key = getattr(label, "calibration_key", "")
        if not calibration_key:
            continue
        was_right = label.label_for(THE_SETUP_WAS_RIGHT)
        if was_right is None:
            continue
        detector.observe_outcome(calibration_key, bool(was_right))
        settled += 1
    return settled


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
    calibration_key: str,
    now_ns=time.time_ns,
) -> EntryCandidate:
    """One candidate, with its strength and its calibrated confidence kept apart.

    Apart deliberately: strength says how unusual the observation is and
    confidence says how often that has meant anything. Multiplying them into one
    score would hide which of the two a weak candidate is weak on.
    """
    if direction not in (LONG, SHORT):
        raise ValueError(f"{direction!r} is not a direction a candidate can point in")
    if not calibration_key:
        # Required, not defaulted. A claim that does not say what it should be
        # scored against cannot be scored, and defaulting it to the regime would
        # silently mis-key the three detectors that do not calibrate on one --
        # their records would be written under a key nothing ever reads back.
        raise ValueError(
            f"{detector} raised a candidate with no calibration key, so the outcome "
            f"could never be routed back to the estimator that made the claim"
        )
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
        calibration_key=calibration_key,
        reason=reason,
        detected_at_ns=now_ns(),
    )
