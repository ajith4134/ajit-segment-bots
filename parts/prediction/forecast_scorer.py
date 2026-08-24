"""forecast-scorer: what each forecast was actually worth, after the fact.

The user did not ask for this part. It exists because a forecast nobody scores is
an assertion, and four blocks downstream are supposed to learn from something.

**Direction and magnitude are scored separately** because they fail separately. A
model that calls direction well and overshoots every magnitude is useful to a bot
and useless for sizing; one number over both would hide which, and the fix for
each is different.

**Interval coverage is scored too**, and it is the measure most often skipped. A
model whose 80% interval contains the outcome 40% of the time is not slightly
overconfident -- it is producing intervals that mean nothing, and everything that
sizes from them is wrong in the same direction every time.

**A forecast is scored against the horizon it named**, and one whose horizon has
not elapsed is not scored yet. Scoring early would credit the model for a move
that has not finished; scoring late would credit it for one that continued past
what it predicted. Both flatter the model.

**Unusable forecasts are counted, not scored.** A model that refused to run has no
directional accuracy, and folding those in as misses would make an honest refusal
look like a wrong answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_staleness import ObservedPrice
from runtime.forecast_types import ForecastAccuracy
from runtime.learned_estimator import RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forecast-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="forecast-scorer",
    consumes=("price-forecast", "market-data"),
    produces=("forecast-accuracy", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)


@dataclass
class PendingForecast:
    """A forecast waiting for its own horizon to elapse."""

    forecast: object
    price_at_forecast: float
    due_at_ns: int


@dataclass
class ScorerRecord:
    direction: RateEstimator
    coverage: RateEstimator
    absolute_error_total: float = 0.0
    scored: int = 0


@dataclass
class ScorerStanding:
    forecasts_taken: int = 0
    forecasts_scored: int = 0
    unusable_forecasts_counted: int = 0
    still_waiting: int = 0
    dropped_no_price: int = 0
    by_forecaster: dict = field(default_factory=dict)


class ForecastScorer:
    """Scores each forecast at its own horizon, on direction, magnitude and interval."""

    def __init__(
        self,
        prior_accuracy: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        self._prior_accuracy = prior_accuracy
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._pending: list[PendingForecast] = []
        self._records: dict[tuple[str, str, str, float], ScorerRecord] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self._unusable: dict[str, int] = {}
        self.standing = ScorerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, kept with the venue's own time for it.

        `at_ns` has no default. A price with no age cannot be told apart from a
        price that stopped arriving, which is how a symbol frozen for 56 minutes
        was traded on 2026-08-23.
        """
        self._prices[(venue_id, symbol)] = ObservedPrice(
            price=price, observed_at_ns=at_ns
        )

    def take_forecast(self, forecast) -> None:
        """Hold a forecast until its horizon elapses.

        An unusable one is counted rather than held: folding a refusal in as a
        miss would make honesty look like being wrong.
        """
        self.standing.forecasts_taken += 1
        if not forecast.is_usable:
            self.standing.unusable_forecasts_counted += 1
            self._unusable[forecast.state] = self._unusable.get(forecast.state, 0) + 1
            return

        observed = self._prices.get((forecast.venue_id, forecast.symbol))
        price = None if observed is None else observed.price
        if price is None:
            self.standing.dropped_no_price += 1
            return

        self._pending.append(
            PendingForecast(
                forecast=forecast,
                price_at_forecast=price,
                due_at_ns=forecast.forecast_at_ns + int(forecast.horizon_seconds * 1e9),
            )
        )
        self.standing.still_waiting = len(self._pending)

    def score_due(self) -> tuple[ForecastAccuracy, ...]:
        """Score every forecast whose horizon has elapsed, and only those."""
        now = self._now_ns()
        due = [pending for pending in self._pending if pending.due_at_ns <= now]
        self._pending = [pending for pending in self._pending if pending.due_at_ns > now]
        self.standing.still_waiting = len(self._pending)

        scored = []
        for pending in due:
            accuracy = self._score_one(pending)
            if accuracy is not None:
                scored.append(accuracy)
        return tuple(scored)

    def _score_one(self, pending: PendingForecast) -> ForecastAccuracy | None:
        forecast = pending.forecast
        observed_now = self._prices.get((forecast.venue_id, forecast.symbol))
        price_now = None if observed_now is None else observed_now.price
        if price_now is None or pending.price_at_forecast <= 0:
            self.standing.dropped_no_price += 1
            return None

        actual = (price_now - pending.price_at_forecast) / pending.price_at_forecast
        record = self._record_for(forecast)

        # Direction: only counted when the model committed to one. A model
        # forecasting exactly zero has not called a direction, and scoring it as
        # a miss would penalise the one honest answer.
        if forecast.direction != 0:
            called_it = (actual > 0) == (forecast.expected_return > 0)
            record.direction.observe(called_it)

        record.absolute_error_total += abs(actual - forecast.expected_return)
        record.scored += 1

        if forecast.lower_return is not None and forecast.upper_return is not None:
            record.coverage.observe(forecast.lower_return <= actual <= forecast.upper_return)

        self.standing.forecasts_scored += 1
        self.standing.by_forecaster[forecast.forecaster] = (
            self.standing.by_forecaster.get(forecast.forecaster, 0) + 1
        )

        direction = record.direction.estimate(self._minimum)
        coverage = record.coverage.estimate(self._minimum)
        return ForecastAccuracy(
            forecaster=forecast.forecaster,
            model_name=forecast.model_name,
            venue_id=forecast.venue_id,
            symbol=forecast.symbol,
            horizon_seconds=forecast.horizon_seconds,
            directional_accuracy=direction,
            mean_absolute_error=record.absolute_error_total / record.scored,
            interval_coverage=coverage,
            forecasts_scored=record.scored,
            reason=(
                f"{forecast.model_name} predicted {forecast.expected_return:+.3%} and "
                f"{forecast.symbol} did {actual:+.3%} over {forecast.horizon_seconds:.0f}s. "
                f"Direction {direction.value:.0%} over {direction.observations} call(s)"
                + (
                    f"; its stated interval has contained the outcome {coverage.value:.0%} of "
                    f"the time, against the interval it claims"
                    if coverage.observations
                    else "; no interval was stated, so coverage cannot be judged"
                )
                + f"; mean absolute error {record.absolute_error_total / record.scored:.4f}"
            ),
            scored_at_ns=self._now_ns(),
        )

    def accuracy_of(self, forecaster: str, model_name: str, venue_id: str, symbol: str, horizon: float):
        record = self._records.get((forecaster, model_name, f"{venue_id}:{symbol}", horizon))
        if record is None:
            return None
        return record.direction.estimate(self._minimum)

    def _record_for(self, forecast) -> ScorerRecord:
        key = (
            forecast.forecaster,
            forecast.model_name,
            f"{forecast.venue_id}:{forecast.symbol}",
            forecast.horizon_seconds,
        )
        record = self._records.get(key)
        if record is None:
            record = ScorerRecord(
                direction=RateEstimator(
                    prior=self._prior_accuracy, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                ),
                coverage=RateEstimator(
                    prior=self._prior_accuracy, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                ),
            )
            self._records[key] = record
        return record


def describe_scoring(scorer: ForecastScorer) -> dict:
    return {
        "part_id": PART_ID,
        "forecasts_taken": scorer.standing.forecasts_taken,
        "forecasts_scored": scorer.standing.forecasts_scored,
        "waiting_for_their_horizon": scorer.standing.still_waiting,
        "unusable_forecasts_counted_not_scored": scorer.standing.unusable_forecasts_counted,
        "unusable_by_state": dict(sorted(scorer._unusable.items())),
        "dropped_for_no_price": scorer.standing.dropped_no_price,
        "by_forecaster": dict(sorted(scorer.standing.by_forecaster.items())),
        "records_held": len(scorer._records),
    }


def run_forecast_scorer(
    scorer: ForecastScorer, control_socket, read_forecasts_and_prices, publish_accuracy,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_forecasts_and_prices(scorer)
        publish_accuracy(scorer.score_due())

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
    from runtime.venues.venue_adapter import NormalisedTrade

    forecasts = Batch(read=context.bus.reader("price-forecast"))
    trades = Batch(read=context.bus.reader("market-data"))
    publish_accuracy = context.bus.publisher_for("forecast-accuracy")
    scorer = ForecastScorer(
        prior_accuracy=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
    )

    def read_forecasts_and_prices(_scorer) -> None:
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                scorer.observe_price(
                    trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
                )
        for forecast in forecasts.payloads():
            scorer.take_forecast(forecast)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_accuracy(kept)

    return run_forecast_scorer(
        scorer=scorer,
        control_socket=context.control_socket,
        read_forecasts_and_prices=read_forecasts_and_prices,
        publish_accuracy=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
