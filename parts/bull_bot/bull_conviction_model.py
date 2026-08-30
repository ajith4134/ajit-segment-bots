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

import pathlib
import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, RawConviction
from runtime.learned_state import (
    STARTED_COLD_UNREADABLE,
    CheckpointSchedule,
    LearnedStateStore,
)
from runtime.online_learner import OnlineLogisticModel
from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-conviction-model"
BOT = "bull-bot"

# What this part stores under its own name in the learned-state directory. A part
# may hold more than one learned thing, so the checkpoint is addressed by
# (part, component) rather than by part alone.
COMPONENT = "conviction"

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
    # Forecasts the gate could not judge at all. Counted apart from the refusals
    # because "nothing to compare it with" is a fact about the gate and being
    # out of distribution is a fact about the forecast.
    forecasts_unjudgeable: int = 0
    refused_nothing_usable: int = 0
    labels_trained_on: int = 0
    retrains: int = 0
    champion_swaps: int = 0
    rewards_applied: int = 0
    rewards_uninterpretable: int = 0
    last_uninterpretable_reward: str | None = None
    live_model: str = CHAMPION
    mean_absolute_error: float = 0.0
    by_symbol: dict = field(default_factory=dict)
    # Where this process's model came from, and what it has written since. Held on
    # the standing rather than kept privately because "the bot started cold again"
    # is exactly the fact an operator needs and the one a restart hides.
    checkpoint_verdict: str | None = None
    checkpoint_detail: str | None = None
    checkpoint_saved_at_ns: int | None = None
    checkpoints_written: int = 0


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

    def observe_forecast_flag(
        self, venue_id: str, symbol: str, is_out_of_distribution: bool, was_judged: bool = True,
    ) -> None:
        """A verdict on the forecast, or the gate saying it had nothing to judge.

        Unjudged is not flagged. A gate with no training statistics for a model
        has not found the forecast unlike anything -- it has found nothing to
        compare it with -- so the forecast is dropped and this model forms its
        conviction from the features alone, exactly as it does when no forecaster
        is running. Refusing instead is how a base model nobody has fine-tuned
        yet stopped every conviction on 2026-08-26: 3,523 of 3,523 checks flagged
        for no statistics, 424 convictions refused, none formed.
        """
        key = (venue_id, symbol)
        if not was_judged:
            self._forecasts.pop(key, None)
            self._forecast_flagged.discard(key)
            self.standing.forecasts_unjudgeable += 1
            return
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

    def note_uninterpretable_reward(self, reason: str) -> None:
        """A `learning-reward` arrived that cannot be read as a weight multiplier.

        `reward-shaper` publishes a shaped, signed figure in units nobody has
        stated a conversion for, and this part's multiplier is a positive number
        around one. Turning one into the other is a decision -- and a wrong one
        would silently scale every future training step -- so it is refused and
        counted here rather than guessed at the call site.

        The multiplier that *is* defined arrives separately as `sample-weight`,
        which this part already reads and applies per example.
        """
        self.standing.rewards_uninterpretable += 1
        self.standing.last_uninterpretable_reward = reason

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

    # -- what this part carries across the off switch ------------------------

    def learned_settings(self) -> dict:
        """The settings that give the stored coefficients their meaning.

        Taken from the champion, because both models are constructed from the same
        `self._settings` and a divergence between them would be a bug in this
        class rather than a state a checkpoint should try to express.
        """
        return self._models[CHAMPION].learned_settings()

    def state(self) -> dict:
        """Both models, which one is live, and what training has cost so far.

        The challenger is stored beside the champion because it is the thing a
        promotion would make live: dropping it would mean every restart threw away
        the only candidate that could replace a decaying champion, and the bot
        would be permanently stuck with whichever model it happened to hold.

        The error total is stored so that `mean_absolute_error` after a restart is
        the mean over everything this model trained on rather than over whatever
        arrived since the process started -- a training error that resets is a
        number that looks like improvement every time the machine reboots.
        """
        return {
            "live": self._live,
            "models": {name: model.state() for name, model in self._models.items()},
            "labels_trained_on": self.standing.labels_trained_on,
            "absolute_error_total": self._absolute_error_total,
        }

    def restore_state(self, state: dict) -> None:
        for name, stored in state["models"].items():
            if name not in self._models:
                raise ValueError(
                    f"the checkpoint holds a model named {name!r} that this part does not run"
                )
            self._models[name].restore_state(stored)
        live = state["live"]
        if live not in self._models:
            raise ValueError(f"the checkpoint names {live!r} live and this part does not hold it")
        self._live = live
        self.standing.live_model = live
        self.standing.labels_trained_on = int(state["labels_trained_on"])
        self._absolute_error_total = float(state["absolute_error_total"])
        if self.standing.labels_trained_on:
            self.standing.mean_absolute_error = (
                self._absolute_error_total / self.standing.labels_trained_on
            )

    @property
    def training_observations(self) -> int:
        """How many labelled outcomes the live model has been trained on.

        The live model's own count, not the standing's: the standing counts what
        this part did, and after a `retrain-request` the retrained model's count
        is the one that decides whether it is fitted.
        """
        return self._models[self._live].observations


def describe_conviction(model: BullConvictionModel) -> dict:
    return {
        "part_id": PART_ID,
        "live_model": model.live_model_name,
        "convictions_formed": model.standing.convictions_formed,
        "refused_features_out_of_distribution": model.standing.refused_features_flagged,
        "refused_forecast_out_of_distribution": model.standing.refused_forecast_flagged,
        "forecasts_the_gate_could_not_judge": model.standing.forecasts_unjudgeable,
        "refused_nothing_usable": model.standing.refused_nothing_usable,
        "labels_trained_on": model.standing.labels_trained_on,
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


def restore_or_start_cold(model: BullConvictionModel, store, part_id: str = PART_ID) -> None:
    """Adopt the previous process's model, or record why this one starts cold.

    Never raises past a part's start. A checkpoint that cannot be adopted is a
    reason to begin learning again, not a reason for the bot to refuse to run --
    and the reason is put on the standing so the board reports a cold start
    instead of showing an untrained model with no explanation.
    """
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


def run_bull_conviction_model(
    model: BullConvictionModel, control_socket, read_vectors_flags_and_labels,
    publish_convictions, health_interval_seconds: float, emit_health,
    checkpoint=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """`checkpoint` is called with the model whenever it may be worth storing.

    Called on every tick rather than only after training, because whether enough
    has been learned to be worth an fsync is the schedule's decision and not this
    loop's -- and because a part that only checkpointed inside the training branch
    would never write the very first one on a quiet market.
    """
    def tick() -> None:
        vectors_with_flags = read_vectors_flags_and_labels(model)
        convictions = []
        for vector, is_flagged in vectors_with_flags:
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
        read_standing=lambda: describe_conviction(model),
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

    vectors = Batch(read=context.bus.reader("bull-feature-vector"))
    flags = LatestByKey(
        read=context.bus.reader("bull-feature-out-of-distribution-flag"),
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
    publish_convictions = context.bus.publisher_for("bull-raw-conviction")

    # Vectors kept per symbol with when they were built, so a label that arrives a
    # horizon later can find the one that was current when the claim was made.
    # Bounded by what the horizon can span, because this is a process's memory and
    # an unbounded history of vectors is a leak with a good excuse.
    remembered: dict[tuple[str, str], list] = {}
    remembered_per_symbol = int(context.number("bull_remembered_vectors_per_symbol"))

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
            model.observe_forecast_flag(
                flag.venue_id,
                flag.symbol,
                flag.is_out_of_distribution,
                # Stated by the gate on the flag itself, so this part reads a
                # field rather than another block's state vocabulary (T-4).
                getattr(flag, "was_judged", True),
            )
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
            vector = vector_current_at(label.venue_id, label.symbol, label.feature_lookup_at_ns)
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
                label=outcome,
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

    model = BullConvictionModel(
        learning_rate=context.number("bull_learning_rate"),
        l2_regularisation=context.number("bull_l2_regularisation"),
        feature_half_life_observations=context.number("bull_feature_half_life_observations"),
        minimum_feature_observations=int(context.number("bull_minimum_feature_observations")),
        minimum_training_observations=int(context.number("bull_minimum_training_observations")),
        default_sample_weight=context.number("bull_default_sample_weight"),
        maximum_sample_weight=context.number("bull_maximum_sample_weight"),
    )

    # What this bot has learned, carried across the off switch. Without it the
    # model was rebuilt empty on every fork, and since it needs
    # bull_minimum_training_observations outcomes of each class before its
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

    def checkpoint(model: BullConvictionModel) -> None:
        observations = model.training_observations
        if not schedule.is_due(observations):
            return
        store.save(PART_ID, COMPONENT, model.state(), model.learned_settings())
        schedule.record_written(observations)
        model.standing.checkpoints_written += 1

    return run_bull_conviction_model(
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
