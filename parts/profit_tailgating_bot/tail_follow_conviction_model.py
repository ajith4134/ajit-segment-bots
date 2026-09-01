"""tail-follow-conviction-model: how likely joining this move is to pay, and it calibrates itself.

The one part in this bot that judges (RL-060), and the only conviction model in
the system that produces a **calibrated** conviction directly rather than handing
a raw number to a separate calibrator. That is not a shortcut -- it is the
blueprint's shape, and the reason is that this bot's question is narrow enough to
be answered by one record: not "will this move happen" but "when a move this far
along, this crowded, with this much left, has been joined, how often did it pay".

Two features carry most of the weight and both are readings rather than raw
numbers:

- **What is left of the move**, and how much the independent views of it
  disagreed. A move three views agree has 2% left is a different trade from one
  where they range 0.5% to 4%, and a model given only the midpoint cannot tell
  them apart. So the disagreement is its own feature.
- **How crowded it is**, per source. The book, funding and sentiment crowd on
  different clocks, and which one tripped predicts different things.

**A crowded move is refused before the model sees it.** Not scored low: crowding
inverts the setup rather than weakening it, and a model that could learn its way
around the refusal would eventually do so on a sample of the crowded moves that
happened to work.

**Champion and challenger, promoted by control** (T-2), same as the directional
bots.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    ESTIMATES_DISAGREE, FROM_ORDER_FLOW, FROM_THE_BOOK, CalibratedConviction,
)
from runtime.online_learner import OnlineLogisticModel, ProbabilityCalibrator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-follow-conviction-model"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-follow-conviction-model",
    consumes=(
        "follow-candidate", "move-remaining", "crowding-reading", "bot-scorecard",
        "training-label", "sample-weight", "retrain-request", "champion-choice",
        "learning-reward",
    ),
    produces=("tail-calibrated-conviction", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CHAMPION = "champion"
CHALLENGER = "challenger"

IS_CROWDED = "the-move-is-already-crowded"
CROWDING_UNKNOWN = "crowding-could-not-be-read"
NOTHING_LEFT = "no-estimate-of-what-is-left-of-the-move"
VIEWS_DISAGREE = "the-views-of-what-is-left-disagree"
NOTHING_USABLE = "no-feature-could-be-standardised"


@dataclass
class ModelStanding:
    convictions_formed: int = 0
    refused_crowded: int = 0
    refused_crowding_unknown: int = 0
    refused_nothing_left: int = 0
    refused_views_disagree: int = 0
    refused_nothing_usable: int = 0
    labels_trained_on: int = 0
    rewards_uninterpretable: int = 0
    last_uninterpretable_reward: str | None = None
    checkpoints_written: int = 0
    checkpoint_verdict: str | None = None
    retrains: int = 0
    champion_swaps: int = 0
    live_model: str = CHAMPION
    by_source: dict = field(default_factory=dict)


class TailFollowConvictionModel:
    """Judges whether joining a move pays, and calibrates against its own record."""

    def __init__(
        self,
        learning_rate: float,
        l2_regularisation: float,
        feature_half_life_observations: float,
        minimum_feature_observations: int,
        minimum_training_observations: int,
        calibration_bin_count: int,
        calibration_minimum_observations: int,
        calibration_half_life: float,
        default_sample_weight: float,
        maximum_sample_weight: float,
        refuse_when_views_disagree: bool,
        now_ns=time.time_ns,
    ) -> None:
        if default_sample_weight <= 0 or maximum_sample_weight < default_sample_weight:
            raise ValueError(
                "the default weight must be positive and the cap must not sit below it"
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
        self._refuse_on_disagreement = refuse_when_views_disagree
        self._now_ns = now_ns
        self._models = {
            CHAMPION: OnlineLogisticModel(**self._settings),
            CHALLENGER: OnlineLogisticModel(**self._settings),
        }
        self._live = CHAMPION
        self._calibrators: dict[str, ProbabilityCalibrator] = {}
        self._calibration_settings = dict(
            bin_count=calibration_bin_count,
            minimum_observations=calibration_minimum_observations,
            half_life_observations=calibration_half_life,
        )
        self._reward_multipliers: dict[str, float] = {}
        self.standing = ModelStanding()

    # -- the features this bot judges on -------------------------------------

    def features_for(self, candidate, remaining, crowding) -> dict:
        """What the model looks at, built from readings rather than raw numbers.

        The disagreement between the independent views of what is left is its own
        feature: a move three views agree has 2% left is a different trade from
        one where they range 0.5% to 4%, and a midpoint cannot express that.
        """
        features = {
            "fraction_of_a_normal_move_done": candidate.fraction_of_a_normal_move_done,
            "observations_in_move": float(candidate.observations_in_move),
            "setup_weight": candidate.setup_weight,
        }
        if candidate.entry_cost_fraction is not None:
            features["entry_cost_fraction"] = candidate.entry_cost_fraction

        if remaining.remaining_fraction is not None:
            features["move_remaining_fraction"] = remaining.remaining_fraction
            features["views_contributing"] = float(len(remaining.estimates))
            spread = remaining.spread
            if spread is not None and remaining.highest:
                features["view_disagreement"] = spread / remaining.highest
            if remaining.remaining_fraction > 0 and candidate.entry_cost_fraction:
                features["remaining_over_cost"] = (
                    remaining.remaining_fraction / candidate.entry_cost_fraction
                )

        for source in (FROM_THE_BOOK, FROM_ORDER_FLOW):
            value = crowding.readings.get(source)
            if value is not None:
                features[f"crowding_{source}"] = value
        return features

    # -- training ------------------------------------------------------------

    def train(self, features: dict, was_profitable: bool, source: str, sample_weight: float | None = None) -> float:
        weight = min(
            self._maximum_sample_weight,
            (self._default_sample_weight if sample_weight is None else sample_weight)
            * self._reward_multipliers.get(source, 1.0),
        )
        error = 0.0
        for name, model in self._models.items():
            model_error = model.train(features, was_profitable, weight)
            if name == self._live:
                error = model_error
        self.standing.labels_trained_on += 1
        self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1
        return error

    def observe_outcome(self, stated_probability: float, was_profitable: bool, source: str) -> None:
        """One closed follow, into the calibration record for its own source.

        Per source because the three ways this bot finds a move fail differently:
        a scanner move can be a print, our own winner can be a position we are
        already too big in, a copy can be one we were too slow for. One
        calibration over all three would describe none of them.
        """
        self._calibrator_for(source).observe_outcome(stated_probability, was_profitable)

    def observe_scorecard(self, scorecard) -> None:
        for band, record in scorecard.describe()["by_probability_band"].items():
            low, high = (float(part) for part in band.split("-"))
            midpoint = (low + high) / 2
            for _ in range(record["wins"]):
                self.observe_outcome(midpoint, True, "all-sources")
            for _ in range(record["trades"] - record["wins"]):
                self.observe_outcome(midpoint, False, "all-sources")

    def observe_learning_reward(self, source: str, multiplier: float) -> None:
        """A positive weight, stating how much this source's examples should count."""
        if multiplier <= 0:
            raise ValueError("a non-positive multiplier would unlearn or erase the example")
        self._reward_multipliers[source] = min(multiplier, self._maximum_sample_weight)

    def note_uninterpretable_reward(self, reason: str) -> None:
        """A `learning-reward` arrived that cannot be read as a weight multiplier.

        The shaped reward is signed: USDT times a set of components, negative for
        a losing trade. The multiplier this model applies to a training example is
        a positive number around one. No conversion between them has been decided,
        and inventing one would silently rescale every future training step.

        Counted rather than raised. Passing the signed figure straight in took
        this part down on 2026-08-28 on the first losing trade it was told about,
        and a losing trade is ordinary traffic.
        """
        self.standing.rewards_uninterpretable += 1
        self.standing.last_uninterpretable_reward = reason

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
                "the live model cannot be retrained from empty while it is being acted on"
            )
        self._models[which] = OnlineLogisticModel(**self._settings)
        self.standing.retrains += 1

    # -- forming a conviction ------------------------------------------------

    def form_conviction(self, candidate, remaining, crowding) -> tuple[CalibratedConviction | None, str]:
        if not crowding.is_measured:
            self.standing.refused_crowding_unknown += 1
            return None, CROWDING_UNKNOWN

        if crowding.is_crowded:
            # Refused before the model sees it. Crowding inverts this setup
            # rather than weakening it, and a model allowed to score its way
            # around that would eventually learn to, on the sample of crowded
            # moves that happened to work.
            self.standing.refused_crowded += 1
            return None, IS_CROWDED

        if remaining.remaining_fraction is None:
            self.standing.refused_nothing_left += 1
            return None, NOTHING_LEFT

        if self._refuse_on_disagreement and remaining.agreement == ESTIMATES_DISAGREE:
            self.standing.refused_views_disagree += 1
            return None, VIEWS_DISAGREE

        features = self.features_for(candidate, remaining, crowding)
        belief = self._models[self._live].believe(features)
        if belief.features_used == 0:
            self.standing.refused_nothing_usable += 1
            return None, NOTHING_USABLE

        calibrated = self._calibrator_for(candidate.source).calibrate(belief.probability)
        self.standing.convictions_formed += 1

        return (
            CalibratedConviction(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=candidate.direction,
                raw_probability=belief.probability,
                calibrated=calibrated,
                scorecard_observations=calibrated.observations,
                reason=(
                    f"joining {candidate.symbol} {candidate.fraction_of_a_normal_move_done:.0%} "
                    f"into a normal move with {remaining.remaining_fraction:.2%} left "
                    f"({remaining.agreement}); the {self._live} model puts it at "
                    f"{belief.probability:.1%} and follows from {candidate.source} have "
                    f"actually paid {calibrated.value:.1%} of the time "
                    f"({'measured' if calibrated.is_fitted else 'not yet measured, so the model stands'})"
                ),
                calibrated_at_ns=self._now_ns(),
            ),
            "formed",
        )

    def reliability(self, source: str) -> tuple:
        calibrator = self._calibrators.get(source)
        return () if calibrator is None else calibrator.reliability()

    def _calibrator_for(self, source: str) -> ProbabilityCalibrator:
        calibrator = self._calibrators.get(source)
        if calibrator is None:
            calibrator = ProbabilityCalibrator(**self._calibration_settings)
            self._calibrators[source] = calibrator
        return calibrator

    @property
    def live_model_name(self) -> str:
        return self._live

    def model(self, name: str) -> OnlineLogisticModel:
        return self._models[name]

    # -- what survives a restart ----------------------------------------------
    #
    # None of this existed until 2026-08-25. `start_part` asked
    # `hasattr(model, "state")` and `hasattr(model, "restore_state")` before
    # checkpointing, both were false, and so **this bot's model has never been
    # saved and never been restored**: every restart began again from the prior,
    # and the guard that hid it read like caution. The bull bot has had these
    # since its checkpoint was built; this is the same shape, so a reader who
    # knows one knows the other.

    def learned_settings(self) -> dict:
        """The settings that give the stored coefficients their meaning.

        The champion's, because both models are built from the same settings and a
        divergence between them would be a bug in this class rather than a state a
        checkpoint should try to express.
        """
        return self._models[CHAMPION].learned_settings()

    def state(self) -> dict:
        """Both models, which one is live, what it has trained on, and its calibration.

        The calibrators are stored with the models because a restored model whose
        calibration was dropped states probabilities it has no record for -- which
        is the one thing calibration exists to prevent, and it would come back
        overconfident at exactly the size the size hint scales with.
        """
        return {
            "live": self._live,
            "models": {name: model.state() for name, model in self._models.items()},
            "labels_trained_on": self.standing.labels_trained_on,
            "trained_by_source": dict(self.standing.by_source),
            "calibrators": {
                source: calibrator.state() for source, calibrator in self._calibrators.items()
            },
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
        self.standing.by_source = dict(state.get("trained_by_source", {}))
        for source, stored in state.get("calibrators", {}).items():
            self._calibrator_for(source).restore_state(stored)

    @property
    def training_observations(self) -> int:
        """How many labelled outcomes the live model has been trained on."""
        return self._models[self._live].observations


def describe_follow_conviction(model: TailFollowConvictionModel) -> dict:
    return {
        "part_id": PART_ID,
        "live_model": model.live_model_name,
        "convictions_formed": model.standing.convictions_formed,
        "refused_because_crowded": model.standing.refused_crowded,
        "refused_because_crowding_unknown": model.standing.refused_crowding_unknown,
        "refused_because_nothing_left": model.standing.refused_nothing_left,
        "refused_because_views_disagree": model.standing.refused_views_disagree,
        "refused_nothing_usable": model.standing.refused_nothing_usable,
        "labels_trained_on": model.standing.labels_trained_on,
        "rewards_uninterpretable": model.standing.rewards_uninterpretable,
        "last_uninterpretable_reward": model.standing.last_uninterpretable_reward,
        "checkpoints_written": model.standing.checkpoints_written,
        "checkpoint_verdict": model.standing.checkpoint_verdict,
        "trained_by_source": dict(sorted(model.standing.by_source.items())),
        "retrains": model.standing.retrains,
        "champion_swaps": model.standing.champion_swaps,
        "champion": model.model(CHAMPION).describe(),
        "sources_calibrated": sorted(model._calibrators),
    }


def run_tail_follow_conviction_model(
    model: TailFollowConvictionModel, control_socket, read_candidates_and_readings,
    publish_convictions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        convictions = []
        for candidate, remaining, crowding in read_candidates_and_readings(model):
            conviction, _ = model.form_conviction(candidate, remaining, crowding)
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
        read_standing=lambda: describe_follow_conviction(model),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A conviction is formed for every follow candidate whose move-remaining
    and crowding readings have both arrived. Training labels for the bot's
    own follows train it; scorecards, rewards, champion choices and retrain
    requests are applied as they arrive. What it learns is checkpointed
    under learned_state_root, as the bull and bear models are.
    """
    import pathlib

    from runtime.input_assembly import Batch, LatestByKey
    from runtime.learned_state import CheckpointSchedule, LearnedStateStore

    candidates = Batch(read=context.bus.reader("follow-candidate"))
    remaining = LatestByKey(read=context.bus.reader("move-remaining"), key_of=lambda r: (r.venue_id, r.symbol))
    crowding = LatestByKey(read=context.bus.reader("crowding-reading"), key_of=lambda c: (c.venue_id, c.symbol))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    labels = Batch(read=context.bus.reader("training-label"))
    weights = LatestByKey(read=context.bus.reader("sample-weight"), key_of=lambda w: (w.venue_id, w.symbol))
    retrains = Batch(read=context.bus.reader("retrain-request"))
    champions = Batch(read=context.bus.reader("champion-choice"))
    rewards = Batch(read=context.bus.reader("learning-reward"))
    publish_convictions = context.bus.publisher_for("tail-calibrated-conviction")
    model = TailFollowConvictionModel(
        learning_rate=context.number("bull_learning_rate"),
        l2_regularisation=context.number("bull_l2_regularisation"),
        feature_half_life_observations=context.number("bull_feature_half_life_observations"),
        minimum_feature_observations=int(context.number("bull_minimum_feature_observations")),
        minimum_training_observations=int(context.number("bull_minimum_training_observations")),
        calibration_bin_count=int(context.number("bull_calibration_bin_count")),
        calibration_minimum_observations=int(context.number("bull_calibration_minimum_observations")),
        calibration_half_life=context.number("bull_calibration_half_life_observations"),
        default_sample_weight=context.number("bull_default_sample_weight"),
        maximum_sample_weight=context.number("bull_maximum_sample_weight"),
        refuse_when_views_disagree=True,
    )
    store = LearnedStateStore(pathlib.Path(str(context.setting("learned_state_root").value)).expanduser())
    store.root.mkdir(parents=True, exist_ok=True)
    # No hasattr guard: it was here until 2026-08-25, both attributes were
    # missing, and the guard turned "this bot never remembers anything" into a
    # silent no-op that looked like caution.
    restoration = store.restore(PART_ID, "conviction", model.learned_settings())
    if restoration.was_restored:
        model.restore_state(restoration.state)
    model.standing.checkpoint_verdict = restoration.verdict
    schedule = CheckpointSchedule(int(context.number("learned_state_checkpoint_interval")))
    remembered: dict[tuple[str, str], object] = {}

    def read_candidates_and_readings(_model):
        for scorecard in scorecards.payloads():
            if getattr(scorecard, "bot", None) == BOT:
                model.observe_scorecard(scorecard)
        for reward in rewards.payloads():
            model.note_uninterpretable_reward(
                f"{reward.detector} sent a shaped reward of {reward.reward!r} in state "
                f"{reward.state!r}; no conversion from a shaped reward to a weight "
                f"multiplier has been decided, and a signed figure cannot be one"
            )
        for choice in champions.payloads():
            if getattr(choice, "model_name", None) == PART_ID:
                # `promotes` rather than the decision string: the gate decides
                # between "promote-the-challenger" and "keep-the-champion", and this
                # part holds roles named "champion" and "challenger". Passing the
                # gate's own word through raises ValueError here, because it is not
                # a role -- one vocabulary translated at the boundary, not two
                # vocabularies hoping to match (T-5).
                model.apply_champion_choice("challenger" if choice.promotes else "champion")
        for request in retrains.payloads():
            if request.model_name == PART_ID and request.state == "scheduled":
                model.apply_retrain_request("challenger")
        weight_by_symbol = weights.mapping()
        for label in labels.payloads():
            candidate = remembered.get((label.venue_id, label.symbol))
            if candidate is None or candidate.detector != label.detector:
                continue
            outcome = label.labels.get("the-setup-was-right") if isinstance(label.labels, dict) else None
            if outcome is None:
                continue
            weight = weight_by_symbol.get((label.venue_id, label.symbol))
            model.train(
                model.features_for(candidate, remaining.mapping().get((label.venue_id, label.symbol)), crowding.mapping().get((label.venue_id, label.symbol))),
                bool(outcome), candidate.source, getattr(weight, "weight", None),
            )
        by_symbol_remaining = remaining.mapping()
        by_symbol_crowding = crowding.mapping()
        jobs = []
        for candidate in candidates.payloads():
            key = (candidate.venue_id, candidate.symbol)
            remembered[key] = candidate
            if key in by_symbol_remaining and key in by_symbol_crowding:
                jobs.append((candidate, by_symbol_remaining[key], by_symbol_crowding[key]))
        return tuple(jobs)

    def publish(convictions) -> None:
        if convictions:
            publish_convictions(convictions)
        observations = model.training_observations
        if schedule.is_due(observations):
            store.save(PART_ID, "conviction", model.state(), model.learned_settings())
            schedule.record_written(observations)
            model.standing.checkpoints_written += 1

    return run_tail_follow_conviction_model(
        model=model,
        control_socket=context.control_socket,
        read_candidates_and_readings=read_candidates_and_readings,
        publish_convictions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
