"""tail-move-remaining-estimator: how much of this move is left, which is the whole bet.

Every other bot asks whether a move will happen. This bot only ever joins moves
that are already happening, so its only real question is how much of one is left
-- and a tailgater that cannot answer it is a momentum bot that buys highs.

Three independent estimates, deliberately not one:

- **From this symbol's own recorded moves.** How far moves in this symbol
  normally go, minus how far this one has gone. Empirical, assumption-free, and
  blind to anything unusual about today.
- **From the price forecast.** Another part's view of where price is going,
  which knows about today and nothing about this symbol's history of moves.
- **From decay.** Moves lose speed before they reverse, so the recent rate of
  progress extrapolated forward is a third view that needs neither history nor
  a forecast.

**They are reported separately and combined by agreement, never averaged.** An
average of three estimates that disagree is a number no evidence supports. When
they disagree the estimate says so and widens, and the parts below can see that
this is a move nobody can size rather than a move with a middling amount left.

**Nothing here extrapolates past what has been observed.** An estimate that says
a move has more left than any move in this symbol has ever covered is not
optimism; it is the model having left the data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    ESTIMATES_AGREE as AGREE, ESTIMATES_DISAGREE as DISAGREE, FROM_DECAY, FROM_FORECAST,
    FROM_HISTORY, NO_ESTIMATE_AVAILABLE as NONE_AVAILABLE, ONLY_ONE_ESTIMATE as ONLY_ONE,
    MoveRemaining,
)
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, linear_fit

PART_ID = "tail-move-remaining-estimator"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-move-remaining-estimator",
    consumes=("follow-candidate", "market-data", "price-forecast"),
    produces=("move-remaining", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

@dataclass
class EstimatorStanding:
    estimates_made: int = 0
    agreements: int = 0
    disagreements: int = 0
    single_view: int = 0
    nothing_available: int = 0
    clamped_to_observed: int = 0
    by_source: dict = field(default_factory=dict)


class TailMoveRemainingEstimator:
    """Estimates what is left of a move three ways, and reports when they disagree."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        move_quantile: float,
        move_window: int,
        prior_normal_move_fraction: float,
        agreement_fraction: float,
        decay_lookback: int,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < agreement_fraction <= 1.0:
            raise ValueError(
                "agreement is how far apart the estimates may sit as a fraction of the largest; "
                "it must be inside (0, 1]"
            )
        if decay_lookback < 3:
            raise ValueError("a rate of progress needs at least three observations to have a slope")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._move_quantile = move_quantile
        self._move_window = move_window
        self._prior_normal_move = prior_normal_move_fraction
        self._agreement_fraction = agreement_fraction
        self._decay_lookback = decay_lookback
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._normal_moves: dict[tuple[str, str], QuantileEstimator] = {}
        self._largest_observed: dict[tuple[str, str], float] = {}
        self._forecasts: dict[tuple[str, str], float] = {}
        self.standing = EstimatorStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._prices[key] = window
        window.observe(price, at_ns)

    def observe_completed_move(self, venue_id: str, symbol: str, move_fraction: float) -> None:
        key = (venue_id, symbol)
        self._normal_move_for(key).observe(abs(move_fraction))
        self._largest_observed[key] = max(
            self._largest_observed.get(key, 0.0), abs(move_fraction)
        )

    def observe_price_forecast(self, venue_id: str, symbol: str, expected_return: float) -> None:
        self._forecasts[(venue_id, symbol)] = expected_return

    def estimate(self, candidate) -> MoveRemaining:
        self.standing.estimates_made += 1
        key = (candidate.venue_id, candidate.symbol)
        estimates: dict[str, float] = {}

        historical = self._from_history(key, candidate)
        if historical is not None:
            estimates[FROM_HISTORY] = historical

        forecast = self._from_forecast(key, candidate)
        if forecast is not None:
            estimates[FROM_FORECAST] = forecast

        decay = self._from_decay(key, candidate)
        if decay is not None:
            estimates[FROM_DECAY] = decay

        for source in estimates:
            self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1

        if not estimates:
            self.standing.nothing_available += 1
            return self._reading(
                candidate, None, None, None, {}, NONE_AVAILABLE,
                "none of the three views could be formed: no recorded moves, no forecast and "
                "too little of this move observed to read its rate",
            )

        lowest = min(estimates.values())
        highest = max(estimates.values())

        if len(estimates) == 1:
            self.standing.single_view += 1
            only = next(iter(estimates))
            return self._reading(
                candidate, lowest, lowest, highest, estimates, ONLY_ONE,
                f"only {only} could be formed, putting {highest:.2%} left; a single view cannot "
                f"be checked against anything, so what follows should treat it as such",
            )

        disagreement = (highest - lowest) / highest if highest > 0 else 1.0
        if disagreement > self._agreement_fraction:
            self.standing.disagreements += 1
            return self._reading(
                candidate, lowest, lowest, highest, estimates, DISAGREE,
                f"the views disagree: {self._describe(estimates)}. They are "
                f"{disagreement:.0%} apart, past the {self._agreement_fraction:.0%} that counts "
                f"as agreement, so the smallest is reported and this is a move nobody can size "
                f"rather than a move with a middling amount left",
            )

        self.standing.agreements += 1
        combined = sum(estimates.values()) / len(estimates)
        combined, clamped = self._clamp_to_observed(key, combined)
        return self._reading(
            candidate, combined, lowest, highest, estimates, AGREE,
            f"{self._describe(estimates)}; they agree within {disagreement:.0%}, so "
            f"{combined:.2%} is left"
            + (
                "; clamped to the largest move ever recorded in this symbol, because an "
                "estimate past that is the model having left the data"
                if clamped
                else ""
            ),
        )

    def _from_history(self, key, candidate) -> float | None:
        normal = self._normal_move_for(key).estimate(
            self._move_quantile, minimum_observations=self._minimum
        )
        if not normal.is_fitted:
            return None
        return max(0.0, normal.value - candidate.move_so_far)

    def _from_forecast(self, key, candidate) -> float | None:
        forecast = self._forecasts.get(key)
        if forecast is None:
            return None
        # The forecast is signed in price terms; what matters is how much of it
        # points the way this move is already going.
        signed = forecast if candidate.direction == "long" else -forecast
        return max(0.0, signed)

    def _from_decay(self, key, candidate) -> float | None:
        """Extrapolate the move's own rate of progress, which fades before it turns."""
        window = self._prices.get(key)
        if window is None or window.count < self._decay_lookback:
            return None
        series = list(window.values)[-self._decay_lookback :]
        if series[0] <= 0:
            return None
        points = [(float(index), value / series[0] - 1.0) for index, value in enumerate(series)]
        fit = linear_fit(points)
        if fit is None:
            return None
        slope, _ = fit
        signed_slope = slope if candidate.direction == "long" else -slope
        if signed_slope <= 0:
            return 0.0
        # Progress decays: the remaining move is what the current rate delivers
        # over the same span again, halving. A move that has stopped making
        # progress has nothing left, which is what a zero slope gives.
        return signed_slope * self._decay_lookback / 2.0

    def _clamp_to_observed(self, key, value: float) -> tuple[float, bool]:
        largest = self._largest_observed.get(key)
        if largest is None or value <= largest:
            return value, False
        self.standing.clamped_to_observed += 1
        return largest, True

    def _describe(self, estimates: dict) -> str:
        return ", ".join(
            f"{source} says {value:.2%}" for source, value in sorted(estimates.items())
        )

    def _normal_move_for(self, key) -> QuantileEstimator:
        estimator = self._normal_moves.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._move_window, prior=self._prior_normal_move)
            self._normal_moves[key] = estimator
        return estimator

    def _reading(
        self, candidate, remaining, lowest, highest, estimates, agreement, reason
    ) -> MoveRemaining:
        return MoveRemaining(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            direction=candidate.direction,
            remaining_fraction=remaining,
            lowest=lowest,
            highest=highest,
            estimates=dict(estimates),
            agreement=agreement,
            reason=reason,
            estimated_at_ns=self._now_ns(),
        )


def describe_move_remaining(estimator: TailMoveRemainingEstimator) -> dict:
    return {
        "part_id": PART_ID,
        "estimates_made": estimator.standing.estimates_made,
        "views_agreed": estimator.standing.agreements,
        "views_disagreed": estimator.standing.disagreements,
        "single_view_only": estimator.standing.single_view,
        "nothing_available": estimator.standing.nothing_available,
        "clamped_to_the_largest_observed_move": estimator.standing.clamped_to_observed,
        "by_source": dict(sorted(estimator.standing.by_source.items())),
        "symbols_with_recorded_moves": len(estimator._normal_moves),
    }


def run_tail_move_remaining_estimator(
    estimator: TailMoveRemainingEstimator, control_socket, read_candidates_and_market,
    publish_readings, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = read_candidates_and_market(estimator)
        publish_readings(tuple(estimator.estimate(candidate) for candidate in candidates))

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
    from runtime.input_assembly import Batch

    candidates = Batch(read=context.bus.reader("follow-candidate"))
    trades = Batch(read=context.bus.reader("market-data"))
    forecasts = Batch(read=context.bus.reader("price-forecast"))
    publish_readings = context.bus.publisher_for("move-remaining")
    estimator = TailMoveRemainingEstimator(
        window_length=int(context.number("tail_window_length")),
        minimum_observations=int(context.number("tail_minimum_observations_in_move")),
        move_quantile=context.number("tail_move_quantile"),
        move_window=int(context.number("tail_move_window")),
        prior_normal_move_fraction=context.number("tail_prior_normal_move_fraction"),
        agreement_fraction=context.number("tail_agreement_fraction"),
        decay_lookback=int(context.number("tail_decay_lookback")),
    )

    def read_candidates_and_market(_estimator):
        for trade in trades.payloads():
            estimator.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
            )
        for forecast in forecasts.payloads():
            if forecast.expected_return is not None:
                estimator.observe_price_forecast(forecast.venue_id, forecast.symbol, forecast.expected_return)
        return tuple(candidates.payloads())

    def publish(items) -> None:
        if items:
            publish_readings(items)

    return run_tail_move_remaining_estimator(
        estimator=estimator,
        control_socket=context.control_socket,
        read_candidates_and_market=read_candidates_and_market,
        publish_readings=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
