"""bull-conviction-calibrator: what this bot's 0.8 has actually meant.

A model's output is a score, not a frequency. A logistic trained online is
routinely overconfident at its extremes -- the region where the bot sizes largest
and where being wrong costs most. Nothing about the model can fix that from the
inside: only the record of what followed its past numbers can, and that record is
the `bot-scorecard`.

So this part is deliberately separate from the model (T-1). The model answers
"given these features, what do I think"; this answers "when I have thought that,
what has happened". They fail in different ways and are fixed differently: a
model that is wrong needs different features, a model that is uncalibrated needs
none.

**Isotonic over bounded bins**, not Platt scaling. Platt assumes the error has a
logistic shape, which is exactly the assumption a model trained through a regime
change violates. Isotonic assumes only that a higher stated probability should
not map to a lower observed frequency -- the one thing that must hold, enforced by
pool-adjacent-violators rather than hoped for.

**Below enough outcomes the model's own number is passed through, marked
unfitted.** Not adjusted toward a prior, not held back: the caller is told this
is the model's number rather than a measured frequency, and the parts below act
on that distinction. A calibrator that quietly returned a made-up number would be
the most dangerous part in the bot, because it would look like evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import CalibratedConviction
from runtime.online_learner import ProbabilityCalibrator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-conviction-calibrator"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-conviction-calibrator",
    consumes=("bull-raw-conviction", "bot-scorecard"),
    produces=("bull-calibrated-conviction", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# Calibration is kept per regime as well as overall. A model that is well
# calibrated on average and overconfident in a trend is overconfident exactly
# when it is sizing up, and the average conceals it.
ALL_REGIMES = "all-regimes"


@dataclass
class CalibratorStanding:
    convictions_calibrated: int = 0
    fitted_calibrations: int = 0
    outcomes_observed: int = 0
    largest_correction: float = 0.0
    by_regime: dict = field(default_factory=dict)


class BullConvictionCalibrator:
    """Maps the model's stated probability to the frequency it has been followed by."""

    def __init__(
        self,
        bin_count: int,
        minimum_observations: int,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        self._bin_count = bin_count
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._calibrators: dict[str, ProbabilityCalibrator] = {}
        self.standing = CalibratorStanding()

    def observe_outcome(self, stated_probability: float, was_right: bool, regime: str = ALL_REGIMES) -> None:
        """One closed trade, into the overall record and into its regime's."""
        self.standing.outcomes_observed += 1
        for key in {ALL_REGIMES, regime}:
            self._calibrator_for(key).observe_outcome(stated_probability, was_right)
            self.standing.by_regime[key] = self.standing.by_regime.get(key, 0) + 1

    def observe_scorecard(self, scorecard) -> None:
        """Adopt what the bot's own scorecard has recorded per probability band.

        The scorecard is the durable record and this part's bins are a decayed
        view of it; a restarted part that ignored the scorecard would spend its
        first hundred trades uncalibrated all over again.
        """
        for band, record in scorecard.describe()["by_probability_band"].items():
            low, high = (float(part) for part in band.split("-"))
            midpoint = (low + high) / 2
            for _ in range(record["wins"]):
                self.observe_outcome(midpoint, True)
            for _ in range(record["trades"] - record["wins"]):
                self.observe_outcome(midpoint, False)

    def calibrate(self, raw, regime: str = ALL_REGIMES) -> CalibratedConviction:
        self.standing.convictions_calibrated += 1

        calibrator = self._calibrators.get(regime)
        if calibrator is None or not calibrator.is_fitted:
            calibrator = self._calibrator_for(ALL_REGIMES)
            used = ALL_REGIMES
        else:
            used = regime

        estimate = calibrator.calibrate(raw.probability)
        if estimate.is_fitted:
            self.standing.fitted_calibrations += 1
            self.standing.largest_correction = max(
                self.standing.largest_correction, abs(estimate.value - raw.probability)
            )
            reason = (
                f"the model said {raw.probability:.1%}; over {estimate.observations} recorded "
                f"outcomes in {used} that band has actually worked "
                f"{estimate.value:.1%} of the time"
            )
        else:
            reason = (
                f"the model said {raw.probability:.1%} and it is passed through unchanged: "
                f"{estimate.observations} of the {self._minimum} outcomes needed before this "
                f"bot's numbers can be read as frequencies"
            )

        return CalibratedConviction(
            bot=BOT,
            venue_id=raw.venue_id,
            symbol=raw.symbol,
            side=raw.side,
            raw_probability=raw.probability,
            calibrated=estimate,
            scorecard_observations=estimate.observations,
            reason=reason,
            calibrated_at_ns=self._now_ns(),
            # Carried through from the model that produced the raw number, so the
            # composer can ask whether the model is trained without having to
            # reach into a part it may not read (T-4).
            model_observations=raw.belief.observations_trained_on,
            model_is_trained=raw.belief.is_fitted,
        )

    def reliability(self, regime: str = ALL_REGIMES) -> tuple:
        """Stated against observed, which is what a reliability diagram plots."""
        calibrator = self._calibrators.get(regime)
        return () if calibrator is None else calibrator.reliability()

    def _calibrator_for(self, regime: str) -> ProbabilityCalibrator:
        calibrator = self._calibrators.get(regime)
        if calibrator is None:
            calibrator = ProbabilityCalibrator(
                bin_count=self._bin_count,
                minimum_observations=self._minimum,
                half_life_observations=self._half_life,
            )
            self._calibrators[regime] = calibrator
        return calibrator


def describe_calibration(calibrator: BullConvictionCalibrator) -> dict:
    return {
        "part_id": PART_ID,
        "convictions_calibrated": calibrator.standing.convictions_calibrated,
        "fitted_calibrations": calibrator.standing.fitted_calibrations,
        "passed_through_unfitted": (
            calibrator.standing.convictions_calibrated - calibrator.standing.fitted_calibrations
        ),
        "outcomes_observed": calibrator.standing.outcomes_observed,
        "largest_correction": calibrator.standing.largest_correction,
        "regimes_tracked": sorted(calibrator._calibrators),
        "reliability": calibrator.reliability(),
    }


def run_bull_conviction_calibrator(
    calibrator: BullConvictionCalibrator, control_socket, read_convictions_and_scorecard,
    publish_calibrated, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        convictions = read_convictions_and_scorecard(calibrator)
        publish_calibrated(
            tuple(calibrator.calibrate(raw, regime) for raw, regime in convictions)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A raw conviction is calibrated against the regime it was formed in, because a
    model that is well calibrated in a trend is not the same model in a chop. The
    regime is a level kept per symbol; a conviction with no regime yet is calibrated
    against 'any', which is what the classifier itself reports before it has seen a
    full window.
    """
    from runtime.input_assembly import Batch

    convictions = Batch(read=context.bus.reader("bull-raw-conviction"))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    publish_calibrated = context.bus.publisher_for("bull-calibrated-conviction")

    def read_convictions_and_scorecard(calibrator):
        for scorecard in scorecards.payloads():
            calibrator.observe_scorecard(scorecard)
        # The regime this part may use is the one carried on the conviction's own
        # reason chain, not a market-regime message: this part does not declare
        # market-regime, and reading a type it has not declared would be private
        # wiring of exactly the kind R-01 forbids.
        return tuple((raw, ALL_REGIMES) for raw in convictions.payloads())

    return run_bull_conviction_calibrator(
        calibrator=BullConvictionCalibrator(
            bin_count=int(context.number("bull_calibration_bin_count")),
            minimum_observations=int(context.number("bull_calibration_minimum_observations")),
            half_life_observations=context.number("bull_calibration_half_life_observations"),
        ),
        control_socket=context.control_socket,
        read_convictions_and_scorecard=read_convictions_and_scorecard,
        publish_calibrated=publish_calibrated,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
