"""spread-reversion-detector: a cointegrated spread stretched far enough to snap back.

The trade that follows from `cointegration-pair-finder`. The pair finder says the
spread reverts; this says it has stretched far enough now to be worth acting on.

Two positions, not one. A spread trade is long one symbol and short the other at
the hedge ratio, and that is the point: the market direction cancels and what is
left is the relationship. A detector that fired on one leg would be taking a
directional bet it never intended.

**It refuses a pair whose cointegration has lapsed.** The pair finder retires
pairs whose spread stops reverting, and a stretched spread on a retired pair is
not an opportunity -- it is two symbols that have parted company, and the trade
that looks best is the one that has already gone wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.market_signal import LONG, REVERSION, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "spread-reversion-detector"

PART_DECLARATION = PartDeclaration(
    part_id="spread-reversion-detector",
    consumes=("cointegrated-pair", "market-data"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_STRETCHED = "spread-not-stretched-far-enough"
PAIR_NOT_COINTEGRATED = "pair-is-not-currently-cointegrated"
NO_PRICES = "no-current-prices-for-both-legs"


@dataclass(frozen=True)
class SpreadCandidatePair:
    """Both legs of one spread trade, and which way each goes."""

    long_symbol: str
    short_symbol: str
    hedge_ratio: float
    spread_z: float


@dataclass
class SpreadStanding:
    tests: int = 0
    candidates: int = 0
    not_stretched: int = 0
    not_cointegrated: int = 0
    no_prices: int = 0
    outcomes_learned: int = 0
    widest_z: float = 0.0


class SpreadReversionDetector:
    """Fires when a live cointegrated spread is unusually far from its own mean."""

    def __init__(
        self,
        z_threshold: float,
        window_length: int,
        minimum_observations: int,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        now_ns=time.time_ns,
    ) -> None:
        if z_threshold <= 0:
            raise ValueError("a threshold of zero fires on every observation")
        self._z_threshold = z_threshold
        self._window_length = window_length
        self._minimum = minimum_observations
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._spreads: dict[tuple[str, str, str], RollingWindow] = {}
        self.standing = SpreadStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        self._prices[(venue_id, symbol)] = price

    def observe_outcome(self, regime: str, reverted: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, regime, reverted)
        self.standing.outcomes_learned += 1

    def detect(self, pair, regime_name: str = "any") -> tuple[object | None, str]:
        """One pair's live spread against its own history."""
        self.standing.tests += 1

        if not pair.is_tradeable:
            # A stretched spread on a retired pair is two symbols that have
            # parted company, and it looks exactly like the best opportunity.
            self.standing.not_cointegrated += 1
            return None, PAIR_NOT_COINTEGRATED

        left = self._prices.get((pair.venue_id, pair.left_symbol))
        right = self._prices.get((pair.venue_id, pair.right_symbol))
        if left is None or right is None:
            self.standing.no_prices += 1
            return None, NO_PRICES

        spread = left - pair.hedge_ratio * right
        key = (pair.venue_id, pair.left_symbol, pair.right_symbol)
        window = self._spreads.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._spreads[key] = window
        window.observe(spread)

        z = window.z_score(spread, self._minimum)
        if z is None:
            # Fall back to the pair finder's own statistics while this detector's
            # own window fills, rather than refusing a pair already proven.
            if pair.spread_deviation and pair.spread_deviation > 0 and pair.spread_mean is not None:
                z = (spread - pair.spread_mean) / pair.spread_deviation
            else:
                self.standing.no_prices += 1
                return None, NO_PRICES

        if abs(z) < self._z_threshold:
            self.standing.not_stretched += 1
            return None, NOT_STRETCHED

        self.standing.widest_z = max(self.standing.widest_z, abs(z))
        self.standing.candidates += 1
        # A high spread means the left leg is rich against the right: sell the
        # left, buy the right. The candidate names the left leg's direction and
        # carries both legs in its evidence.
        direction = SHORT if z > 0 else LONG
        legs = SpreadCandidatePair(
            long_symbol=pair.right_symbol if z > 0 else pair.left_symbol,
            short_symbol=pair.left_symbol if z > 0 else pair.right_symbol,
            hedge_ratio=pair.hedge_ratio,
            spread_z=z,
        )
        confidence = self._calibrator.confidence(PART_ID, regime_name)

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=pair.venue_id,
                symbol=pair.left_symbol,
                direction=direction,
                expectation=REVERSION,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "long_symbol": legs.long_symbol,
                    "short_symbol": legs.short_symbol,
                    "hedge_ratio": pair.hedge_ratio,
                    "spread": spread,
                    "spread_z": z,
                    "reversion_strength": pair.reversion_strength,
                    "both_legs_required": True,
                },
                reason=(
                    f"the {pair.left_symbol}/{pair.right_symbol} spread is {abs(z):.2f} standard "
                    f"deviations {'wide' if z > 0 else 'narrow'} at a hedge ratio of "
                    f"{pair.hedge_ratio:.4f}; long {legs.long_symbol} and short "
                    f"{legs.short_symbol}, which cancels market direction. Reverted "
                    f"{confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_spreads(detector: SpreadReversionDetector) -> dict:
    return {
        "part_id": PART_ID,
        "tests": detector.standing.tests,
        "candidates": detector.standing.candidates,
        "not_stretched": detector.standing.not_stretched,
        "pair_not_cointegrated": detector.standing.not_cointegrated,
        "no_prices": detector.standing.no_prices,
        "outcomes_learned": detector.standing.outcomes_learned,
        "widest_z": detector.standing.widest_z,
    }


def run_spread_reversion_detector(
    detector: SpreadReversionDetector, control_socket, read_pairs, publish_candidates,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        pairs = read_pairs(detector)
        candidates = []
        for pair in pairs:
            candidate, _ = detector.detect(pair)
            if candidate is not None:
                candidates.append(candidate)
        publish_candidates(tuple(candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
