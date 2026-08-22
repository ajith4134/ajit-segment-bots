"""volatility-feature-builder: what a volatility model gets to look at.

Volatility is the one quantity in this system with three genuinely independent
sources -- what already happened, what the options market expects, and what order
flow implies -- and this part assembles the first two into the features a
regressor reads.

The features are chosen for what they separate, not for completeness:

- **Realised volatility over several windows.** One window cannot distinguish
  volatility that is high and falling from volatility that is low and rising, and
  those two states are different trades.
- **Parkinson and Garman-Klass estimators alongside close-to-close.** Close-to-
  close throws away the high and the low, which is most of what a candle knows.
  Parkinson uses the range; Garman-Klass uses the open and close too, and the
  gap between them is itself informative: a symbol whose range volatility far
  exceeds its close-to-close is one that moves and comes back.
- **The overnight gap.** Crypto does not close, but funding settlements and
  session handovers still produce discontinuities, and a model that never sees
  them is surprised by every one.
- **Implied against realised**, when a surface exists. The premium of implied
  over realised is what the options market is charging for uncertainty, and it
  leads realised volatility more reliably than realised leads itself.

**A feature that cannot be computed is named as missing, never zeroed** -- the
same rule as every other feature builder in this system, for the same reason.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "volatility-feature-builder"

PART_DECLARATION = PartDeclaration(
    part_id="volatility-feature-builder",
    consumes=("kline-window", "implied-vol-surface"),
    produces=("vol-feature-set", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FEATURE_NAMES = (
    "close_to_close_short",
    "close_to_close_long",
    "volatility_ratio_short_to_long",
    "parkinson",
    "garman_klass",
    "range_over_close_to_close",
    "mean_gap_fraction",
    "largest_gap_fraction",
    "implied_at_the_money",
    "implied_over_realised",
    "implied_skew",
)


@dataclass(frozen=True)
class VolFeatureSet:
    """Everything a volatility model may look at, and what could not be measured."""

    venue_id: str
    symbol: str
    features: dict
    missing: tuple
    sources: dict
    candles_used: int
    built_at_ns: int

    @property
    def is_complete(self) -> bool:
        return not self.missing


@dataclass
class BuilderStanding:
    sets_built: int = 0
    complete_sets: int = 0
    surfaces_available: int = 0
    missing_by_feature: dict = field(default_factory=dict)


class VolatilityFeatureBuilder:
    """Builds the volatility feature set from candles and, when it exists, a surface."""

    def __init__(
        self,
        short_window: int,
        long_window: int,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if short_window >= long_window:
            raise ValueError(
                "one window cannot tell volatility that is high and falling from volatility "
                "that is low and rising; the two must differ"
            )
        self._short = short_window
        self._long = long_window
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._surfaces: dict[tuple[str, str], object] = {}
        self.standing = BuilderStanding()

    def observe_surface(self, venue_id: str, symbol: str, surface) -> None:
        self._surfaces[(venue_id, symbol)] = surface

    def build(self, window, horizon_seconds: float) -> VolFeatureSet:
        self.standing.sets_built += 1
        features: dict[str, float] = {}
        sources: dict[str, str] = {}
        missing: list[str] = []

        def record(name: str, value, source: str) -> None:
            if value is None:
                missing.append(name)
                self.standing.missing_by_feature[name] = (
                    self.standing.missing_by_feature.get(name, 0) + 1
                )
            else:
                features[name] = float(value)
                sources[name] = source

        candles = list(window.candles)
        short_return = self._close_to_close(candles[-self._short :])
        long_return = self._close_to_close(candles[-self._long :])
        record("close_to_close_short", short_return, f"{self._short}-candle closes")
        record("close_to_close_long", long_return, f"{self._long}-candle closes")
        record(
            "volatility_ratio_short_to_long",
            None if not short_return or not long_return else short_return / long_return,
            "short over long realised volatility",
        )

        parkinson = self._parkinson(candles[-self._long :])
        garman_klass = self._garman_klass(candles[-self._long :])
        record("parkinson", parkinson, "high-low range estimator")
        record("garman_klass", garman_klass, "open-high-low-close estimator")
        record(
            "range_over_close_to_close",
            None if not parkinson or not long_return else parkinson / long_return,
            "how much a symbol moves and comes back",
        )

        gaps = self._gap_fractions(candles)
        record(
            "mean_gap_fraction",
            None if not gaps else sum(abs(gap) for gap in gaps) / len(gaps),
            "close-to-next-open discontinuities",
        )
        record(
            "largest_gap_fraction",
            None if not gaps else max(abs(gap) for gap in gaps),
            "largest discontinuity in the window",
        )

        surface = self._surfaces.get((window.venue_id, window.symbol))
        if surface is None or not getattr(surface, "is_usable", False):
            for name in ("implied_at_the_money", "implied_over_realised", "implied_skew"):
                record(name, None, "no usable implied-vol surface")
        else:
            self.standing.surfaces_available += 1
            expiry = self._nearest_expiry(surface, horizon_seconds)
            implied = surface.at_the_money.get(expiry) if expiry is not None else None
            record("implied_at_the_money", implied, f"surface at {expiry}s to expiry")
            record(
                "implied_over_realised",
                None if implied is None or not long_return else implied / long_return,
                "what the options market charges for uncertainty over what has happened",
            )
            record(
                "implied_skew",
                surface.skew.get(expiry) if expiry is not None else None,
                "downside implied volatility less upside",
            )

        if not missing:
            self.standing.complete_sets += 1

        return VolFeatureSet(
            venue_id=window.venue_id,
            symbol=window.symbol,
            features=features,
            missing=tuple(missing),
            sources=sources,
            candles_used=len(candles),
            built_at_ns=self._now_ns(),
        )

    def _close_to_close(self, candles) -> float | None:
        """The standard deviation of log returns -- the textbook estimator."""
        if len(candles) < self._minimum:
            return None
        returns = [
            math.log(later.close / earlier.close)
            for earlier, later in zip(candles, candles[1:])
            if earlier.close > 0 and later.close > 0
        ]
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return math.sqrt(variance)

    def _parkinson(self, candles) -> float | None:
        """The high-low range estimator: five times more efficient than close-to-close.

        Close-to-close throws away the high and the low, which is most of what a
        candle knows about how far price actually travelled.
        """
        usable = [candle for candle in candles if candle.high > 0 and candle.low > 0]
        if len(usable) < self._minimum:
            return None
        total = sum(math.log(candle.high / candle.low) ** 2 for candle in usable)
        return math.sqrt(total / (4 * math.log(2) * len(usable)))

    def _garman_klass(self, candles) -> float | None:
        """Range and body together, which is more efficient again than the range alone."""
        usable = [
            candle
            for candle in candles
            if candle.high > 0 and candle.low > 0 and candle.open > 0 and candle.close > 0
        ]
        if len(usable) < self._minimum:
            return None
        total = 0.0
        for candle in usable:
            log_range = math.log(candle.high / candle.low)
            log_body = math.log(candle.close / candle.open)
            total += 0.5 * log_range ** 2 - (2 * math.log(2) - 1) * log_body ** 2
        return math.sqrt(max(0.0, total / len(usable)))

    def _gap_fractions(self, candles) -> list:
        """Close to the next open. Crypto never closes and still gaps."""
        return [
            (later.open - earlier.close) / earlier.close
            for earlier, later in zip(candles, candles[1:])
            if earlier.close > 0 and later.open != earlier.close
        ]

    def _nearest_expiry(self, surface, horizon_seconds: float) -> float | None:
        expiries = list(surface.at_the_money)
        if not expiries:
            return None
        return min(expiries, key=lambda expiry: abs(expiry - horizon_seconds))


def describe_vol_features(builder: VolatilityFeatureBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "sets_built": builder.standing.sets_built,
        "complete_sets": builder.standing.complete_sets,
        "incomplete_sets": builder.standing.sets_built - builder.standing.complete_sets,
        "sets_with_an_implied_surface": builder.standing.surfaces_available,
        "missing_by_feature": dict(sorted(builder.standing.missing_by_feature.items())),
        "feature_names": list(FEATURE_NAMES),
    }


def run_volatility_feature_builder(
    builder: VolatilityFeatureBuilder, control_socket, read_windows_and_surfaces,
    publish_feature_sets, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        requests = read_windows_and_surfaces(builder)
        publish_feature_sets(
            tuple(builder.build(window, horizon) for window, horizon in requests)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
