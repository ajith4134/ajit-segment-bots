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
    consumes=("market-data", "venue-premium", "symbol-universe"),
    produces=("funding-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FORECAST = "forecast"
NO_SYMBOL_PARAMETERS = "this-symbol's-funding-formula-parameters-are-not-known"
NO_PREMIUM_OBSERVATIONS = "no-premium-index-observations-for-this-symbol"


@dataclass(frozen=True)
class FundingParameters:
    """One symbol's own funding formula, as its venue documents it. Never assumed.

    Per symbol, not per venue: measured 2026-08-28, Binance's own `fundingInfo`
    splits 444 symbols at a four-hour interval, 314 at eight, 2 at one, and its
    `adjustedFundingRateCap` runs 0.003 to 0.02 across symbols on the same read.
    A venue-wide constant would mis-time or mis-cap the forecast for whichever
    symbols sit off the majority value -- wrong invisibly, same failure shape
    `docs/proposals/venue-declared-funding-facts.md` already found and fixed
    for the rate and interval that feed a position's carry.

    `averaging_window_seconds` is not a field here: the premium index is
    averaged over the settlement interval itself, so it is `interval_seconds`,
    not a second unknown neither venue publishes separately.
    """

    venue_id: str
    symbol: str
    interval_seconds: float
    cap: float
    floor: float
    interest_rate_per_interval: float
    premium_clamp: float


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
        self._parameters: dict[tuple[str, str], FundingParameters] = {}
        self._premiums: dict[tuple[str, str], RollingWindow] = {}
        self._next_settlement: dict[tuple[str, str], int] = {}
        self.standing = ForecasterStanding()

    def observe_funding_parameters(self, parameters: FundingParameters) -> None:
        self._parameters[(parameters.venue_id, parameters.symbol)] = parameters

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
        parameters = self._parameters.get((venue_id, symbol))

        if parameters is None:
            self.standing.refused_no_parameters += 1
            return self._forecast(
                venue_id, symbol, NO_SYMBOL_PARAMETERS, None, None, None, None, False, 0,
                f"the funding formula's interval, cap, floor and interest rate for "
                f"{venue_id}|{symbol} are not known here; they differ by symbol and change, "
                f"so they are read rather than assumed",
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
        capped = rate > parameters.cap or rate < parameters.floor
        if capped:
            rate = parameters.cap if rate > parameters.cap else parameters.floor
            self.standing.capped_forecasts += 1

        settlement = self._next_settlement.get((venue_id, symbol))
        seconds_to_settlement = (
            None if settlement is None else max(0.0, (settlement - self._now_ns()) / 1e9)
        )
        # The premium index is averaged over the settlement interval itself, so
        # that is what "how much of this window is already past" is measured
        # against -- not a second, unfetched window length.
        elapsed = (
            None
            if seconds_to_settlement is None or parameters.interval_seconds <= 0
            else min(
                1.0,
                max(
                    0.0,
                    1.0 - seconds_to_settlement / parameters.interval_seconds,
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
            + (
                f", capped at {(parameters.cap if rate == parameters.cap else parameters.floor):.3%}"
                if capped else ""
            )
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
        "symbols_with_known_parameters": len(forecaster._parameters),
        "forecasts_made": forecaster.standing.forecasts_made,
        "forecasts_produced": forecaster.standing.forecasts_produced,
        "refused_no_symbol_parameters": forecaster.standing.refused_no_parameters,
        "refused_no_premium_observations": forecaster.standing.refused_no_observations,
        "premium_observations": forecaster.standing.premium_observations,
        "forecasts_hitting_the_cap": forecaster.standing.capped_forecasts,
        "largest_predicted_rate": forecaster.standing.largest_predicted,
        "symbols_tracked": forecaster.standing.symbols_tracked,
    }


def run_funding_rate_forecaster(
    forecaster: FundingRateForecaster, control_socket, read_premiums, publish_forecasts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_funding_forecasting(forecaster),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The premium a funding rate is averaged from is mark minus index. Neither is
    on `market-data`, which carries trades and candles, and until 2026-08-28
    nothing else carried them either: this part read five million messages, made
    zero forecasts, and recorded zero refusals, because it never reached the code
    that would refuse. Six parts consume `funding-forecast` and none had ever
    seen one.

    `venue-premium-stream-reader` carries them now. `market-data` stays on the
    consumes and is still drained: it is what says a symbol is live at all, and
    dropping an input in the same change that adds one is how a part quietly
    loses a capability nobody was watching.

    `symbol-universe` is read for the venue's own funding-formula facts --
    interval, cap, floor and interest rate -- since 2026-08-29. Before it,
    `observe_funding_parameters` was never called at all: every forecast
    refused `NO_SYMBOL_PARAMETERS`, and `funding_forecast_change` was missing
    on 100% of every bull and bear feature vector. `symbol-catalogue-reader`
    already fetches all four in the same round-trip it uses for the rate and
    interval `venue-declared-funding-facts.md` wired in; nothing here costs an
    extra request. Only `premium_clamp` is not on that wire -- neither venue
    publishes it via any endpoint read in this codebase -- so it is the one
    setting-sourced constant, from this part's own documented formula above.
    """
    # `market-data` carries trades AND candles: venue-trade-stream-reader
    # publishes the first, ccxt-venue-reader the second, and both have always
    # declared it. This part wants trades and now says so, rather than assuming
    # the wire holds only what it happens to want -- a part that dies on an
    # unexpected shape is a part the wiring can kill.
    from runtime.market_data_stream import trades_in
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("market-data"))
    premiums = Batch(read=context.bus.reader("venue-premium"))
    listings = Batch(read=context.bus.reader("symbol-universe"))
    publish_forecasts = context.bus.publisher_for("funding-forecast")
    forecaster = FundingRateForecaster(
        premium_window_observations=int(context.number("funding_premium_window")),
        minimum_observations=int(context.number("learning_minimum_observations")),
    )
    premium_clamp = context.number("funding_premium_clamp")

    def read_premiums(_forecaster):
        # Every declared symbol this tick, before the premiums: a parameter
        # arriving the same tick as the premium that would first clear the
        # minimum-observations floor must be in place before forecast() runs.
        for listing in listings.payloads():
            if (
                listing.funding_settlements_per_day is None
                or listing.funding_rate_cap is None
                or listing.funding_rate_floor is None
                or listing.funding_interest_rate_per_interval is None
            ):
                # Undeclared for this symbol on this read -- the same fact
                # `bull-feature-builder` already reports as a missing feature,
                # never filled in with a platform default.
                continue
            _forecaster.observe_funding_parameters(
                FundingParameters(
                    venue_id=listing.venue_id,
                    symbol=listing.symbol,
                    interval_seconds=86_400.0 / listing.funding_settlements_per_day,
                    cap=listing.funding_rate_cap,
                    floor=listing.funding_rate_floor,
                    interest_rate_per_interval=listing.funding_interest_rate_per_interval,
                    premium_clamp=premium_clamp,
                )
            )
        # Drained rather than read: this part forecasts from the premium, and the
        # trades are here so an unread wire does not back up behind it.
        trades_in(trades.payloads())
        touched: set[tuple[str, str]] = set()
        for premium in premiums.payloads():
            if premium.mark_price is None or not premium.index_price:
                continue
            _forecaster.observe_premium(
                premium.venue_id, premium.symbol, premium.mark_price, premium.index_price
            )
            touched.add((premium.venue_id, premium.symbol))
        # Only the symbols this tick actually heard about. Forecasting every
        # symbol ever seen on every tick would republish an unchanged level for
        # hundreds of symbols a second, which is the storm of 2026-08-26.
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_forecasts(kept)

    return run_funding_rate_forecaster(
        forecaster=forecaster,
        control_socket=context.control_socket,
        read_premiums=read_premiums,
        publish_forecasts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
