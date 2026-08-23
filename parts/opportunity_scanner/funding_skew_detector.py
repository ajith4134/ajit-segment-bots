"""funding-skew-detector: a funding rate stretched far enough that the crowd unwinds.

Funding is the clearest crowding signal a perpetual market has. When longs pay
shorts heavily, the long side is crowded and paying for the privilege; the
position becomes expensive to hold, and the marginal holder eventually stops
holding it. That unwind is the trade.

The subtlety that decides whether this makes money: **funding being high is not
the signal**. A rate can sit high for weeks in a strong trend, and fading it the
whole way is how a funding strategy loses more than it ever earned. What matters
is the rate being extreme *relative to its own recent history* and the price no
longer rewarding the crowded side.

So it fires on the deviation, not the level, and it carries the carry cost in its
evidence -- because the trade is only worth taking if the funding collected
exceeds what the position risks while waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.market_signal import LONG, SHORT, UNWIND, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "funding-skew-detector"

PART_DECLARATION = PartDeclaration(
    part_id="funding-skew-detector",
    consumes=("market-data", "funding-forecast", "playbook-rule"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_SKEWED = "funding-not-stretched-for-this-symbol"
TOO_FEW_OBSERVATIONS = "too-few-funding-observations"
PRICE_STILL_REWARDING = "the-crowded-side-is-still-being-paid"

# Funding settles three times a day on both phase 1 venues, so a rate is
# annualised over that cadence when reporting what the carry is worth.
SETTLEMENTS_PER_DAY = 3


@dataclass
class SkewStanding:
    observations: int = 0
    candidates: int = 0
    not_skewed: int = 0
    too_few: int = 0
    price_still_rewarding: int = 0
    symbols_tracked: int = 0
    outcomes_learned: int = 0
    largest_skew_z: float = 0.0


class FundingSkewDetector:
    """Fires when funding is extreme against its own history and price has stopped paying."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        skew_z_threshold: float,
        price_confirmation_window: int,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        now_ns=time.time_ns,
    ) -> None:
        if skew_z_threshold <= 0:
            raise ValueError("a threshold of zero calls every funding rate extreme")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._threshold = skew_z_threshold
        self._price_window = price_confirmation_window
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        self._funding: dict[tuple[str, str], RollingWindow] = {}
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self.standing = SkewStanding()

    def observe_funding(self, venue_id: str, symbol: str, rate: float) -> None:
        self.standing.observations += 1
        key = (venue_id, symbol)
        window = self._funding.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._funding[key] = window
        window.observe(rate)
        self.standing.symbols_tracked = len(self._funding)

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(length=self._price_window)
            self._prices[key] = window
        window.observe(price)

    def observe_outcome(self, regime: str, unwound: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, regime, unwound)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str, regime_name: str = "any") -> tuple[object | None, str]:
        key = (venue_id, symbol)
        funding = self._funding.get(key)
        if funding is None or funding.count < self._minimum:
            self.standing.too_few += 1
            return None, TOO_FEW_OBSERVATIONS

        rate = funding.latest
        z = funding.z_score(rate, self._minimum)
        if z is None or abs(z) < self._threshold:
            # The level is not the signal: a rate can sit high for weeks in a
            # strong trend, and fading it the whole way is how this loses.
            self.standing.not_skewed += 1
            return None, NOT_SKEWED

        prices = self._prices.get(key)
        if prices is not None and prices.count >= 2:
            returns = prices.returns()
            recent_move = sum(returns[-min(len(returns), 5) :])
            crowded_long = rate > 0
            still_paying = recent_move > 0 if crowded_long else recent_move < 0
            if still_paying:
                # The crowded side is still being rewarded by price. Fading it
                # now is fighting the trend that created the crowding.
                self.standing.price_still_rewarding += 1
                return None, PRICE_STILL_REWARDING

        self.standing.largest_skew_z = max(self.standing.largest_skew_z, abs(z))
        self.standing.candidates += 1
        # Positive funding means longs pay: the crowded side is long, so the
        # unwind is downward and the trade is short.
        direction = SHORT if rate > 0 else LONG
        confidence = self._calibrator.confidence(PART_ID, regime_name)
        annualised = rate * SETTLEMENTS_PER_DAY * 365

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=UNWIND,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "funding_rate": rate,
                    "annualised_carry": annualised,
                    "funding_z": z,
                    "observations": funding.count,
                    "crowded_side": "long" if rate > 0 else "short",
                    "carry_collected_per_settlement": abs(rate),
                },
                reason=(
                    f"funding of {rate:+.4%} is {abs(z):.2f} standard deviations from this "
                    f"symbol's own range, so the {'long' if rate > 0 else 'short'} side is "
                    f"crowded and paying {abs(annualised):.1%} annualised to stay there, and "
                    f"price has stopped rewarding it. Unwound {confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_funding_skew(detector: FundingSkewDetector) -> dict:
    return {
        "part_id": PART_ID,
        "observations": detector.standing.observations,
        "symbols_tracked": detector.standing.symbols_tracked,
        "candidates": detector.standing.candidates,
        "not_skewed": detector.standing.not_skewed,
        "too_few_observations": detector.standing.too_few,
        "price_still_rewarding": detector.standing.price_still_rewarding,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_skew_z": detector.standing.largest_skew_z,
    }


def run_funding_skew_detector(
    detector: FundingSkewDetector, control_socket, read_funding, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_funding(detector)
        candidates = []
        for venue_id, symbol in symbols:
            candidate, _ = detector.detect(venue_id, symbol)
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
