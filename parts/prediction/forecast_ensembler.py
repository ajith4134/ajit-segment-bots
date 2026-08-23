"""forecast-ensembler: several forecasters into one view, weighted by what each earned.

Prediction produces forecasts from independent sources -- Kronos over candles, a
realised-volatility regressor over historical estimators, an entropy forecaster
over order flow, an implied surface where one exists. This part combines them,
and how it does so is the whole design.

**Weighted by measured accuracy, never equally.** An equal-weighted ensemble is
the average of everything including whatever is broken, and it is most wrong
exactly when one member fails badly. Weights come from `forecast-trust`, which is
measured by the scorer.

**A forecaster with no measured record contributes nothing**, and is listed as
excluded rather than silently dropped. Including it "a little" is how an
unvalidated model reaches the decision it should have been kept out of.

**A flagged forecast is excluded whatever its record.** Out-of-distribution is not
a discount: a model outside the region it was trained on is not a slightly worse
version of itself, it is extrapolating, and its historical accuracy was measured
somewhere else.

**Disagreement is reported, not averaged away.** When the members disagree, the
combined number is not a better estimate -- it is a number none of them holds. So
the ensemble carries how far apart they were, and the parts below can treat a
unanimous 2% differently from a mean of 2% over a range from -1% to 5%.

**Volatility and direction combine differently.** Directional forecasts average
in return space; volatility forecasts average in log space, because volatility is
bounded below by zero and right-skewed and an arithmetic mean of volatilities is
dominated by whichever member is most alarmed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.forecast_types import AVAILABLE, EnsembleForecast
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forecast-ensembler"

PART_DECLARATION = PartDeclaration(
    part_id="forecast-ensembler",
    consumes=(
        "price-forecast", "volatility-forecast", "forecast-trust",
        "forecast-out-of-distribution-flag",
    ),
    produces=("ensemble-forecast", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NOTHING_USABLE = "no-forecaster-contributed"
NO_TRUSTED_MEMBER = "no-contributing-forecaster-has-a-measured-record"

EXCLUDED_UNUSABLE = "the-forecast-itself-was-unusable"
EXCLUDED_UNMEASURED = "no-measured-record-for-this-forecaster"
EXCLUDED_FLAGGED = "flagged-out-of-distribution"


@dataclass
class EnsemblerStanding:
    combinations: int = 0
    published: int = 0
    refused_nothing_usable: int = 0
    refused_nothing_trusted: int = 0
    excluded_unmeasured: int = 0
    excluded_flagged: int = 0
    largest_disagreement: float | None = None
    by_contributor: dict = field(default_factory=dict)


class ForecastEnsembler:
    """Combines forecasters by measured accuracy, and reports what they disagree on."""

    def __init__(
        self,
        minimum_trust: float,
        minimum_members: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= minimum_trust <= 1.0:
            raise ValueError("trust is a hit rate and must be in [0, 1]")
        if minimum_members < 1:
            raise ValueError("an ensemble of nothing is not an ensemble")
        self._minimum_trust = minimum_trust
        self._minimum_members = minimum_members
        self._now_ns = now_ns
        self._trust: dict[str, tuple] = {}
        self._flagged: set[tuple[str, str, str]] = set()
        self.standing = EnsemblerStanding()

    def observe_trust(self, forecaster: str, accuracy: float, is_measured: bool) -> None:
        """What the scorer has measured for this forecaster."""
        self._trust[forecaster] = (accuracy, is_measured)

    def observe_out_of_distribution_flag(
        self, forecaster: str, venue_id: str, symbol: str, is_flagged: bool
    ) -> None:
        key = (forecaster, venue_id, symbol)
        if is_flagged:
            self._flagged.add(key)
        else:
            self._flagged.discard(key)

    def combine(self, venue_id: str, symbol: str, price_forecasts, volatility_forecasts) -> EnsembleForecast:
        self.standing.combinations += 1
        excluded: dict[str, str] = {}

        usable_price = self._usable(price_forecasts, venue_id, symbol, excluded)
        usable_volatility = self._usable(volatility_forecasts, venue_id, symbol, excluded)

        if not usable_price and not usable_volatility:
            self.standing.refused_nothing_usable += 1
            return self._ensemble(
                venue_id, symbol, None, None, {}, None, excluded, NOTHING_USABLE,
                f"nothing contributed: {len(excluded)} forecaster(s) excluded",
            )

        weights = {}
        for forecast in usable_price + usable_volatility:
            accuracy, measured = self._trust.get(forecast.forecaster, (0.0, False))
            if not measured:
                excluded[forecast.forecaster] = EXCLUDED_UNMEASURED
                self.standing.excluded_unmeasured += 1
                continue
            if accuracy < self._minimum_trust:
                excluded[forecast.forecaster] = EXCLUDED_UNMEASURED
                self.standing.excluded_unmeasured += 1
                continue
            weights[forecast.forecaster] = accuracy

        if len(weights) < self._minimum_members:
            self.standing.refused_nothing_trusted += 1
            return self._ensemble(
                venue_id, symbol, None, None, {}, None, excluded, NO_TRUSTED_MEMBER,
                f"{len(weights)} forecaster(s) with a measured record, below the "
                f"{self._minimum_members} this ensemble needs. Including an unmeasured member "
                f"'a little' is how an unvalidated model reaches the decision it should have "
                f"been kept out of",
            )

        contributing_price = [f for f in usable_price if f.forecaster in weights]
        contributing_volatility = [f for f in usable_volatility if f.forecaster in weights]

        expected_return, disagreement = self._weighted_return(contributing_price, weights)
        expected_volatility = self._weighted_volatility(contributing_volatility, weights)

        contributors = {
            forecast.forecaster: {
                "weight": weights[forecast.forecaster],
                "expected_return": forecast.expected_return,
            }
            for forecast in contributing_price
        }
        contributors.update(
            {
                forecast.forecaster: {
                    "weight": weights[forecast.forecaster],
                    "expected_volatility": forecast.expected_volatility,
                }
                for forecast in contributing_volatility
            }
        )
        for name in contributors:
            self.standing.by_contributor[name] = self.standing.by_contributor.get(name, 0) + 1

        if disagreement is not None and (
            self.standing.largest_disagreement is None
            or disagreement > self.standing.largest_disagreement
        ):
            self.standing.largest_disagreement = disagreement

        horizon = (contributing_price or contributing_volatility)[0].horizon_seconds
        self.standing.published += 1
        return self._ensemble(
            venue_id, symbol, expected_return, expected_volatility, contributors,
            disagreement, excluded, AVAILABLE,
            f"{len(contributors)} member(s) weighted by measured accuracy"
            + (
                f": expected return {expected_return:+.3%}"
                if expected_return is not None
                else ""
            )
            + (
                f", expected volatility {expected_volatility:.3%}"
                if expected_volatility is not None
                else ""
            )
            + (
                f"; they disagree by {disagreement:.3%}, which is reported rather than "
                f"averaged away -- a combined number the members do not hold is not a better "
                f"estimate"
                if disagreement
                else ""
            )
            + (f"; {len(excluded)} excluded" if excluded else ""),
            horizon,
        )

    def _usable(self, forecasts, venue_id: str, symbol: str, excluded: dict) -> list:
        usable = []
        for forecast in forecasts or ():
            if not forecast.is_usable:
                excluded[forecast.forecaster] = EXCLUDED_UNUSABLE
                continue
            if (forecast.forecaster, venue_id, symbol) in self._flagged:
                # Not a discount. A model outside its training region is not a
                # slightly worse version of itself, and its measured accuracy
                # was measured somewhere else.
                excluded[forecast.forecaster] = EXCLUDED_FLAGGED
                self.standing.excluded_flagged += 1
                continue
            usable.append(forecast)
        return usable

    def _weighted_return(self, forecasts, weights: dict) -> tuple:
        if not forecasts:
            return None, None
        total = sum(weights[forecast.forecaster] for forecast in forecasts)
        if total <= 0:
            return None, None
        combined = (
            sum(forecast.expected_return * weights[forecast.forecaster] for forecast in forecasts)
            / total
        )
        returns = [forecast.expected_return for forecast in forecasts]
        return combined, (max(returns) - min(returns)) if len(returns) > 1 else 0.0

    def _weighted_volatility(self, forecasts, weights: dict) -> float | None:
        """Combined in log space, because volatility is bounded below and right-skewed.

        An arithmetic mean of volatilities is dominated by whichever member is
        most alarmed, which is the member most likely to be wrong.
        """
        usable = [
            forecast
            for forecast in forecasts
            if forecast.expected_volatility and forecast.expected_volatility > 0
        ]
        if not usable:
            return None
        total = sum(weights[forecast.forecaster] for forecast in usable)
        if total <= 0:
            return None
        log_mean = (
            sum(
                math.log(forecast.expected_volatility) * weights[forecast.forecaster]
                for forecast in usable
            )
            / total
        )
        return math.exp(log_mean)

    def _ensemble(
        self, venue_id, symbol, expected_return, expected_volatility, contributors,
        disagreement, excluded, state, reason, horizon=0.0,
    ) -> EnsembleForecast:
        return EnsembleForecast(
            venue_id=venue_id,
            symbol=symbol,
            expected_return=expected_return,
            expected_volatility=expected_volatility,
            horizon_seconds=horizon,
            contributors=contributors,
            disagreement=disagreement,
            excluded=dict(excluded),
            state=state,
            reason=reason,
            combined_at_ns=self._now_ns(),
        )


def describe_ensembling(ensembler: ForecastEnsembler) -> dict:
    return {
        "part_id": PART_ID,
        "combinations": ensembler.standing.combinations,
        "published": ensembler.standing.published,
        "refused_nothing_usable": ensembler.standing.refused_nothing_usable,
        "refused_no_trusted_member": ensembler.standing.refused_nothing_trusted,
        "excluded_for_no_measured_record": ensembler.standing.excluded_unmeasured,
        "excluded_as_out_of_distribution": ensembler.standing.excluded_flagged,
        "largest_disagreement": ensembler.standing.largest_disagreement,
        "by_contributor": dict(sorted(ensembler.standing.by_contributor.items())),
        "forecasters_with_a_trust_record": sorted(ensembler._trust),
    }


def run_forecast_ensembler(
    ensembler: ForecastEnsembler, control_socket, read_forecasts, publish_ensembles,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        groups = read_forecasts(ensembler)
        publish_ensembles(
            tuple(
                ensembler.combine(venue_id, symbol, prices, volatilities)
                for venue_id, symbol, prices, volatilities in groups
            )
        )

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

    prices = Batch(read=context.bus.reader("price-forecast"))
    volatilities = Batch(read=context.bus.reader("volatility-forecast"))
    trusts = Batch(read=context.bus.reader("forecast-trust"))
    flags = Batch(read=context.bus.reader("forecast-out-of-distribution-flag"))
    publish_ensembles = context.bus.publisher_for("ensemble-forecast")
    ensembler = ForecastEnsembler(
        minimum_trust=context.number("ensemble_minimum_trust"),
        minimum_members=int(context.number("ensemble_minimum_members")),
    )

    def read_forecasts(_ensembler):
        for trust in trusts.payloads():
            ensembler.observe_trust(trust.forecaster, trust.trust, trust.state == "measured")
        for flag in flags.payloads():
            ensembler.observe_out_of_distribution_flag(flag.forecaster, flag.venue_id, flag.symbol, flag.is_out_of_distribution)
        grouped: dict[tuple[str, str], tuple[list, list]] = {}
        for forecast in prices.payloads():
            grouped.setdefault((forecast.venue_id, forecast.symbol), ([], []))[0].append(forecast)
        for forecast in volatilities.payloads():
            grouped.setdefault((forecast.venue_id, forecast.symbol), ([], []))[1].append(forecast)
        return tuple((key[0], key[1], tuple(p), tuple(v)) for key, (p, v) in sorted(grouped.items()))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_ensembles(kept)

    return run_forecast_ensembler(
        ensembler=ensembler,
        control_socket=context.control_socket,
        read_forecasts=read_forecasts,
        publish_ensembles=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
