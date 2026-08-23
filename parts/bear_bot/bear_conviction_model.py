"""bear-conviction-model: how likely this short is to work, learned from short outcomes.

Structurally the bull's counterpart and epistemically a separate model, because
the two sides are not the same problem wearing different signs.

**Why not one model with a side feature.** A single model pools the two sides'
outcomes and learns whichever dominates the sample. Crypto's long side has more
trades, longer trends and a survivorship bias no short shares; a pooled model
would carry that into every short it scored, and the coefficient on the side flag
would be the only thing standing against it. Two models make the asymmetry
structural rather than something one weight has to hold back.

**Why the horizon matters more here.** A short's cost of being wrong grows with
time -- funding is charged every settlement in the regimes where shorts look
attractive, and the losing tail has no ceiling. So this model learns from
outcomes labelled **within the horizon they were given** and treats a trade that
resolved after its horizon as a loss, not as a slow win. A model that counted
those as wins would learn to hold shorts through squeezes.

Champion and challenger, both trained, one believed, promoted by control (T-2).
Two refusals, and neither returns a probability: a flagged feature vector, and a
price forecast flagged out of distribution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SHORT, RawConviction
from runtime.online_learner import OnlineLogisticModel
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-conviction-model"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-conviction-model",
    consumes=(
        "bear-feature-vector", "price-forecast", "kline-window",
        "forecast-out-of-distribution-flag", "training-label", "sample-weight",
        "retrain-request", "champion-choice", "learning-reward",
        "bear-feature-out-of-distribution-flag",
    ),
    produces=("bear-raw-conviction", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CHAMPION = "champion"
CHALLENGER = "challenger"

FEATURES_FLAGGED = "features-out-of-distribution"
FORECAST_FLAGGED = "forecast-out-of-distribution"
NOTHING_USABLE = "no-feature-could-be-standardised"


@dataclass
class ModelStanding:
    convictions_formed: int = 0
    refused_features_flagged: int = 0
    refused_forecast_flagged: int = 0
    refused_nothing_usable: int = 0
    labels_trained_on: int = 0
    resolved_after_horizon: int = 0
    retrains: int = 0
    champion_swaps: int = 0
    rewards_applied: int = 0
    live_model: str = CHAMPION
    mean_absolute_error: float = 0.0
    by_symbol: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ShortOutcome:
    """One closed short, with the time it took as well as the direction it went.

    The seconds are not bookkeeping. A short that reached its target after three
    times its horizon paid funding the whole way and sat exposed to a squeeze it
    was never sized for; counting it as a win teaches the model to hold on.
    """

    features: dict
    moved_the_expected_way: bool
    seconds_to_resolve: float
    horizon_seconds: float
    sample_weight: float
    source: str

    @property
    def label(self) -> bool:
        return self.moved_the_expected_way and self.seconds_to_resolve <= self.horizon_seconds

    @property
    def was_late(self) -> bool:
        return self.moved_the_expected_way and self.seconds_to_resolve > self.horizon_seconds


class BearConvictionModel:
    """Two online models over short outcomes, judged inside the horizon they were given."""

    def __init__(
        self,
        learning_rate: float,
        l2_regularisation: float,
        feature_half_life_observations: float,
        minimum_feature_observations: int,
        minimum_training_observations: int,
        default_sample_weight: float,
        maximum_sample_weight: float,
        now_ns=time.time_ns,
    ) -> None:
        if default_sample_weight <= 0 or maximum_sample_weight < default_sample_weight:
            raise ValueError(
                "the default weight must be positive and the cap must not sit below it, or a "
                "reward could silently mute every example"
            )
        self._settings = dict(
            learning_rate=learning_rate,
            l2_regularisation=l2_regularisation,
            feature_half_life_observations=feature_half_life_observations,
            minimum_feature_observations=minimum_feature_observations,
            minimum_training_observations=minimum_training_observations,
        )
        self._default_sample_weight = default_sample_weight
        self._maximum_sample_weight = maximum_sample_weight
        self._now_ns = now_ns
        self._models = {
            CHAMPION: OnlineLogisticModel(**self._settings),
            CHALLENGER: OnlineLogisticModel(**self._settings),
        }
        self._live = CHAMPION
        self._forecasts: dict[tuple[str, str], float] = {}
        self._forecast_flagged: set[tuple[str, str]] = set()
        self._kline_features: dict[tuple[str, str], dict] = {}
        self._reward_multipliers: dict[str, float] = {}
        self._absolute_error_total = 0.0
        self.standing = ModelStanding()

    def observe_price_forecast(self, venue_id: str, symbol: str, expected_return: float) -> None:
        self._forecasts[(venue_id, symbol)] = expected_return

    def observe_forecast_flag(self, venue_id: str, symbol: str, is_out_of_distribution: bool) -> None:
        key = (venue_id, symbol)
        if is_out_of_distribution:
            self._forecast_flagged.add(key)
        else:
            self._forecast_flagged.discard(key)

    def observe_kline_window(self, venue_id: str, symbol: str, closes, highs, lows) -> None:
        """Shape features from the candle window, read from the short's end of the range."""
        if not closes or not highs or not lows:
            return
        high = max(highs)
        low = min(lows)
        span = high - low
        features = {"kline_close_to_open_fraction": (closes[-1] - closes[0]) / closes[0]}
        if span > 0:
            # Distance from the high rather than position in the range: a short
            # entered near the high has room beneath it, and that is the number
            # the model should be weighing.
            features["kline_distance_below_high"] = (high - closes[-1]) / span
            features["kline_range_fraction"] = span / closes[-1] if closes[-1] else 0.0
        self._kline_features[(venue_id, symbol)] = features

    def observe_learning_reward(self, detector: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("a non-positive multiplier would unlearn or erase the example")
        self._reward_multipliers[detector] = min(multiplier, self._maximum_sample_weight)
        self.standing.rewards_applied += 1

    def apply_champion_choice(self, chosen: str) -> None:
        if chosen not in self._models:
            raise ValueError(f"{chosen!r} is not a model this part holds")
        if chosen != self._live:
            self.standing.champion_swaps += 1
        self._live = chosen
        self.standing.live_model = chosen

    def apply_retrain_request(self, which: str) -> None:
        if which not in self._models:
            raise ValueError(f"{which!r} is not a model this part holds")
        if which == self._live:
            raise ValueError(
                "the live model cannot be retrained from empty while it is being acted on; "
                "retrain the other and promote it with a champion choice"
            )
        self._models[which] = OnlineLogisticModel(**self._settings)
        self.standing.retrains += 1

    def train(self, outcome: ShortOutcome) -> float:
        """One closed short into both models, labelled inside its own horizon."""
        weight = min(
            self._maximum_sample_weight,
            outcome.sample_weight * self._reward_multipliers.get(outcome.source, 1.0),
        )
        error = 0.0
        for name, model in self._models.items():
            model_error = model.train(outcome.features, outcome.label, weight)
            if name == self._live:
                error = model_error
        self.standing.labels_trained_on += 1
        if outcome.was_late:
            self.standing.resolved_after_horizon += 1
        self._absolute_error_total += abs(error)
        self.standing.mean_absolute_error = (
            self._absolute_error_total / self.standing.labels_trained_on
        )
        return error

    def train_from_label(
        self,
        features: dict,
        moved_the_expected_way: bool,
        seconds_to_resolve: float,
        horizon_seconds: float,
        source: str,
        sample_weight: float | None = None,
    ) -> float:
        return self.train(
            ShortOutcome(
                features=dict(features),
                moved_the_expected_way=moved_the_expected_way,
                seconds_to_resolve=seconds_to_resolve,
                horizon_seconds=horizon_seconds,
                sample_weight=self._default_sample_weight if sample_weight is None else sample_weight,
                source=source,
            )
        )

    def form_conviction(self, vector, is_out_of_distribution: bool) -> tuple[RawConviction | None, str]:
        key = (vector.venue_id, vector.symbol)

        if is_out_of_distribution:
            self.standing.refused_features_flagged += 1
            return None, FEATURES_FLAGGED

        if key in self._forecast_flagged:
            self.standing.refused_forecast_flagged += 1
            return None, FORECAST_FLAGGED

        features = dict(vector.features)
        forecast = self._forecasts.get(key)
        if forecast is not None:
            features["price_forecast_expected_return"] = forecast
        features.update(self._kline_features.get(key, {}))

        model = self._models[self._live]
        belief = model.believe(features)

        if belief.features_used == 0:
            self.standing.refused_nothing_usable += 1
            return None, NOTHING_USABLE

        self.standing.convictions_formed += 1
        self.standing.by_symbol[vector.symbol] = self.standing.by_symbol.get(vector.symbol, 0) + 1

        strongest = belief.strongest_reason
        driver = (
            f"; {strongest[0]} moved it most, by {strongest[1]:+.3f} in log-odds"
            if strongest
            else ""
        )
        return (
            RawConviction(
                bot=BOT,
                venue_id=vector.venue_id,
                symbol=vector.symbol,
                side=SHORT,
                belief=belief,
                reason=(
                    f"the {self._live} model puts this short at {belief.probability:.1%} from "
                    f"{belief.features_used} feature(s){driver}, learned from shorts that "
                    f"resolved inside the horizon they were given. {belief.reason}"
                ),
                formed_at_ns=self._now_ns(),
            ),
            "formed",
        )

    @property
    def live_model_name(self) -> str:
        return self._live

    def model(self, name: str) -> OnlineLogisticModel:
        return self._models[name]


def describe_conviction(model: BearConvictionModel) -> dict:
    return {
        "part_id": PART_ID,
        "live_model": model.live_model_name,
        "convictions_formed": model.standing.convictions_formed,
        "refused_features_out_of_distribution": model.standing.refused_features_flagged,
        "refused_forecast_out_of_distribution": model.standing.refused_forecast_flagged,
        "refused_nothing_usable": model.standing.refused_nothing_usable,
        "labels_trained_on": model.standing.labels_trained_on,
        "shorts_that_worked_but_resolved_late": model.standing.resolved_after_horizon,
        "mean_absolute_training_error": model.standing.mean_absolute_error,
        "retrains": model.standing.retrains,
        "champion_swaps": model.standing.champion_swaps,
        "champion": model.model(CHAMPION).describe(),
        "challenger": model.model(CHALLENGER).describe(),
    }


def run_bear_conviction_model(
    model: BearConvictionModel, control_socket, read_vectors_flags_and_labels,
    publish_convictions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        convictions = []
        for vector, is_flagged in read_vectors_flags_and_labels(model):
            conviction, _ = model.form_conviction(vector, is_flagged)
            if conviction is not None:
                convictions.append(conviction)
        publish_convictions(tuple(convictions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
