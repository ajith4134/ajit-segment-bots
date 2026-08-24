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

from runtime.price_frames import levels_in
from runtime.price_staleness import ObservedPrice, PriceStalenessEstimator, price_staleness_from
from runtime.market_signal import LONG, REVERSION, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "spread-reversion-detector"

PART_DECLARATION = PartDeclaration(
    part_id="spread-reversion-detector",
    consumes=("cointegrated-pair", "symbol-price-frame"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_STRETCHED = "spread-not-stretched-far-enough"
PAIR_NOT_COINTEGRATED = "pair-is-not-currently-cointegrated"
NO_PRICES = "no-current-prices-for-both-legs"
A_LEG_IS_STALE = "one-leg's-last-price-is-too-old-to-price-the-spread-with"


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
    stale_leg: int = 0
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
        price_staleness: PriceStalenessEstimator | None = None,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if z_threshold <= 0:
            raise ValueError("a threshold of zero fires on every observation")
        self._z_threshold = z_threshold
        self._window_length = window_length
        self._minimum = minimum_observations
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        # A spread is two prices subtracted, and they arrive separately. If either
        # leg is old the difference is not a spread that ever existed -- and a
        # stale leg produces exactly the shape this detector exists to fire on,
        # because a price that stopped moving while the other leg ran looks like
        # the widest stretch it has ever seen. Both bounds are the caller's to
        # state; this part invents neither (RL-061).
        self._price_staleness = price_staleness
        self._maximum_gap_seconds = maximum_gap_seconds
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._spreads: dict[tuple[str, str, str], RollingWindow] = {}
        self.standing = SpreadStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One leg's print, with the venue's own time for it."""
        self._prices[(venue_id, symbol)] = ObservedPrice(price=price, observed_at_ns=at_ns)

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

        if self._price_staleness is not None:
            at = self._now_ns()
            for symbol, observed in (
                (pair.left_symbol, left), (pair.right_symbol, right),
            ):
                bound = self._price_staleness.believable_age_seconds(pair.venue_id, symbol)
                if observed.age_seconds(at) > bound.value:
                    self.standing.stale_leg += 1
                    return None, A_LEG_IS_STALE

        spread = left.price - pair.hedge_ratio * right.price
        # The spread is as recent as its older leg, not as its newer one: a
        # difference is only as current as the least current thing in it.
        spread_at_ns = min(left.observed_at_ns, right.observed_at_ns)
        key = (pair.venue_id, pair.left_symbol, pair.right_symbol)
        window = self._spreads.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length, maximum_gap_seconds=self._maximum_gap_seconds
            )
            self._spreads[key] = window
        window.observe(spread, spread_at_ns)

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
        "stale_leg": detector.standing.stale_leg,
        "outcomes_learned": detector.standing.outcomes_learned,
        "widest_z": detector.standing.widest_z,
    }


def run_spread_reversion_detector(
    detector: SpreadReversionDetector, control_socket, read_pairs, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_spreads(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A cointegrated pair is a level -- these two symbols hold together, until the
    finder says they no longer do -- so the latest verdict per pair is kept across
    ticks. A pair the finder retires arrives as a fresh verdict with a state that is
    no longer tradeable, and `detect` refuses it: a stretched spread on a retired
    pair is two symbols that have parted company, and it looks exactly like the best
    opportunity there has ever been.
    """
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    pairs = LatestByKey(
        read=context.bus.reader("cointegrated-pair"),
        key_of=lambda pair: (pair.venue_id, pair.left_symbol, pair.right_symbol),
    )
    publish_candidates = context.bus.publisher_for("entry-candidate")

    price_staleness = price_staleness_from(context)
    detector = SpreadReversionDetector(
        z_threshold=context.number("spread_reversion_z_threshold"),
        window_length=int(context.number("spread_reversion_window_length")),
        minimum_observations=int(context.number("spread_reversion_minimum_observations")),
        horizon_seconds=context.number("spread_reversion_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
        price_staleness=price_staleness,
        maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
    )

    def read_pairs(_detector):
        for trade in levels_in(trades.payloads()):
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
            price_staleness.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        return pairs.values()

    return run_spread_reversion_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_pairs=read_pairs,
        publish_candidates=publish_candidates,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
