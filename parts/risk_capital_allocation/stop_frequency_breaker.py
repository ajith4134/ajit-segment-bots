"""stop-frequency-breaker: cut the limit when stops cluster, before equity shows it.

A run of stop-outs says the strategy has stopped working *before* the equity
curve does. Each individual loss is inside its risk budget -- that is what the
stop is for -- so a drawdown breaker sized to catch real damage will not fire
until several more have happened. This one fires on the pattern instead.

What makes it more than a counter: **the threshold is learned** (RL-060). How
often this strategy ordinarily stops out is a property of the strategy and the
regime, not a number anyone can state in advance. A strategy that stops out four
times in ten by design is healthy at a rate that would be alarming for one that
stops out twice in a hundred, and a fixed threshold is wrong for one of them.

So it measures its own long-run stop rate and fires when a recent window is far
enough above it to be unlikely as chance -- and until it has enough history to
know its own rate, it uses the operator's prior and says that is what it is doing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "stop-frequency-breaker"

PART_DECLARATION = PartDeclaration(
    part_id="stop-frequency-breaker",
    consumes=("closed-trade",),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

STOPPED_OUT = "stopped-out"

TRADING = "trading"
TRIPPED = "tripped"


@dataclass
class FrequencyStanding:
    trades_seen: int = 0
    stops_seen: int = 0
    trips: int = 0
    releases: int = 0
    window_stop_rate: float = 0.0
    baseline_stop_rate: float = 0.0
    state: str = TRADING
    trades_since_trip: int = 0


class StopFrequencyBreaker:
    """Fires when the recent stop rate is improbably high against this strategy's own."""

    def __init__(
        self,
        window_trades: int,
        prior_stop_rate: float,
        excess_ratio: float,
        minimum_observations: int,
        cooldown_trades: int,
        allowed_fraction_when_trading: float,
        half_life_observations: float = 200.0,
        now_ns=time.time_ns,
    ) -> None:
        if window_trades < 2:
            raise ValueError("a rate needs at least two trades to be a rate")
        if excess_ratio <= 1.0:
            raise ValueError("the excess ratio must be above 1: at 1 every ordinary run trips it")
        self._window = window_trades
        self._excess_ratio = excess_ratio
        self._minimum_observations = minimum_observations
        self._cooldown = cooldown_trades
        self._allowed = allowed_fraction_when_trading
        self._now_ns = now_ns
        self._baseline = RateEstimator(
            prior=prior_stop_rate, prior_weight=5.0, half_life_observations=half_life_observations
        )
        self._recent: list[bool] = []
        self.standing = FrequencyStanding()

    def observe_closed_trade(self, was_stopped_out: bool) -> RiskLimit:
        """One finished trade; returns the limit that follows."""
        self.standing.trades_seen += 1
        if was_stopped_out:
            self.standing.stops_seen += 1
        # A trade only joins the baseline once it has fallen out of the recent
        # window. Otherwise the cluster being detected teaches the baseline that
        # clusters are normal, and the breaker can never fire -- measured: ten
        # consecutive stop-outs produced a baseline of 100% and a threshold of
        # 100%, so the worst possible run compared as ordinary.
        self._recent.append(was_stopped_out)
        while len(self._recent) > self._window:
            self._baseline.observe(self._recent.pop(0))

        if self.standing.state == TRIPPED:
            self.standing.trades_since_trip += 1
            if self.standing.trades_since_trip >= self._cooldown:
                self.standing.state = TRADING
                self.standing.releases += 1
                self.standing.trades_since_trip = 0
                return self._limit(
                    self._allowed, f"{self._cooldown} trades have passed since the last cluster"
                )
            return self._limit(
                NO_RISK_ALLOWED,
                f"cooling down: {self.standing.trades_since_trip} of {self._cooldown} trades",
            )

        baseline = self._baseline_estimate()
        window_rate = sum(1 for stopped in self._recent if stopped) / len(self._recent)
        self.standing.window_stop_rate = window_rate
        self.standing.baseline_stop_rate = baseline.value

        if len(self._recent) < self._window:
            return self._limit(
                self._allowed,
                f"{len(self._recent)} of {self._window} trades in the window so far",
            )

        threshold = min(1.0, baseline.value * self._excess_ratio)
        if window_rate > threshold:
            self.standing.state = TRIPPED
            self.standing.trips += 1
            self.standing.trades_since_trip = 0
            return self._limit(
                NO_RISK_ALLOWED,
                f"{window_rate:.0%} of the last {self._window} trades were stopped out, against "
                f"a {'learned' if baseline.is_fitted else 'prior'} rate of {baseline.value:.0%} "
                f"and a threshold of {threshold:.0%}",
            )

        return self._limit(
            self._allowed,
            f"stop rate {window_rate:.0%} against a threshold of {threshold:.0%}",
        )

    def _baseline_estimate(self) -> Estimate:
        return self._baseline.estimate(self._minimum_observations)

    def _limit(self, fraction: float, reason: str) -> RiskLimit:
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=fraction,
            reason=reason,
            is_binding=fraction <= NO_RISK_ALLOWED,
            decided_at_ns=self._now_ns(),
        )

    @property
    def is_tripped(self) -> bool:
        return self.standing.state == TRIPPED


def describe_stop_frequency(breaker: StopFrequencyBreaker) -> dict:
    baseline = breaker._baseline_estimate()
    return {
        "part_id": PART_ID,
        "state": breaker.standing.state,
        "trades_seen": breaker.standing.trades_seen,
        "stops_seen": breaker.standing.stops_seen,
        "window_stop_rate": breaker.standing.window_stop_rate,
        "baseline_stop_rate": baseline.value,
        "baseline_is_fitted": baseline.is_fitted,
        "baseline_observations": baseline.observations,
        "trips": breaker.standing.trips,
        "releases": breaker.standing.releases,
    }


def run_stop_frequency_breaker(
    breaker: StopFrequencyBreaker, control_socket, read_closed_trades, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for was_stopped_out in read_closed_trades():
            publish_limit(breaker.observe_closed_trade(was_stopped_out))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
