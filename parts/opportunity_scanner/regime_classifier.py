"""regime-classifier: which regime the market is in, by a Hurst proxy.

Every detector downstream is right in one regime and wrong in another. A mean
reverter is a machine for losing money in a trend; a momentum detector gives back
everything it makes in a chop. So the regime is not decoration -- it is the thing
that decides which detectors should be believed at all.

Hurst rather than a moving-average cross, because a cross tells you what the
price did and Hurst tells you what *kind* of series it is. A series can be rising
and mean-reverting at once, and a cross cannot express that.

Three states, and the third is the important one:

- **Trending** -- moves persist, so continuation setups have an edge.
- **Reverting** -- moves are given back, so reversion setups have an edge.
- **Random** -- neither. Both kinds of detector will lose slowly, and saying so
  is more useful than picking the nearer of the two.

**Unclassified is its own state.** Below enough observations the estimator swings
wildly on the same data, and a regime that flips between trending and reverting
every tick is worse than no regime at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, hurst_exponent

PART_ID = "regime-classifier"

PART_DECLARATION = PartDeclaration(
    part_id="regime-classifier",
    consumes=("market-data",),
    produces=("market-regime", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TRENDING = "trending"
REVERTING = "reverting"
RANDOM = "random-walk"
UNCLASSIFIED = "unclassified"

# The Hurst value of a pure random walk. Everything here is a distance from it.
RANDOM_WALK_HURST = 0.5


@dataclass(frozen=True)
class MarketRegime:
    """What kind of series this symbol is right now, and how sure that is."""

    venue_id: str
    symbol: str
    regime: str
    hurst: float | None
    distance_from_random: float | None
    observations: int
    volatility: float | None
    reason: str
    classified_at_ns: int

    @property
    def is_classified(self) -> bool:
        return self.regime != UNCLASSIFIED

    def favours(self, expectation: str) -> bool:
        """Whether this regime supports a detector expecting reversion or continuation."""
        from runtime.market_signal import CONTINUATION, REVERSION

        if self.regime == TRENDING:
            return expectation == CONTINUATION
        if self.regime == REVERTING:
            return expectation == REVERSION
        return False


@dataclass
class ClassifierStanding:
    observations: int = 0
    classifications: int = 0
    symbols_tracked: int = 0
    by_regime: dict = field(default_factory=dict)
    unclassified: int = 0


class RegimeClassifier:
    """Estimates the Hurst exponent per symbol and names the regime it implies."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        trending_above: float,
        reverting_below: float,
        now_ns=time.time_ns,
    ) -> None:
        if not reverting_below < RANDOM_WALK_HURST < trending_above:
            raise ValueError(
                "the reverting and trending thresholds must sit either side of the "
                f"random-walk value of {RANDOM_WALK_HURST}"
            )
        self._window_length = window_length
        self._minimum = minimum_observations
        self._trending_above = trending_above
        self._reverting_below = reverting_below
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self.standing = ClassifierStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        self.standing.observations += 1
        self._window_for((venue_id, symbol)).observe(price)
        self.standing.symbols_tracked = len(self._prices)

    def classify(self, venue_id: str, symbol: str) -> MarketRegime:
        window = self._prices.get((venue_id, symbol))
        if window is None:
            return self._regime(
                venue_id, symbol, UNCLASSIFIED, None, 0, None,
                "no prices have been seen for this symbol",
            )

        series = list(window.values)
        hurst = hurst_exponent(series, self._minimum)
        volatility = window.standard_deviation(self._minimum)
        self.standing.classifications += 1

        if hurst is None:
            self.standing.unclassified += 1
            return self._regime(
                venue_id, symbol, UNCLASSIFIED, None, len(series), volatility,
                f"{len(series)} observations of the {self._minimum} needed; below that the "
                f"estimator swings on the same data and a regime that flips every tick is "
                f"worse than none",
            )

        if hurst >= self._trending_above:
            regime = TRENDING
            reason = f"Hurst {hurst:.3f} above {self._trending_above:.3f}: moves persist"
        elif hurst <= self._reverting_below:
            regime = REVERTING
            reason = f"Hurst {hurst:.3f} below {self._reverting_below:.3f}: moves are given back"
        else:
            regime = RANDOM
            reason = (
                f"Hurst {hurst:.3f} is inside [{self._reverting_below:.3f}, "
                f"{self._trending_above:.3f}]: neither kind of detector has an edge here"
            )

        self.standing.by_regime[regime] = self.standing.by_regime.get(regime, 0) + 1
        return self._regime(venue_id, symbol, regime, hurst, len(series), volatility, reason)

    def classify_all(self) -> tuple[MarketRegime, ...]:
        return tuple(self.classify(venue, symbol) for venue, symbol in sorted(self._prices))

    def _window_for(self, key) -> RollingWindow:
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._prices[key] = window
        return window

    def _regime(self, venue_id, symbol, regime, hurst, observations, volatility, reason) -> MarketRegime:
        return MarketRegime(
            venue_id=venue_id,
            symbol=symbol,
            regime=regime,
            hurst=hurst,
            distance_from_random=None if hurst is None else hurst - RANDOM_WALK_HURST,
            observations=observations,
            volatility=volatility,
            reason=reason,
            classified_at_ns=self._now_ns(),
        )


def describe_regimes(classifier: RegimeClassifier) -> dict:
    return {
        "part_id": PART_ID,
        "observations": classifier.standing.observations,
        "symbols_tracked": classifier.standing.symbols_tracked,
        "classifications": classifier.standing.classifications,
        "unclassified": classifier.standing.unclassified,
        "by_regime": dict(classifier.standing.by_regime),
    }


def run_regime_classifier(
    classifier: RegimeClassifier, control_socket, read_prices, publish_regimes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for venue_id, symbol, price in read_prices():
            classifier.observe_price(venue_id, symbol, price)
        publish_regimes(classifier.classify_all())

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
    """The one entry point every part carries (T-1).

    Trades arrive as an event stream and prices are observed one by one; the regime
    is a level, republished every tick for every symbol seen so far. That asymmetry
    is deliberate. A consumer that joined late must still learn what regime a symbol
    is in, and a classifier that only spoke when the regime *changed* would leave it
    with nothing and no way to know it was missing something.

    Woken by its data rather than by its clock: the whole point of the regime is to
    be current when a detector asks, and the tick floor keeps a busy symbol from
    spinning this part at the rate of the tape.
    """
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("market-data"))
    publish_regimes = context.bus.publisher_for("market-regime")

    def read_prices():
        return tuple(
            (trade.venue_id, trade.symbol, trade.price) for trade in trades.payloads()
        )

    return run_regime_classifier(
        classifier=RegimeClassifier(
            window_length=int(context.number("regime_window_length")),
            minimum_observations=int(context.number("regime_minimum_observations")),
            trending_above=context.number("regime_trending_hurst_above"),
            reverting_below=context.number("regime_reverting_hurst_below"),
        ),
        control_socket=context.control_socket,
        read_prices=read_prices,
        publish_regimes=publish_regimes,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
