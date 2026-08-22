"""bull-conviction-model: how likely this long is to work, learned from what happened.

The learned part of the bull bot (RL-060). Everything above it measures; this is
where the bot forms a belief, and it forms it from its own record rather than
from a rule somebody wrote down.

**Online logistic regression**, not a tree ensemble and not a batch retrain:

- It updates from a single closed trade in constant time and memory, which is
  what a part that must be able to release its memory on demand can afford (T-3).
- Its coefficients can be read. A conviction of 0.71 that cannot be explained is
  not reviewable, and a part nobody can argue with is a part nobody can fix.
- Nothing about it is a placeholder for a better model later. A model that
  improves is a **new model run as a challenger** against this one, promoted by
  `champion-choice` -- which is why this part holds two.

**Champion and challenger, both trained, only one believed.** A model that
replaced itself the moment a new one looked better would chase noise; a model
that never changed would be trading a market that has ended. Both learn from
every labelled outcome, the champion is what the bot acts on, and the swap is a
decision made elsewhere and delivered as `champion-choice`. That is the same
control-path-separate-from-data-path rule the governor follows (T-2).

**Two refusals, and neither returns a probability.** A vector the outlier
rejector flagged, or a price forecast flagged out of distribution, produces no
conviction at all -- because the model's answer there would be an extrapolation
wearing the same type as a measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, RawConviction
from runtime.online_learner import OnlineLogisticModel
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-conviction-model"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-conviction-model",
    consumes=(
        "bull-feature-vector", "price-forecast", "kline-window",
        "forecast-out-of-distribution-flag", "training-label", "sample-weight",
        "retrain-request", "champion-choice", "learning-reward",
        "bull-feature-out-of-distribution-flag",
    ),
    produces=("bull-raw-conviction", "part-health"),
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
    retrains: int = 0
    champion_swaps: int = 0
    rewards_applied: int = 0
    live_model: str = CHAMPION
    mean_absolute_error: float = 0.0
    by_symbol: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingExample:
    """One labelled outcome, and how much it should count.

    The weight is not decoration. A trade closed in a regime the bot no longer
    operates in, or one whose label came from a partial fill, should move the
    model less than a clean recent one -- and the part that knows that is the
    learning loop, not this one.
    """

    features: dict
    label: bool
    sample_weight: float
    source: str


class BullConvictionModel:
    """Two online models, one believed, both learning from every labelled outcome."""

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
                "the default weight must be positive and the cap must not sit below it, "
                "or a reward could silently mute every example"
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

    # -- what the model is told about the world ------------------------------

    def observe_price_forecast(self, venue_id: str, symbol: str, expected_return: float) -> None:
        """Another part's view of where price is going, as one more feature."""
        self._forecasts[(venue_id, symbol)] = expected_return

    def observe_forecast_flag(self, venue_id: str, symbol: str, is_out_of_distribution: bool) -> None:
        key = (venue_id, symbol)
        if is_out_of_distribution:
            self._forecast_flagged.add(key)
        else:
            self._forecast_flagged.discard(key)

    def observe_kline_window(self, venue_id: str, symbol: str, closes, highs, lows) -> None:
        """Shape features from the candle window: where price sits in its own range."""
        if not closes or not highs or not lows:
            return
        high = max(highs)
        low = min(lows)
        span = high - low
        features = {"kline_close_to_open_fraction": (closes[-1] - closes[0]) / closes[0]}
        if span > 0:
            features["kline_position_in_range"] = (closes[-1] - low) / span
            features["kline_range_fraction"] = span / closes[-1] if closes[-1] else 0.0
        self._kline_features[(venue_id, symbol)] = features

    def observe_learning_reward(self, detector: str, multiplier: float) -> None:
        """How much the learning loop wants this detector's outcomes to count."""
        if multiplier <= 0:
            raise ValueError("a non-positive multiplier would unlearn or erase the example")
        self._reward_multipliers[detector] = min(multiplier, self._maximum_sample_weight)
        self.standing.rewards_applied += 1

    def apply_champion_choice(self, chosen: str) -> None:
        """Promote a model to live. The decision is made elsewhere (T-2)."""
        if chosen not in self._models:
            raise ValueError(f"{chosen!r} is not a model this part holds")
        if chosen != self._live:
            self.standing.champion_swaps += 1
        self._live = chosen
        self.standing.live_model = chosen

    def apply_retrain_request(self, which: str) -> None:
        """Start one model over. The other keeps trading, which is the point.

        A retrain that stopped the bot would make retraining expensive enough
        that it would be avoided in exactly the regimes that need it.
        """
        if which not in self._models:
            raise ValueError(f"{which!r} is not a model this part holds")
        if which == self._live:
            raise ValueError(
                "the live model cannot be retrained from empty while it is being acted on; "
                "retrain the other and promote it with a champion choice"
            )
        self._models[which] = OnlineLogisticModel(**self._settings)
        self.standing.retrains += 1

    # -- training ------------------------------------------------------------

    def train(self, example: TrainingExample) -> float:
        """One labelled outcome into both models. Returns the live model's error."""
        weight = min(
            self._maximum_sample_weight,
            example.sample_weight * self._reward_multipliers.get(example.source, 1.0),
        )
        error = 0.0
        for name, model in self._models.items():
            model_error = model.train(example.features, example.label, weight)
            if name == self._live:
                error = model_error
        self.standing.labels_trained_on += 1
        self._absolute_error_total += abs(error)
        self.standing.mean_absolute_error = (
            self._absolute_error_total / self.standing.labels_trained_on
        )
        return error

    def train_from_label(
        self, features: dict, label: bool, source: str, sample_weight: float | None = None
    ) -> float:
        return self.train(
            TrainingExample(
                features=dict(features),
                label=label,
                sample_weight=self._default_sample_weight if sample_weight is None else sample_weight,
                source=source,
            )
        )

    # -- forming a conviction ------------------------------------------------

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
        self.standing.by_symbol[vector.symbol] = (
            self.standing.by_symbol.get(vector.symbol, 0) + 1
        )

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
                side=LONG,
                belief=belief,
                reason=(
                    f"the {self._live} model puts this long at {belief.probability:.1%} from "
                    f"{belief.features_used} feature(s){driver}. {belief.reason}"
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


def describe_conviction(model: BullConvictionModel) -> dict:
    return {
        "part_id": PART_ID,
        "live_model": model.live_model_name,
        "convictions_formed": model.standing.convictions_formed,
        "refused_features_out_of_distribution": model.standing.refused_features_flagged,
        "refused_forecast_out_of_distribution": model.standing.refused_forecast_flagged,
        "refused_nothing_usable": model.standing.refused_nothing_usable,
        "labels_trained_on": model.standing.labels_trained_on,
        "mean_absolute_training_error": model.standing.mean_absolute_error,
        "retrains": model.standing.retrains,
        "champion_swaps": model.standing.champion_swaps,
        "champion": model.model(CHAMPION).describe(),
        "challenger": model.model(CHALLENGER).describe(),
    }


def run_bull_conviction_model(
    model: BullConvictionModel, control_socket, read_vectors_flags_and_labels,
    publish_convictions, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        vectors_with_flags = read_vectors_flags_and_labels(model)
        convictions = []
        for vector, is_flagged in vectors_with_flags:
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
    )
