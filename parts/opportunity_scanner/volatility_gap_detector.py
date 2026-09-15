"""volatility-gap-detector: forecast volatility disagreeing with the option market.

Two independent estimates of the same future quantity. Realised-volatility
forecasting says what this symbol has been doing and tends to keep doing; implied
volatility says what option buyers are paying to be protected against. When they
disagree materially, one of them is wrong and the gap is the trade.

**The direction of the gap decides the trade, and they are not symmetric.**
Implied far above forecast means protection is expensive -- the crowd is paying
for a move the series does not support. Implied far below forecast means a move
is coming that nobody is priced for, which is the more dangerous side to be short.

This is the one detector that needs an options market to exist. Phase 1 captures
no options data, so it is honest about that: with no surface, it produces
nothing and says why, rather than falling back to a realised-only signal that
would be a completely different strategy wearing this one's name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.market_signal import (
    LONG,
    REVERSION,
    SHORT,
    SignalCalibrator,
    make_candidate,
    settle_claims_from,
)
from runtime.forecast_types import annualise
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.underlying_of_a_trading_symbol import underlying_of_a_trading_symbol

PART_ID = "volatility-gap-detector"

PART_DECLARATION = PartDeclaration(
    part_id="volatility-gap-detector",
    consumes=(
        "volatility-forecast",
        "implied-vol-surface",
        "playbook-rule",
        "options-flow",
        "training-label",
    ),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NO_SURFACE = "no-implied-volatility-surface"
NO_FORECAST = "no-volatility-forecast"
GAP_TOO_SMALL = "gap-inside-the-noise-of-both-estimates"

IMPLIED_RICH = "implied-rich"
IMPLIED_CHEAP = "implied-cheap"


@dataclass
class GapStanding:
    tests: int = 0
    candidates: int = 0
    no_surface: int = 0
    no_forecast: int = 0
    gap_too_small: int = 0
    implied_rich: int = 0
    implied_cheap: int = 0
    outcomes_learned: int = 0
    largest_gap: float = 0.0


class VolatilityGapDetector:
    """Fires when implied and forecast volatility disagree by more than either's noise."""

    def __init__(
        self,
        minimum_gap_fraction: float,
        horizon_seconds: float,
        seconds_per_year: float,
        calibrator: SignalCalibrator,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_gap_fraction <= 0:
            raise ValueError("a gap threshold of zero fires on rounding differences")
        self._minimum_gap = minimum_gap_fraction
        self._horizon = horizon_seconds
        if seconds_per_year <= 0:
            raise ValueError("a year with no trading seconds cannot annualise anything")
        self._seconds_per_year = seconds_per_year
        self._calibrator = calibrator
        self._now_ns = now_ns
        self._forecast: dict[tuple[str, str], float] = {}
        self._implied: dict[tuple[str, str], float] = {}
        self.standing = GapStanding()

    def observe_forecast(
        self, venue_id: str, symbol: str, volatility: float, horizon_seconds: float,
    ) -> None:
        """A volatility over `horizon_seconds`, held as a year's so it meets implied."""
        if horizon_seconds <= 0:
            return
        self._forecast[(venue_id, symbol)] = annualise(volatility, horizon_seconds, self._seconds_per_year)

    def observe_implied(self, venue_id: str, symbol: str, implied_volatility: float) -> None:
        """From an options surface. Phase 1 captures none, so this is usually absent."""
        self._implied[(venue_id, symbol)] = implied_volatility

    def observe_outcome(self, calibration_key: str, gap_closed: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, calibration_key, gap_closed)
        self.standing.outcomes_learned += 1

    def detect(self, venue_id: str, symbol: str, regime_name: str = "any") -> tuple[object | None, str]:
        self.standing.tests += 1
        key = (venue_id, symbol)
        forecast = self._forecast.get(key)
        implied = self._implied.get(key)

        if forecast is None or forecast <= 0:
            self.standing.no_forecast += 1
            return None, NO_FORECAST

        if implied is None or implied <= 0:
            # No options market for this symbol. Falling back to a realised-only
            # signal would be a different strategy wearing this one's name.
            self.standing.no_surface += 1
            return None, NO_SURFACE

        # Both are a year's: the forecast was annualised as it arrived, over its own
        # horizon, because Upstox's implied volatility is a year's. Compared raw on 2026-09-15, implied read hundreds of times the
        # forecast on every test -- 17,242 of 17,242 candidates "implied rich",
        # largest gap 718 -- so every candidate this part had ever raised was the
        # unit mismatch. Annualised over trading seconds, because implied variance
        # is realised while the market trades and the forecast is of trading time.
        gap = (implied - forecast) / forecast
        if abs(gap) < self._minimum_gap:
            self.standing.gap_too_small += 1
            return None, GAP_TOO_SMALL

        self.standing.largest_gap = max(self.standing.largest_gap, abs(gap))
        self.standing.candidates += 1

        if gap > 0:
            # Protection is expensive against what the series actually does:
            # the crowd is paying for a move the history does not support.
            state = IMPLIED_RICH
            direction = SHORT
            self.standing.implied_rich += 1
        else:
            # A move is coming that nobody is priced for. The more dangerous side.
            state = IMPLIED_CHEAP
            direction = LONG
            self.standing.implied_cheap += 1

        confidence = self._calibrator.confidence(PART_ID, regime_name)
        return (
            make_candidate(
                detector=PART_ID,
                venue_id=venue_id,
                symbol=symbol,
                direction=direction,
                expectation=REVERSION,
                signal_strength=abs(gap),
                confidence=confidence,
                calibration_key=regime_name,
                horizon_seconds=self._horizon,
                evidence={
                    "implied_volatility": implied,
                    "forecast_volatility": forecast,
                    "gap_fraction": gap,
                    "state": state,
                    "trades_volatility_not_direction": True,
                },
                reason=(
                    f"implied volatility of {implied:.2%} is {abs(gap):.0%} "
                    f"{'above' if gap > 0 else 'below'} the {forecast:.2%} forecast, so "
                    f"{'protection is expensive against what this series actually does' if gap > 0 else 'a move is coming that nobody is priced for'}. "
                    f"The gap closed {confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_volatility_gaps(detector: VolatilityGapDetector) -> dict:
    return {
        "part_id": PART_ID,
        "tests": detector.standing.tests,
        "candidates": detector.standing.candidates,
        "no_implied_surface": detector.standing.no_surface,
        "no_forecast": detector.standing.no_forecast,
        "gap_too_small": detector.standing.gap_too_small,
        "implied_rich": detector.standing.implied_rich,
        "implied_cheap": detector.standing.implied_cheap,
        "outcomes_learned": detector.standing.outcomes_learned,
        "largest_gap": detector.standing.largest_gap,
    }


def run_volatility_gap_detector(
    detector: VolatilityGapDetector, control_socket, read_volatility, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_volatility(detector)
        candidates = []
        for venue_id, symbol in symbols:
            candidate, _ = detector.detect(venue_id, symbol)
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
        read_standing=lambda: describe_volatility_gaps(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Realised-volatility forecasts per symbol against the implied surface's
    at-the-money level for the same underlying. Options flow and playbook
    rules are read and not yet used to shade the gap: no options feed runs in
    phase 1.
    """
    from runtime.input_assembly import Batch

    forecasts = Batch(read=context.bus.reader("volatility-forecast"))
    surfaces = Batch(read=context.bus.reader("implied-vol-surface"))
    rules = Batch(read=context.bus.reader("playbook-rule"))
    flows = Batch(read=context.bus.reader("options-flow"))
    # The record of whether this detector was right, back from
    # `signal-outcome-labeller`. Until 2026-08-28 nothing carried it here and
    # `observe_outcome` had never been called by anything that runs.
    labels = Batch(read=context.bus.reader("training-label"))
    publish_candidates = context.bus.publisher_for("entry-candidate")
    detector = VolatilityGapDetector(
        minimum_gap_fraction=context.number("volatility_gap_minimum_fraction"),
        horizon_seconds=context.number("volatility_gap_horizon"),
        seconds_per_year=context.number("implied_volatility_trading_seconds_per_year"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
    )
    symbols_of: dict[tuple[str, str], set[str]] = {}

    def read_volatility(_detector):
        # Whichever of this detector's own claims the market has settled since
        # the last tick. Drained first, so a candidate raised below is priced by
        # the record including everything already known -- a claim settled this
        # tick and used next tick would make the confidence one tick stale for
        # no reason.
        settle_claims_from(labels.payloads(), detector, PART_ID)
        rules.payloads()
        flows.payloads()
        touched = set()
        for forecast in forecasts.payloads():
            if forecast.expected_volatility is None:
                continue
            detector.observe_forecast(
                forecast.venue_id, forecast.symbol, forecast.expected_volatility,
                forecast.horizon_seconds,
            )
            touched.add((forecast.venue_id, forecast.symbol))
            # The underlying this forecast's symbol is a claim on, so the
            # implied-vol surface below -- which is keyed by the underlying --
            # finds the symbols it should be recorded against.
            #
            # This was `forecast.symbol.rstrip("USDT")` until 2026-09-12.
            # `rstrip` is not "remove this suffix": it strips every trailing
            # character in the set, so it removed any run of U, S, D and T. It
            # gave "BTC" for "BTCUSDT" and mangled 39 of the 210 NSE F&O stock
            # underlyings -- LT to L, HAVELLS to HAVELL, ADANIENT to ADANIEN.
            # A mangled key does not raise, it just never matches, so those
            # names silently had no implied volatility recorded at all.
            symbols_of.setdefault(
                (forecast.venue_id, underlying_of_a_trading_symbol(forecast.symbol)), set()
            ).add(forecast.symbol)
        for surface in surfaces.payloads():
            at_the_money = surface.at_the_money if isinstance(surface.at_the_money, dict) else {}
            nearest = min(at_the_money.items(), default=(None, None))[1] if at_the_money else None
            if nearest is None:
                continue
            for symbol in symbols_of.get((surface.venue_id, surface.underlying), ()):
                detector.observe_implied(surface.venue_id, symbol, float(nearest))
                touched.add((surface.venue_id, symbol))
        return tuple(sorted(touched))

    def publish(candidates) -> None:
        if candidates:
            publish_candidates(candidates)

    return run_volatility_gap_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_volatility=read_volatility,
        publish_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
