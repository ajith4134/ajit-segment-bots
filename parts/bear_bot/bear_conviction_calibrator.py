"""bear-conviction-calibrator: what this bot's 0.8 on a short has actually meant.

Same mechanism as the bull's, kept separate because the two sides' miscalibration
does not have the same shape and averaging them would hide both.

**Split by regime, and the regimes that matter are different.** A short model is
usually well calibrated in a downtrend and catastrophically overconfident in the
chop that precedes a squeeze -- the periods where its features look best are
precisely the ones its record is thinnest in. Bins per regime are what make that
visible rather than averaged away.

**The record decays.** A short book calibrated over a bear market carries
frequencies that the next expansion invalidates in days. The half-life is what
stops the bot spending a whole regime trading a mapping learned in the last one.

**A regime the bot has never shorted falls back to the overall record, marked as
having done so.** Not to a prior, and not to the regime's bull-side number: what
happened to the long book in a market says nothing about what happens to the
short book in it.

Below enough outcomes the model's own number is passed through, marked unfitted.

**The regime is `market-regime`, read the same way `signal-outcome-labeller`
already does, since 2026-08-30.** Before this every call site passed the same
`ALL_REGIMES` constant regardless of what regime a conviction actually formed
in, so the per-regime split above existed but was never exercised -- every
short was calibrated, and every outcome recorded, against the pooled record
only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SHORT, CalibratedConviction
from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.online_learner import ProbabilityCalibrator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-conviction-calibrator"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-conviction-calibrator",
    consumes=("bear-raw-conviction", "bot-scorecard", "training-label", "market-regime"),
    produces=("bear-calibrated-conviction", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

ALL_REGIMES = "all-regimes"


def label_outcome_for(label, direction: str) -> bool | None:
    """This label's `THE_SETUP_WAS_RIGHT` verdict, or None if it does not apply.

    Filtered to a closed-trade label (`calibration_key` empty, same population
    `bot-scorecard` already reads) and to the matching direction, because a
    single detector fires both sides and a label carries no other notion of
    which bot's trade it was labelling.
    """
    if label.calibration_key or label.direction != direction:
        return None
    return label.label_for(THE_SETUP_WAS_RIGHT)


@dataclass
class CalibratorStanding:
    convictions_calibrated: int = 0
    fitted_calibrations: int = 0
    fell_back_to_overall: int = 0
    outcomes_observed: int = 0
    largest_correction: float = 0.0
    largest_overconfidence: float = 0.0
    by_regime: dict = field(default_factory=dict)


class BearConvictionCalibrator:
    """Maps this bot's stated probability to the frequency shorts have actually worked."""

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
        self.standing.outcomes_observed += 1
        for key in {ALL_REGIMES, regime}:
            self._calibrator_for(key).observe_outcome(stated_probability, was_right)
            self.standing.by_regime[key] = self.standing.by_regime.get(key, 0) + 1

    def observe_scorecard(self, scorecard) -> None:
        """Adopt this bot's durable record, so a restart is not uncalibrated again."""
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
            if regime != ALL_REGIMES:
                self.standing.fell_back_to_overall += 1
            calibrator = self._calibrator_for(ALL_REGIMES)
            used = ALL_REGIMES
        else:
            used = regime

        estimate = calibrator.calibrate(raw.probability)
        if estimate.is_fitted:
            self.standing.fitted_calibrations += 1
            correction = estimate.value - raw.probability
            self.standing.largest_correction = max(
                self.standing.largest_correction, abs(correction)
            )
            if correction < 0:
                self.standing.largest_overconfidence = max(
                    self.standing.largest_overconfidence, -correction
                )
            reason = (
                f"the model said {raw.probability:.1%}; over {estimate.observations} recorded "
                f"short outcomes in {used} that band has actually worked {estimate.value:.1%} "
                f"of the time"
                + (
                    f" (the regime {regime} has too thin a record of its own, which is itself "
                    f"a reason for caution here)"
                    if used != regime
                    else ""
                )
            )
        else:
            reason = (
                f"the model said {raw.probability:.1%} and it is passed through unchanged: "
                f"{estimate.observations} of the {self._minimum} short outcomes needed before "
                f"this bot's numbers can be read as frequencies"
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


def describe_calibration(calibrator: BearConvictionCalibrator) -> dict:
    return {
        "part_id": PART_ID,
        "convictions_calibrated": calibrator.standing.convictions_calibrated,
        "fitted_calibrations": calibrator.standing.fitted_calibrations,
        "passed_through_unfitted": (
            calibrator.standing.convictions_calibrated - calibrator.standing.fitted_calibrations
        ),
        "fell_back_to_the_overall_record": calibrator.standing.fell_back_to_overall,
        "outcomes_observed": calibrator.standing.outcomes_observed,
        "largest_correction": calibrator.standing.largest_correction,
        "largest_overconfidence_corrected": calibrator.standing.largest_overconfidence,
        "regimes_tracked": sorted(calibrator._calibrators),
        "reliability": calibrator.reliability(),
    }


def run_bear_conviction_calibrator(
    calibrator: BearConvictionCalibrator, control_socket, read_convictions_and_scorecard,
    publish_calibrated, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_calibration(calibrator),
    )

def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A raw conviction is calibrated against the regime it was formed in, read from
    `market-regime` and kept per symbol; a symbol regime-classifier has not
    reached yet calibrates against `ALL_REGIMES`, same as an outcome for a symbol
    with no regime record.

    Outcomes are observed from `training-label`, filtered to a closed-trade label
    (`calibration_key` empty, same population `bot-scorecard` already reads)
    whose direction is short and whose setup component was judged -- the same
    detector fires both directions, and a label carries no other notion of which
    bot's trade it was labelling. The label names when the trade opened, not what
    this bot said about it at the time, so the raw conviction the model published
    for that symbol around then is remembered and matched by timestamp -- the
    same join `bull-conviction-model` already does for feature vectors.
    """
    from runtime.input_assembly import Batch, LatestByKey

    convictions = Batch(read=context.bus.reader("bear-raw-conviction"))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    labels = Batch(read=context.bus.reader("training-label"))
    regimes = LatestByKey(
        read=context.bus.reader("market-regime"),
        key_of=lambda regime: (regime.venue_id, regime.symbol),
    )
    publish_calibrated = context.bus.publisher_for("bear-calibrated-conviction")

    remembered: dict[tuple[str, str], list] = {}
    remembered_per_symbol = int(context.number("bear_remembered_convictions_per_symbol"))

    def remember(raw) -> None:
        key = (raw.venue_id, raw.symbol)
        history = remembered.setdefault(key, [])
        history.append(raw)
        if len(history) > remembered_per_symbol:
            del history[0]

    def probability_current_at(venue_id: str, symbol: str, at_ns: int) -> float | None:
        history = remembered.get((venue_id, symbol), ())
        current = None
        for raw in history:
            if raw.formed_at_ns <= at_ns:
                current = raw
            else:
                break
        return current.probability if current is not None else None

    def read_convictions_and_scorecard(calibrator):
        for scorecard in scorecards.payloads():
            calibrator.observe_scorecard(scorecard)

        for label in labels.payloads():
            outcome = label_outcome_for(label, SHORT)
            if outcome is None:
                continue
            probability = probability_current_at(
                label.venue_id, label.symbol, label.feature_lookup_at_ns
            )
            if probability is None:
                continue
            calibrator.observe_outcome(probability, outcome, label.regime)

        regime_by_symbol = regimes.mapping()
        paired = []
        for raw in convictions.payloads():
            remember(raw)
            regime = regime_by_symbol.get((raw.venue_id, raw.symbol))
            paired.append((raw, regime.regime if regime else ALL_REGIMES))
        return tuple(paired)

    return run_bear_conviction_calibrator(
        calibrator=BearConvictionCalibrator(
            bin_count=int(context.number("bear_calibration_bin_count")),
            minimum_observations=int(context.number("bear_calibration_minimum_observations")),
            half_life_observations=context.number("bear_calibration_half_life_observations"),
        ),
        control_socket=context.control_socket,
        read_convictions_and_scorecard=read_convictions_and_scorecard,
        publish_calibrated=publish_calibrated,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
