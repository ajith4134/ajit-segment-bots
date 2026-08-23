"""forgetting-auditor: whether the system still knows what it learned.

Every online model in this system is trained continuously, and continuous
training forgets. A model finetuned through a trending month gradually loses what
it knew about a choppy one, and nothing in its recent accuracy shows it -- because
the market has been trending, so the thing it forgot has not been tested.

The forgetting surfaces exactly when the old regime returns, which is the worst
possible moment.

So this part tests **recall**, not accuracy: it replays episodes the model was
trained on and asks whether the model still gets them right.

- **Old episodes, deliberately.** Testing on recent data measures fit, not
  memory. The episodes that matter are the ones from conditions the system has
  not seen lately.
- **By era, not in aggregate.** A single recall number over all history hides
  which period was forgotten, and which period was forgotten is the whole answer.
- **A model with no replayable episodes cannot be audited**, and that is
  reported. Silence would be read as intact memory, which is the failure this
  part exists to prevent.

**It does not retrain.** Recall is a measurement; what to do about a model that
has forgotten belongs to the drift monitor and the finetuner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forgetting-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="forgetting-auditor",
    consumes=("model-version", "recalled-episode", "trade-episode"),
    produces=("forgetting-report", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

AUDITED = "audited"
NOTHING_TO_REPLAY = "no-episode-old-enough-to-test-memory-with"
FORGETTING = "the-model-has-forgotten-an-era-it-was-trained-on"
INTACT = "recall-is-intact"


@dataclass(frozen=True)
class EraRecall:
    """How much of one era a model still gets right."""

    era: str
    recall: Estimate
    episodes_replayed: int
    episodes_recalled: int
    is_forgotten: bool
    reason: str


@dataclass(frozen=True)
class ForgettingReport:
    """What a model still knows, era by era."""

    model_name: str
    state: str
    overall_recall: float | None
    by_era: tuple
    forgotten_eras: tuple
    episodes_replayed: int
    reason: str
    audited_at_ns: int

    @property
    def has_forgotten(self) -> bool:
        return bool(self.forgotten_eras)

    @property
    def is_measured(self) -> bool:
        return self.state == AUDITED


@dataclass
class AuditorStanding:
    audits: int = 0
    audited: int = 0
    refused_nothing_to_replay: int = 0
    episodes_replayed: int = 0
    models_forgetting: int = 0
    by_forgotten_era: dict = field(default_factory=dict)
    lowest_recall_seen: float | None = None


class ForgettingAuditor:
    """Replays old episodes past a model and measures how much it still gets right."""

    def __init__(
        self,
        minimum_episodes_per_era: int,
        recall_threshold: float,
        prior_recall: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < recall_threshold < 1.0:
            raise ValueError("recall is a fraction and its threshold must be inside (0, 1)")
        self._minimum = minimum_episodes_per_era
        self._threshold = recall_threshold
        self._prior_recall = prior_recall
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._episodes: dict[str, list] = {}
        self._recall: dict[tuple[str, str], RateEstimator] = {}
        self.standing = AuditorStanding()

    def observe_training_episode(self, era: str, episode) -> None:
        """One episode a model was trained on, kept so its memory can be tested.

        Old episodes deliberately: testing on recent data measures fit rather
        than memory, and the era that matters is the one the market has not been
        in lately.
        """
        self._episodes.setdefault(era, []).append(episode)

    def observe_recall(self, model_name: str, era: str, was_recalled: bool) -> None:
        """The result of replaying one old episode past the model."""
        self._recall_for(model_name, era).observe(was_recalled)
        self.standing.episodes_replayed += 1

    def replay(self, model_name: str, era: str, predict) -> EraRecall:
        """Replay every episode from one era and count what the model still gets right."""
        episodes = self._episodes.get(era, [])
        recalled = 0
        for episode in episodes:
            was_right = bool(predict(episode))
            self.observe_recall(model_name, era, was_right)
            recalled += 1 if was_right else 0

        recall = self._recall_for(model_name, era).estimate(self._minimum)
        forgotten = recall.is_fitted and recall.value < self._threshold
        return EraRecall(
            era=era,
            recall=recall,
            episodes_replayed=len(episodes),
            episodes_recalled=recalled,
            is_forgotten=forgotten,
            reason=(
                f"{model_name} still gets {recall.value:.0%} of the {era} era right over "
                f"{recall.observations} replayed episode(s)"
                + (
                    f", below the {self._threshold:.0%} that counts as intact -- and this "
                    f"surfaces when that era returns, which is the worst possible moment"
                    if forgotten
                    else ""
                )
            ),
        )

    def audit(self, model_name: str) -> ForgettingReport:
        self.standing.audits += 1
        eras = sorted(
            era for era, episodes in self._episodes.items() if len(episodes) >= self._minimum
        )

        if not eras:
            self.standing.refused_nothing_to_replay += 1
            return ForgettingReport(
                model_name=model_name,
                state=NOTHING_TO_REPLAY,
                overall_recall=None,
                by_era=(),
                forgotten_eras=(),
                episodes_replayed=0,
                reason=(
                    f"no era has {self._minimum} replayable episode(s), so this model's "
                    f"memory cannot be tested. Reported rather than passed over: silence "
                    f"would be read as intact memory"
                ),
                audited_at_ns=self._now_ns(),
            )

        recalls = []
        for era in eras:
            estimate = self._recall_for(model_name, era).estimate(self._minimum)
            forgotten = estimate.is_fitted and estimate.value < self._threshold
            recalls.append(
                EraRecall(
                    era=era,
                    recall=estimate,
                    episodes_replayed=estimate.observations,
                    episodes_recalled=int(estimate.value * estimate.observations),
                    is_forgotten=forgotten,
                    reason=f"{estimate.value:.0%} recall over {estimate.observations} episode(s)",
                )
            )

        measured = [entry for entry in recalls if entry.recall.is_fitted]
        forgotten = tuple(entry.era for entry in recalls if entry.is_forgotten)
        overall = (
            sum(entry.recall.value for entry in measured) / len(measured) if measured else None
        )

        if forgotten:
            self.standing.models_forgetting += 1
            for era in forgotten:
                self.standing.by_forgotten_era[era] = (
                    self.standing.by_forgotten_era.get(era, 0) + 1
                )
        if overall is not None and (
            self.standing.lowest_recall_seen is None
            or overall < self.standing.lowest_recall_seen
        ):
            self.standing.lowest_recall_seen = overall

        self.standing.audited += 1
        return ForgettingReport(
            model_name=model_name,
            state=AUDITED,
            overall_recall=overall,
            by_era=tuple(recalls),
            forgotten_eras=forgotten,
            episodes_replayed=sum(entry.episodes_replayed for entry in recalls),
            reason=(
                f"{model_name} recalls "
                + (f"{overall:.0%} " if overall is not None else "an unmeasured amount ")
                + f"across {len(recalls)} era(s)"
                + (
                    f"; forgotten: {', '.join(forgotten)}. Reported by era rather than in "
                    f"aggregate, because which period was forgotten is the whole answer"
                    if forgotten
                    else "; nothing is forgotten"
                )
                + ". This measures memory and does not retrain -- what to do about it belongs "
                "to the drift monitor and the finetuner"
            ),
            audited_at_ns=self._now_ns(),
        )

    def _recall_for(self, model_name: str, era: str) -> RateEstimator:
        key = (model_name, era)
        estimator = self._recall.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_recall, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._recall[key] = estimator
        return estimator


def describe_forgetting(auditor: ForgettingAuditor) -> dict:
    return {
        "part_id": PART_ID,
        "audits": auditor.standing.audits,
        "audited": auditor.standing.audited,
        "refused_nothing_to_replay": auditor.standing.refused_nothing_to_replay,
        "episodes_replayed": auditor.standing.episodes_replayed,
        "models_forgetting": auditor.standing.models_forgetting,
        "by_forgotten_era": dict(sorted(auditor.standing.by_forgotten_era.items())),
        "lowest_recall_seen": auditor.standing.lowest_recall_seen,
        "eras_held": sorted(auditor._episodes),
        "retrains": False,
    }


def run_forgetting_auditor(
    auditor: ForgettingAuditor, control_socket, read_models_and_episodes, publish_reports,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        models = read_models_and_episodes(auditor)
        publish_reports(tuple(auditor.audit(model) for model in models))

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

    Every closed episode is kept under the era it opened in -- its calendar
    month -- so each model version registered can be asked whether it still
    recalls the eras before the one it was trained in. No model here exposes
    a predictor to replay against, so recall is estimated only from what has
    been observed, and a model with nothing observed is reported as having
    nothing to replay, not as intact. Recalled episodes are the store's
    memory, not a model's; they are read and drained.
    """
    import datetime

    from runtime.input_assembly import Batch

    versions = Batch(read=context.bus.reader("model-version"))
    recalled = Batch(read=context.bus.reader("recalled-episode"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    publish_reports = context.bus.publisher_for("forgetting-report")
    auditor = ForgettingAuditor(
        minimum_episodes_per_era=int(context.number("decoding_minimum_trades")),
        recall_threshold=context.number("forgetting_recall_threshold"),
        prior_recall=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    models_seen: set[str] = set()

    def era_of(opened_at_ns: int) -> str:
        return datetime.datetime.fromtimestamp(opened_at_ns / 1e9, datetime.UTC).strftime("%Y-%m")

    def read_models_and_episodes(_auditor):
        recalled.payloads()
        for episode in episodes.payloads():
            auditor.observe_training_episode(era_of(int(episode.opened_at_ns)), episode)
        for version in versions.payloads():
            models_seen.add(str(version.model_name))
        return tuple(sorted(models_seen))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_reports(kept)

    return run_forgetting_auditor(
        auditor=auditor,
        control_socket=context.control_socket,
        read_models_and_episodes=read_models_and_episodes,
        publish_reports=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
