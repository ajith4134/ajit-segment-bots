"""turbulence-index-gauge: how unusual today's whole market is, in one number.

Volatility says how much things are moving. Turbulence says how *unusual* the
pattern of movement is -- and they come apart at exactly the moments that matter.
A market where everything moves 2% is ordinary; a market where the things that
normally move together stop, or the things that never move suddenly do, is
turbulent even at the same volatility.

This is the Mahalanobis distance of today's return vector from its own history:

    d_t = (r_t - mu)' * S^-1 * (r_t - mu)

which measures a move against the covariance structure rather than against each
symbol's own volatility. A 2% move in a symbol that normally moves 2% is
unremarkable; the same move while its usual partner goes the other way is not,
and only the covariance form can tell them apart.

**The covariance is estimated with shrinkage.** With more symbols than
observations the sample covariance is singular and its inverse is arbitrarily
large -- the index would then be enormous and meaningless. Shrinking toward a
diagonal target is what makes the inverse exist and behave, and the shrinkage
weight is reported so the estimate's own reliability is visible.

**The index is reported as a percentile of its own history**, because the raw
distance has no units anyone can act on. "The 99th percentile of the last ninety
days" is something a risk gate can size against; "142.7" is not.

**It advises and does not act.** Turbulence is a fact about the market, not an
instruction; what to do about it belongs to the risk gate and the governor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy

from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "turbulence-index-gauge"

PART_DECLARATION = PartDeclaration(
    part_id="turbulence-index-gauge",
    consumes=("market-data",),
    produces=("turbulence-index", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MEASURED = "measured"
TOO_FEW_OBSERVATIONS = "too-few-observations-to-estimate-a-covariance"
TOO_FEW_SYMBOLS = "turbulence-is-a-property-of-a-market-not-of-one-symbol"
SINGULAR = "the-covariance-could-not-be-inverted-even-with-shrinkage"


@dataclass(frozen=True)
class TurbulenceIndex:
    """How unusual this market's joint movement is, against its own history."""

    state: str
    distance: float | None
    percentile: float | None
    symbols: tuple
    observations: int
    shrinkage: float | None
    largest_contributor: str | None
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == MEASURED and self.distance is not None

    def is_above(self, percentile: float) -> bool:
        return self.percentile is not None and self.percentile >= percentile


@dataclass
class GaugeStanding:
    measurements: int = 0
    measured: int = 0
    refused_few_observations: int = 0
    refused_few_symbols: int = 0
    refused_singular: int = 0
    highest_percentile_seen: float | None = None
    largest_distance_seen: float | None = None
    mean_shrinkage: float = 0.0


class TurbulenceIndexGauge:
    """Measures the market's joint movement against its own covariance structure."""

    def __init__(
        self,
        window_observations: int,
        minimum_observations: int,
        minimum_symbols: int,
        percentile_window: int,
        prior_distance: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_symbols < 2:
            raise ValueError(
                "turbulence is about how symbols move relative to each other; one symbol has "
                "no covariance structure to be unusual against"
            )
        if minimum_observations <= minimum_symbols:
            raise ValueError(
                "with fewer observations than symbols the sample covariance is singular and "
                "its inverse is arbitrarily large, which would make every reading enormous"
            )
        self._window = window_observations
        self._minimum_observations = minimum_observations
        self._minimum_symbols = minimum_symbols
        self._now_ns = now_ns
        self._returns: dict[str, list] = {}
        self._history = QuantileEstimator(window=percentile_window, prior=prior_distance)
        self._shrinkage_total = 0.0
        self.standing = GaugeStanding()

    def observe_returns(self, returns: dict) -> None:
        """One period's return for every symbol, together.

        Together on purpose: turbulence is about the joint move, and returns
        collected at different moments would measure a market that never existed
        at any one instant.
        """
        for symbol, value in returns.items():
            series = self._returns.setdefault(symbol, [])
            series.append(float(value))
            del series[: max(0, len(series) - self._window)]

    def _matrix(self) -> tuple:
        """The symbols with a full window, and their returns as a matrix."""
        length = min((len(series) for series in self._returns.values()), default=0)
        symbols = tuple(
            sorted(symbol for symbol, series in self._returns.items() if len(series) >= length)
        )
        if not symbols or length == 0:
            return (), numpy.zeros((0, 0)), 0
        matrix = numpy.array([self._returns[symbol][-length:] for symbol in symbols])
        return symbols, matrix, length

    def shrunk_covariance(self, matrix) -> tuple:
        """Sample covariance shrunk toward a diagonal target, Ledoit-Wolf style.

        The shrinkage weight is what stops the inverse exploding when the sample
        is small relative to the number of symbols, and it is returned rather
        than hidden because a reading from a heavily shrunk covariance is a
        weaker claim than one from a well-estimated matrix.
        """
        symbols, observations = matrix.shape
        centred = matrix - matrix.mean(axis=1, keepdims=True)
        sample = (centred @ centred.T) / max(1, observations - 1)

        # The target: the same average variance on the diagonal, zero elsewhere.
        average_variance = float(numpy.trace(sample) / symbols)
        target = numpy.eye(symbols) * average_variance

        # Shrinkage rises as observations fall relative to symbols, which is
        # exactly when the sample estimate stops being usable.
        weight = min(1.0, symbols / max(1.0, float(observations)))
        return (1.0 - weight) * sample + weight * target, weight

    def measure(self) -> TurbulenceIndex:
        self.standing.measurements += 1
        symbols, matrix, length = self._matrix()

        if len(symbols) < self._minimum_symbols:
            self.standing.refused_few_symbols += 1
            return self._index(
                TOO_FEW_SYMBOLS, None, None, symbols, length, None, None,
                f"{len(symbols)} symbol(s) of the {self._minimum_symbols} needed; turbulence "
                f"is a property of a market, not of one instrument",
            )

        if length < self._minimum_observations:
            self.standing.refused_few_observations += 1
            return self._index(
                TOO_FEW_OBSERVATIONS, None, None, symbols, length, None, None,
                f"{length} observation(s) of the {self._minimum_observations} needed to "
                f"estimate a covariance over {len(symbols)} symbol(s)",
            )

        covariance, shrinkage = self.shrunk_covariance(matrix)
        try:
            inverse = numpy.linalg.inv(covariance)
        except numpy.linalg.LinAlgError:
            self.standing.refused_singular += 1
            return self._index(
                SINGULAR, None, None, symbols, length, shrinkage, None,
                "the covariance could not be inverted even after shrinkage, so the distance "
                "would be arbitrarily large rather than large",
            )

        latest = matrix[:, -1]
        mean = matrix.mean(axis=1)
        deviation = latest - mean
        distance = float(deviation @ inverse @ deviation)

        # Which symbol contributed most, so a reading is actionable rather than
        # merely alarming.
        contributions = deviation * (inverse @ deviation)
        largest = symbols[int(numpy.argmax(numpy.abs(contributions)))]

        percentile = self._percentile_of(distance)
        self._history.observe(distance)

        self.standing.measured += 1
        self._shrinkage_total += shrinkage
        self.standing.mean_shrinkage = self._shrinkage_total / self.standing.measured
        if (
            self.standing.largest_distance_seen is None
            or distance > self.standing.largest_distance_seen
        ):
            self.standing.largest_distance_seen = distance
        if percentile is not None and (
            self.standing.highest_percentile_seen is None
            or percentile > self.standing.highest_percentile_seen
        ):
            self.standing.highest_percentile_seen = percentile

        return self._index(
            MEASURED, distance, percentile, symbols, length, shrinkage, largest,
            f"turbulence {distance:.2f} over {len(symbols)} symbol(s) and {length} "
            f"observation(s)"
            + (
                f", the {percentile:.0%} percentile of its own history -- which is the form "
                f"anything downstream can act on, because the raw distance has no units"
                if percentile is not None
                else ", with no history yet to rank it against"
            )
            + f"; {largest} contributed most. Measured against the covariance structure rather "
            f"than each symbol's own volatility, so a 2% move while a symbol's usual partner "
            f"goes the other way reads as unusual and the same move alone does not"
            + (
                f". The covariance is {shrinkage:.0%} shrunk toward its diagonal, so this is "
                f"a weaker claim than a well-estimated one"
                if shrinkage > 0.3
                else ""
            ),
        )

    def _percentile_of(self, distance: float) -> float | None:
        samples = self._history._samples
        if len(samples) < self._minimum_observations:
            return None
        return sum(1 for value in samples if value <= distance) / len(samples)

    def release(self, symbol: str) -> None:
        """T-3."""
        self._returns.pop(symbol, None)

    def _index(
        self, state, distance, percentile, symbols, observations, shrinkage, largest, reason
    ) -> TurbulenceIndex:
        return TurbulenceIndex(
            state=state,
            distance=distance,
            percentile=percentile,
            symbols=symbols,
            observations=observations,
            shrinkage=shrinkage,
            largest_contributor=largest,
            reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_turbulence(gauge: TurbulenceIndexGauge) -> dict:
    return {
        "part_id": PART_ID,
        "measurements": gauge.standing.measurements,
        "measured": gauge.standing.measured,
        "refused_too_few_symbols": gauge.standing.refused_few_symbols,
        "refused_too_few_observations": gauge.standing.refused_few_observations,
        "refused_singular_covariance": gauge.standing.refused_singular,
        "highest_percentile_seen": gauge.standing.highest_percentile_seen,
        "largest_distance_seen": gauge.standing.largest_distance_seen,
        "mean_shrinkage": gauge.standing.mean_shrinkage,
        "symbols_tracked": len(gauge._returns),
        "acts_on_its_own_reading": False,
    }


def run_turbulence_index_gauge(
    gauge: TurbulenceIndexGauge, control_socket, read_returns, publish_index,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_returns(gauge)
        publish_index(gauge.measure())

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

    One return per symbol per tick: the log change from the price sampled
    at the previous tick to the latest print now. Sampling on the tick puts
    every symbol on one clock, which a covariance across symbols needs.
    """
    import math

    from runtime.input_assembly import LatestByKey
    from runtime.venues.venue_adapter import NormalisedTrade

    trades = LatestByKey(read=context.bus.reader("market-data"), key_of=lambda t: (t.venue_id, t.symbol))
    publish_index = context.bus.publisher_for("turbulence-index")
    gauge = TurbulenceIndexGauge(
        window_observations=int(context.number("correlation_window_length")),
        minimum_observations=int(context.number("correlation_minimum_shared_observations")),
        minimum_symbols=int(context.number("turbulence_minimum_symbols")),
        percentile_window=int(context.number("turbulence_percentile_window")),
        prior_distance=context.number("turbulence_prior_distance"),
    )
    sampled: dict[str, float] = {}

    def read_returns(_gauge) -> None:
        latest: dict[str, float] = {}
        for (venue_id, symbol), trade in trades.mapping().items():
            if isinstance(trade, NormalisedTrade) and trade.price > 0:
                latest[symbol] = trade.price
        returns = {
            symbol: math.log(price / sampled[symbol])
            for symbol, price in latest.items() if symbol in sampled and sampled[symbol] > 0
        }
        sampled.update(latest)
        if returns:
            gauge.observe_returns(returns)

    def publish(index) -> None:
        if index is not None:
            publish_index((index,))

    return run_turbulence_index_gauge(
        gauge=gauge,
        control_socket=context.control_socket,
        read_returns=read_returns,
        publish_index=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
