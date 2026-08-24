"""mean-reversion-detector: a symbol stretched far enough from its mean to snap back.

The oldest idea in trading and the easiest to lose money on, because the two
things it needs are in tension: a price far from its mean is either about to
revert or in the early part of a trend that will go much further, and the
observation is identical.

Three things separate this from a naive z-score:

- **The regime decides whether to fire at all.** In a trending series a stretched
  price is a trend continuing, and a reverter that ignores regime is a machine
  for selling strength in a bull market.
- **Confidence is learned, not the z-score** (RL-060). How unusual a deviation is
  says nothing about how often it reverts, and only this detector's own record
  can say that -- separately per regime, because its hit rate differs completely
  between them.
- **A stretched price with no volatility to revert through is refused.** A z-score
  computed on a series that barely moved is arithmetic, not a signal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.market_signal import LONG, REVERSION, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "mean-reversion-detector"

PART_DECLARATION = PartDeclaration(
    part_id="mean-reversion-detector",
    consumes=("market-data", "market-regime", "playbook-rule"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_STRETCHED = "not-stretched-far-enough"
WRONG_REGIME = "regime-does-not-favour-reversion"
TOO_FEW_OBSERVATIONS = "too-few-observations"
NO_VOLATILITY = "no-volatility-to-revert-through"


@dataclass
class DetectorStanding:
    observations: int = 0
    candidates: int = 0
    not_stretched: int = 0
    wrong_regime: int = 0
    too_few: int = 0
    no_volatility: int = 0
    symbols_tracked: int = 0
    outcomes_learned: int = 0
    largest_z: float = 0.0


class MeanReversionDetector:
    """Fires when a price is unusually far from its own mean, and the regime agrees."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        z_threshold: float,
        minimum_volatility_fraction: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if z_threshold <= 0:
            raise ValueError("a threshold of zero fires on every observation")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._z_threshold = z_threshold
        self._minimum_volatility = minimum_volatility_fraction
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self.standing = DetectorStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        self.standing.observations += 1
        window = self._prices.get((venue_id, symbol))
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._prices[(venue_id, symbol)] = window
        window.observe(price, at_ns)
        self.standing.symbols_tracked = len(self._prices)

    def observe_outcome(self, regime: str, reverted: bool) -> None:
        """Whether the price actually reverted within the horizon after a call."""
        self._calibrator.observe_outcome(PART_ID, regime, reverted)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str, regime) -> tuple[object | None, str]:
        """One symbol against its own history. Returns the candidate and the outcome."""
        window = self._prices.get((venue_id, symbol))
        if window is None or window.count < self._minimum:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS

        price = window.latest
        mean = window.mean(self._minimum)
        deviation = window.standard_deviation(self._minimum)
        if mean is None or deviation is None:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS

        if mean == 0 or deviation / mean < self._minimum_volatility:
            # A z-score on a series that barely moved is arithmetic, not a signal.
            self.standing.no_volatility += 1
            return None, NO_VOLATILITY

        z = window.z_score(price, self._minimum)
        if z is None or abs(z) < self._z_threshold:
            self.standing.not_stretched += 1
            return None, NOT_STRETCHED

        if not regime.favours(REVERSION):
            # In a trend, a stretched price is a trend continuing.
            self.standing.wrong_regime += 1
            return None, WRONG_REGIME

        self.standing.largest_z = max(self.standing.largest_z, abs(z))
        self.standing.candidates += 1
        direction = SHORT if z > 0 else LONG
        confidence = self._calibrator.confidence(PART_ID, regime.regime)

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=REVERSION,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "price": price,
                    "mean": mean,
                    "standard_deviation": deviation,
                    "z_score": z,
                    "observations": window.count,
                    "regime": regime.regime,
                    "hurst": regime.hurst,
                },
                reason=(
                    f"{price:g} is {abs(z):.2f} standard deviations "
                    f"{'above' if z > 0 else 'below'} its {window.count}-observation mean of "
                    f"{mean:g}, in a {regime.regime} regime; reverted "
                    f"{confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_reversion(detector: MeanReversionDetector) -> dict:
    return {
        "part_id": PART_ID,
        "observations": detector.standing.observations,
        "symbols_tracked": detector.standing.symbols_tracked,
        "candidates": detector.standing.candidates,
        "not_stretched": detector.standing.not_stretched,
        "wrong_regime": detector.standing.wrong_regime,
        "too_few_observations": detector.standing.too_few,
        "no_volatility": detector.standing.no_volatility,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_z": detector.standing.largest_z,
    }


def run_mean_reversion_detector(
    detector: MeanReversionDetector, control_socket, read_prices_and_regimes, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        regimes = read_prices_and_regimes(detector)
        candidates = []
        for regime in regimes:
            candidate, _ = detector.detect(regime.venue_id, regime.symbol, regime)
            if candidate is not None:
                candidates.append(candidate)
        publish_candidates(tuple(candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("market-data"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = MeanReversionDetector(
        window_length=int(context.number("detector_window_length")),
        minimum_observations=int(context.number("detector_minimum_observations")),
        z_threshold=context.number("detector_z_threshold"),
        minimum_volatility_fraction=context.number("mean_reversion_minimum_volatility_fraction"),
        horizon_seconds=context.number("mean_reversion_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
    )

    def read_prices_and_regimes(_detector):
        rules.payloads()
        touched = set()
        for trade in trades.payloads():
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
            )
            touched.add((trade.venue_id, trade.symbol))
        by_symbol = regimes.mapping()
        return tuple(by_symbol[key] for key in sorted(touched) if key in by_symbol)

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_mean_reversion_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_prices_and_regimes=read_prices_and_regimes,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
