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

import pathlib
import time
from dataclasses import dataclass, field

from runtime.bot_opinion import SHORT, RawConviction
from runtime.learned_state import (
    STARTED_COLD_UNREADABLE,
    CheckpointSchedule,
    LearnedStateStore,
)
from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.online_learner import OnlineLogisticModel
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-conviction-model"
# The checkpoint is addressed by component, as the bull bot's is.
COMPONENT = "conviction"
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
    # What the checkpoint said at start, as in the bull model (ported 2026-08-23).
    checkpoint_verdict: str | None = None
    checkpoint_detail: str | None = None
    checkpoint_saved_at_ns: int | None = None
    checkpoints_written: int = 0


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


    # -- what this part carries across the off switch (ported from the bull) --

    def learned_settings(self) -> dict:
        return self._models[CHAMPION].learned_settings()

    def state(self) -> dict:
        return {
            "live": self._live,
            "models": {name: model.state() for name, model in self._models.items()},
            "labels_trained_on": self.standing.labels_trained_on,
            "absolute_error_total": self._absolute_error_total,
        }

    def restore_state(self, state: dict) -> None:
        for name, stored in state["models"].items():
            if name not in self._models:
                raise ValueError(f"the checkpoint holds a model named {name!r} that this part does not run")
            self._models[name].restore_state(stored)
        live = state["live"]
        if live not in self._models:
            raise ValueError(f"the checkpoint names {live!r} live and this part does not hold it")
        self._live = live
        self.standing.live_model = live
        self.standing.labels_trained_on = int(state["labels_trained_on"])
        self._absolute_error_total = float(state["absolute_error_total"])
        if self.standing.labels_trained_on:
            self.standing.mean_absolute_error = self._absolute_error_total / self.standing.labels_trained_on

    @property
    def training_observations(self) -> int:
        return self._models[self._live].observations


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
        "checkpoint_verdict": model.standing.checkpoint_verdict,
        "checkpoint_detail": model.standing.checkpoint_detail,
        "checkpoint_saved_at_ns": model.standing.checkpoint_saved_at_ns,
        "checkpoints_written": model.standing.checkpoints_written,
        "champion": model.model(CHAMPION).describe(),
        "challenger": model.model(CHALLENGER).describe(),
    }


def restore_or_start_cold(model: BearConvictionModel, store, part_id: str = PART_ID) -> None:
    """Adopt the previous process's model, or record why this one starts cold."""
    restoration = store.restore(part_id, COMPONENT, model.learned_settings())
    model.standing.checkpoint_saved_at_ns = restoration.saved_at_ns
    if not restoration.was_restored:
        model.standing.checkpoint_verdict = restoration.verdict
        model.standing.checkpoint_detail = restoration.detail
        return
    try:
        model.restore_state(restoration.state)
    except (KeyError, TypeError, ValueError) as refusal:
        model.standing.checkpoint_verdict = STARTED_COLD_UNREADABLE
        model.standing.checkpoint_detail = f"{restoration.detail}, but it could not be adopted: {refusal}"
        return
    model.standing.checkpoint_verdict = restoration.verdict
    model.standing.checkpoint_detail = restoration.detail


def run_bear_conviction_model(
    model: BearConvictionModel, control_socket, read_vectors_flags_and_labels,
    publish_convictions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    checkpoint=None,
) -> int:
    """`checkpoint` is called with the model on every tick, as in the bull model:
    whether enough has been learned to be worth an fsync is the schedule's
    decision, and a part that only checkpointed after training would never
    write the first one on a quiet market."""
    def tick() -> None:
        convictions = []
        for vector, is_flagged in read_vectors_flags_and_labels(model):
            conviction, _ = model.form_conviction(vector, is_flagged)
            if conviction is not None:
                convictions.append(conviction)
        publish_convictions(tuple(convictions))
        if checkpoint is not None:
            checkpoint(model)

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
    """The one entry point every part carries (T-1).

    This is the part the whole cold-start problem was about. It forms no conviction
    until it has been trained, and until `signal-outcome-labeller` exists nothing
    could train it -- see `docs/proposals/signal-outcome-labelling.md`.

    A label arrives after the vector it belongs to, by the length of the claim's
    horizon. So vectors are kept by symbol with the time they were built, and a
    label is matched to the vector that was current when the claim was made, not to
    whichever vector happens to be current when the label lands. Training on the
    latter would teach the model to predict the past from the present.
    """
    from runtime.input_assembly import Batch, LatestByKey

    vectors = Batch(read=context.bus.reader("bear-feature-vector"))
    flags = LatestByKey(
        read=context.bus.reader("bear-feature-out-of-distribution-flag"),
        key_of=lambda flag: (flag.venue_id, flag.symbol),
    )
    labels = Batch(read=context.bus.reader("training-label"))
    weights = LatestByKey(
        read=context.bus.reader("sample-weight"),
        key_of=lambda weight: (weight.venue_id, weight.symbol),
    )
    forecasts = Batch(read=context.bus.reader("price-forecast"))
    forecast_flags = Batch(read=context.bus.reader("forecast-out-of-distribution-flag"))
    kline_windows = Batch(read=context.bus.reader("kline-window"))
    rewards = Batch(read=context.bus.reader("learning-reward"))
    publish_convictions = context.bus.publisher_for("bear-raw-conviction")

    # Vectors kept per symbol with when they were built, so a label that arrives a
    # horizon later can find the one that was current when the claim was made.
    # Bounded by what the horizon can span, because this is a process's memory and
    # an unbounded history of vectors is a leak with a good excuse.
    remembered: dict[tuple[str, str], list] = {}
    remembered_per_symbol = int(context.number("bear_remembered_vectors_per_symbol"))

    def remember(vector) -> None:
        key = (vector.venue_id, vector.symbol)
        history = remembered.setdefault(key, [])
        history.append(vector)
        if len(history) > remembered_per_symbol:
            del history[0]

    def vector_current_at(venue_id: str, symbol: str, at_ns: int):
        history = remembered.get((venue_id, symbol), ())
        current = None
        for vector in history:
            if vector.built_at_ns <= at_ns:
                current = vector
            else:
                break
        return current

    def read_vectors_flags_and_labels(model):
        for forecast in forecasts.payloads():
            model.observe_price_forecast(forecast.venue_id, forecast.symbol, forecast.expected_return)
        for flag in forecast_flags.payloads():
            model.observe_forecast_flag(flag.venue_id, flag.symbol, flag.is_out_of_distribution)
        for window in kline_windows.payloads():
            # The window carries candles; the model wants the three series it
            # shapes features from. Unpacked here rather than in the model, which
            # must not know the shape of a type another block publishes (T-4).
            model.observe_kline_window(
                window.venue_id,
                window.symbol,
                [candle.close for candle in window.candles],
                [candle.high for candle in window.candles],
                [candle.low for candle in window.candles],
            )
        for reward in rewards.payloads():
            model.note_uninterpretable_reward(
                f"{reward.detector} sent a shaped reward of {reward.reward!r} in state "
                f"{reward.state!r}; no conversion from a shaped reward to a weight "
                f"multiplier has been decided, and sample-weight is the input that carries one"
            )

        weight_by_symbol = weights.mapping()
        for label in labels.payloads():
            vector = vector_current_at(label.venue_id, label.symbol, label.claimed_at_ns)
            if vector is None:
                # A label for a symbol this bot never built a vector for. Not an
                # error: the labeller scores every detector's claims, and the bull
                # bot only builds vectors for the ones its filter accepted.
                continue
            outcome = label.label_for(THE_SETUP_WAS_RIGHT)
            if outcome is None:
                continue
            weight = weight_by_symbol.get((label.venue_id, label.symbol))
            model.train_from_label(
                features=vector.features,
                moved_the_expected_way=outcome,
                seconds_to_resolve=float(getattr(label, "seconds_to_resolve", 0.0) or 0.0),
                horizon_seconds=float(getattr(label, "horizon_seconds", 0.0) or 0.0) or float("inf"),
                source=f"{label.detector}:{THE_SETUP_WAS_RIGHT}",
                sample_weight=getattr(weight, "weight", None),
            )

        flag_by_symbol = flags.mapping()
        judged = []
        for vector in vectors.payloads():
            remember(vector)
            flag = flag_by_symbol.get((vector.venue_id, vector.symbol))
            judged.append((vector, bool(flag.is_out_of_distribution) if flag else False))
        return judged

    model = BearConvictionModel(
        learning_rate=context.number("bear_learning_rate"),
        l2_regularisation=context.number("bear_l2_regularisation"),
        feature_half_life_observations=context.number("bear_feature_half_life_observations"),
        minimum_feature_observations=int(context.number("bear_minimum_feature_observations")),
        minimum_training_observations=int(context.number("bear_minimum_training_observations")),
        default_sample_weight=context.number("bear_default_sample_weight"),
        maximum_sample_weight=context.number("bear_maximum_sample_weight"),
    )

    # What this bot has learned, carried across the off switch. Without it the
    # model was rebuilt empty on every fork, and since it needs
    # bear_minimum_training_observations outcomes of each class before its
    # conviction is a measurement, a bot that was restarted more often than that
    # took could never form an opinion at all. Measured on the run of 2026-08-22
    # 17:37-18:42: 184 412 candidates noticed, no opinion formed, count back to
    # zero at the next start.
    store = LearnedStateStore(
        pathlib.Path(str(context.setting("learned_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    restore_or_start_cold(model, store)
    schedule = CheckpointSchedule(
        int(context.number("learned_state_checkpoint_interval"))
    )

    def checkpoint(model: BearConvictionModel) -> None:
        observations = model.training_observations
        if not schedule.is_due(observations):
            return
        store.save(PART_ID, COMPONENT, model.state(), model.learned_settings())
        schedule.record_written(observations)
        model.standing.checkpoints_written += 1

    return run_bear_conviction_model(
        model=model,
        control_socket=context.control_socket,
        read_vectors_flags_and_labels=read_vectors_flags_and_labels,
        publish_convictions=publish_convictions,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        checkpoint=checkpoint,
    )
