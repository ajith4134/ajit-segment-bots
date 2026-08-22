"""copy-latency-estimator: how much of somebody else's edge the delay eats.

Copying is arithmetic, not admiration. Between somebody opening a position and this
system being able to open the same one there is a delay, and during that delay the
price moves -- usually in the direction that made their entry good. The estimator
measures that move. It is the number that decides whether following a profitable
trader is a strategy or a way of paying for their exit.

Three things make this measurement easy to get wrong, and each is handled here:

- **The delay is not one number.** It is the sum of how long the source took to
  publish, how long the read cycle took to notice, and how long an order takes to
  reach the venue. They are measured together, end to end, because only the total
  is what the price had to move through.
- **The move is not symmetric.** What matters is not how far the price travelled
  but how far it travelled *against the copier*. A trader who bought and then saw
  the price fall gave the copier a better entry, and averaging that in with the
  adverse cases produces a number that understates the cost of the ones that hurt.
- **It is per symbol.** A thin altcoin moves several times further in the same
  seconds than BTCUSDT does. One global latency cost applied to both makes the
  liquid symbol look uncopyable and the illiquid one look free -- exactly backwards.

Nothing here is assumed. The estimator starts unfitted, says so, and reports its
observation count with every answer, so a consumer can tell a measured cost from a
starting guess (RL-061, Rule 8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import CopyLatency
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "copy-latency-estimator"

PART_DECLARATION = PartDeclaration(
    part_id="copy-latency-estimator",
    consumes=("external-position", "market-data"),
    produces=("copy-latency", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
NOT_MEASURED = "not-measured-yet"


@dataclass(frozen=True)
class LatencyObservation:
    """One case where somebody's entry could be compared with a later price."""

    venue_id: str
    symbol: str
    side: str
    their_price: float
    our_price: float
    delay_seconds: float
    adverse_move_fraction: float
    observed_at_ns: int

    @property
    def moved_against_the_copier(self) -> bool:
        return self.adverse_move_fraction > 0.0


@dataclass
class LatencyStanding:
    observations: int = 0
    adverse_cases: int = 0
    favourable_cases: int = 0
    symbols_measured: int = 0
    unusable_observations: int = 0


class CopyLatencyEstimator:
    """Measures, per venue and symbol, what the copying delay costs."""

    def __init__(
        self,
        window: int,
        prior_delay_seconds: float,
        prior_adverse_move_fraction: float,
        adverse_quantile: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if window < 2:
            raise ValueError("a quantile over fewer than two observations is one number")
        if prior_delay_seconds <= 0:
            raise ValueError(
                "the prior delay is what is believed before measurement, and copying "
                "with zero delay is not a thing that happens"
            )
        if not 0.0 < adverse_quantile < 1.0:
            raise ValueError("the adverse quantile is inside (0, 1)")
        if minimum_observations < 1:
            raise ValueError("an estimate needs at least one observation to be fitted")
        self._window = window
        self._prior_delay = prior_delay_seconds
        self._prior_adverse = prior_adverse_move_fraction
        self._adverse_quantile = adverse_quantile
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        self._delays: dict[tuple, QuantileEstimator] = {}
        self._adverse: dict[tuple, QuantileEstimator] = {}
        self._counts: dict[tuple, int] = {}
        self.standing = LatencyStanding()

    def observe(
        self, venue_id: str, symbol: str, side: str, their_price: float,
        our_price: float, delay_seconds: float,
    ) -> LatencyObservation | None:
        """One end-to-end case: their fill price, ours, and the gap between them."""
        if their_price <= 0 or our_price <= 0 or delay_seconds < 0:
            self.standing.unusable_observations += 1
            return None

        # Adverse means worse for the copier, which depends on the direction.
        if side == "long":
            adverse = (our_price - their_price) / their_price
        elif side == "short":
            adverse = (their_price - our_price) / their_price
        else:
            self.standing.unusable_observations += 1
            return None

        key = (venue_id, symbol)
        if key not in self._counts:
            self.standing.symbols_measured += 1
        self._delays.setdefault(
            key, QuantileEstimator(window=self._window, prior=self._prior_delay)
        ).observe(delay_seconds)
        # Favourable cases are kept in the sample -- dropping them would measure the
        # cost of the bad half only -- but they are counted separately so the shape
        # of the distribution stays visible.
        self._adverse.setdefault(
            key, QuantileEstimator(window=self._window, prior=self._prior_adverse)
        ).observe(adverse)
        self._counts[key] = self._counts.get(key, 0) + 1

        self.standing.observations += 1
        if adverse > 0:
            self.standing.adverse_cases += 1
        else:
            self.standing.favourable_cases += 1

        return LatencyObservation(
            venue_id=venue_id, symbol=symbol, side=side, their_price=their_price,
            our_price=our_price, delay_seconds=delay_seconds,
            adverse_move_fraction=adverse, observed_at_ns=self._now_ns(),
        )

    def estimate(self, venue_id: str, symbol: str) -> CopyLatency:
        key = (venue_id, symbol)
        count = self._counts.get(key, 0)
        delay = (
            self._delays[key].estimate(0.5, self._minimum_observations)
            if key in self._delays
            else None
        )
        # The adverse move is taken at an upper quantile rather than the median:
        # the cost of copying is set by the cases that moved against, not by the
        # typical case, and sizing against the median is sizing against the half
        # of the distribution that never hurt.
        adverse = (
            self._adverse[key].estimate(self._adverse_quantile, self._minimum_observations)
            if key in self._adverse
            else None
        )
        return CopyLatency(
            venue_id=venue_id,
            symbol=symbol,
            detection_delay_seconds=delay.value if delay else self._prior_delay,
            adverse_move_fraction=adverse.value if adverse else self._prior_adverse,
            observations=count,
            is_fitted=bool(delay and delay.is_fitted and adverse and adverse.is_fitted),
            measured_at_ns=self._now_ns(),
        )

    def symbols_measured(self) -> tuple:
        return tuple(sorted(self._counts))


def describe_latency_estimation(estimator: CopyLatencyEstimator) -> dict:
    return {
        "part_id": PART_ID,
        "observations": estimator.standing.observations,
        "cases_that_moved_against_the_copier": estimator.standing.adverse_cases,
        "cases_that_moved_in_favour": estimator.standing.favourable_cases,
        "symbols_measured": estimator.standing.symbols_measured,
        "unusable_observations": estimator.standing.unusable_observations,
        "uses_one_global_latency": False,
        "drops_favourable_cases_from_the_sample": False,
    }


def run_copy_latency_estimator(
    estimator: CopyLatencyEstimator, control_socket, read_pairs, publish_latency,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for venue_id, symbol in read_pairs(estimator):
            publish_latency(estimator.estimate(venue_id, symbol))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
