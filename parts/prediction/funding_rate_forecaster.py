"""funding-rate-forecaster: what the next settlement will charge, from the mechanism.

Funding is not a price to be predicted from its own history -- it is computed from
one, by a formula the venue publishes. So this part forecasts it the way the venue
calculates it rather than by fitting a curve to past rates:

    funding = clamp(premium_index_average + clamp(interest - premium, ±0.05%), ±cap)

The premium index is the running average of how far the perpetual has traded from
its index price. That is observable in real time, which means most of the next
settlement's funding is already determined by the time this part runs, and a
model fitted to past *rates* would be learning a lagged version of something it
can compute directly.

**The forecast tightens as the settlement approaches**, and it says how much of
the averaging window is already in the past. A funding forecast an hour before
settlement is a different object from one a minute before, and a part that
reported both the same way would let a bot size on the first as though it were
the second.

**The venue's own parameters, never assumed.** Interval, cap and interest rate
differ between venues and change; they arrive as venue facts and a symbol with
none produces no forecast.

**Predicted funding is a cost, not a signal.** It goes to the bots as carry, and
the direction it implies for price is a different claim this part does not make.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "funding-rate-forecaster"

PART_DECLARATION = PartDeclaration(
    part_id="funding-rate-forecaster",
    consumes=("market-data",),
    produces=("funding-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FORECAST = "forecast"
NO_VENUE_PARAMETERS = "this-venue's-funding-formula-parameters-are-not-known"
NO_PREMIUM_OBSERVATIONS = "no-premium-index-observations-for-this-symbol"


@dataclass(frozen=True)
class FundingParameters:
    """A venue's own funding formula, as the venue documents it. Never assumed."""

    venue_id: str
    interval_seconds: float
    cap: float
    interest_rate_per_interval: float
    premium_clamp: float
    averaging_window_seconds: float


@dataclass(frozen=True)
class FundingForecast:
    """The next settlement's rate, and how much of it is already determined."""

    venue_id: str
    symbol: str
    state: str
    predicted_rate: float | None
    premium_average: float | None
    fraction_of_window_elapsed: float | None
    seconds_to_settlement: float | None
    was_capped: bool
    observations: int
    reason: str
    forecast_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == FORECAST and self.predicted_rate is not None

    @property
    def is_nearly_settled(self) -> bool:
        """Most of the averaging window is past, so little can still change it."""
        return (
            self.fraction_of_window_elapsed is not None
            and self.fraction_of_window_elapsed >= 0.9
        )


@dataclass
class ForecasterStanding:
    forecasts_made: int = 0
    forecasts_produced: int = 0
    refused_no_parameters: int = 0
    refused_no_observations: int = 0
    premium_observations: int = 0
    capped_forecasts: int = 0
    largest_predicted: float | None = None
    symbols_tracked: int = 0


class FundingRateForecaster:
    """Computes the next funding rate the way the venue does, from live premiums."""

    def __init__(
        self,
        premium_window_observations: int,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_observations < 2:
            raise ValueError("an average over one premium observation is that observation")
        self._window = premium_window_observations
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._parameters: dict[str, FundingParameters] = {}
        self._premiums: dict[tuple[str, str], RollingWindow] = {}
        self._next_settlement: dict[tuple[str, str], int] = {}
        self.standing = ForecasterStanding()

    def observe_venue_parameters(self, parameters: FundingParameters) -> None:
        self._parameters[parameters.venue_id] = parameters

    def observe_premium(self, venue_id: str, symbol: str, mark_price: float, index_price: float) -> None:
        """One premium observation: how far the perpetual trades from its index."""
        if index_price <= 0:
            return
        self.standing.premium_observations += 1
        key = (venue_id, symbol)
        window = self._premiums.get(key)
        if window is None:
            window = RollingWindow(length=self._window)
            self._premiums[key] = window
        window.observe((mark_price - index_price) / index_price)
        self.standing.symbols_tracked = len(self._premiums)

    def observe_next_settlement(self, venue_id: str, symbol: str, at_ns: int) -> None:
        self._next_settlement[(venue_id, symbol)] = at_ns

    def forecast(self, venue_id: str, symbol: str) -> FundingForecast:
        self.standing.forecasts_made += 1
        parameters = self._parameters.get(venue_id)

        if parameters is None:
            self.standing.refused_no_parameters += 1
            return self._forecast(
                venue_id, symbol, NO_VENUE_PARAMETERS, None, None, None, None, False, 0,
                f"the funding formula's interval, cap and interest rate for {venue_id} are "
                f"not known here; they differ between venues and change, so they are read "
                f"rather than assumed",
            )

        window = self._premiums.get((venue_id, symbol))
        if window is None or window.count < self._minimum:
            self.standing.refused_no_observations += 1
            return self._forecast(
                venue_id, symbol, NO_PREMIUM_OBSERVATIONS, None, None, None, None, False,
                0 if window is None else window.count,
                f"{0 if window is None else window.count} premium observation(s) of the "
                f"{self._minimum} needed; funding is computed from the premium index, not "
                f"fitted to past rates",
            )

        premium_average = window.mean(self._minimum)

        # The venue's formula: the premium average plus the clamped difference
        # between the interest rate and the premium, then capped.
        interest_component = max(
            -parameters.premium_clamp,
            min(parameters.premium_clamp, parameters.interest_rate_per_interval - premium_average),
        )
        rate = premium_average + interest_component
        capped = abs(rate) > parameters.cap
        if capped:
            rate = parameters.cap if rate > 0 else -parameters.cap
            self.standing.capped_forecasts += 1

        settlement = self._next_settlement.get((venue_id, symbol))
        seconds_to_settlement = (
            None if settlement is None else max(0.0, (settlement - self._now_ns()) / 1e9)
        )
        elapsed = (
            None
            if seconds_to_settlement is None or parameters.averaging_window_seconds <= 0
            else min(
                1.0,
                max(
                    0.0,
                    1.0 - seconds_to_settlement / parameters.averaging_window_seconds,
                ),
            )
        )

        self.standing.forecasts_produced += 1
        if self.standing.largest_predicted is None or abs(rate) > abs(self.standing.largest_predicted):
            self.standing.largest_predicted = rate

        return self._forecast(
            venue_id, symbol, FORECAST, rate, premium_average, elapsed, seconds_to_settlement,
            capped, window.count,
            f"the premium index has averaged {premium_average:+.5%} over {window.count} "
            f"observation(s), which with {venue_id}'s "
            f"{parameters.interest_rate_per_interval:+.5%} interest component gives "
            f"{rate:+.5%} at the next settlement"
            + (f", capped at {parameters.cap:.3%}" if capped else "")
            + (
                f". {elapsed:.0%} of the averaging window is already past, so that much of "
                f"this is determined"
                if elapsed is not None
                else ". The settlement time is unknown, so how much of this is already "
                "determined cannot be said"
            )
            + ". This is a cost to whichever side pays it, not a claim about direction",
        )

    def _forecast(
        self, venue_id, symbol, state, rate, premium, elapsed, seconds, capped, observations, reason
    ) -> FundingForecast:
        return FundingForecast(
            venue_id=venue_id,
            symbol=symbol,
            state=state,
            predicted_rate=rate,
            premium_average=premium,
            fraction_of_window_elapsed=elapsed,
            seconds_to_settlement=seconds,
            was_capped=capped,
            observations=observations,
            reason=reason,
            forecast_at_ns=self._now_ns(),
        )


def describe_funding_forecasting(forecaster: FundingRateForecaster) -> dict:
    return {
        "part_id": PART_ID,
        "venues_with_known_parameters": sorted(forecaster._parameters),
        "forecasts_made": forecaster.standing.forecasts_made,
        "forecasts_produced": forecaster.standing.forecasts_produced,
        "refused_no_venue_parameters": forecaster.standing.refused_no_parameters,
        "refused_no_premium_observations": forecaster.standing.refused_no_observations,
        "premium_observations": forecaster.standing.premium_observations,
        "forecasts_hitting_the_cap": forecaster.standing.capped_forecasts,
        "largest_predicted_rate": forecaster.standing.largest_predicted,
        "symbols_tracked": forecaster.standing.symbols_tracked,
    }


def run_funding_rate_forecaster(
    forecaster: FundingRateForecaster, control_socket, read_premiums, publish_forecasts,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        symbols = read_premiums(forecaster)
        publish_forecasts(
            tuple(forecaster.forecast(venue_id, symbol) for venue_id, symbol in symbols)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
