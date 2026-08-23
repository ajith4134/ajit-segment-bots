"""feature-attribution-tracker: which features actually moved each conviction.

A conviction of 0.71 that cannot be decomposed is a number nobody can argue with,
and a system full of those cannot be debugged -- when it starts being wrong,
there is nowhere to look.

This part records, for every conviction, how much each feature contributed. That
is cheap for the models this system uses -- an online logistic model's
contribution is the coefficient times the standardised feature -- and it is what
makes four other parts possible: the reliability scorer, the refutation battery's
claimed-feature test, the explainer, and the drift monitor's ability to say *what*
drifted.

**Attribution is recorded per model version.** A model that was retrained is a
different model, and pooling attributions across versions describes a model that
never existed.

**Both bots are tracked, separately.** The same feature can be the bull bot's
strongest signal and the bear bot's noise, and a single table would average them
into something true of neither.

**Attribution is not importance.** It says what moved this conviction, not
whether moving it was right -- that is the reliability scorer's question, and
conflating them is how a model's confident mistake becomes evidence for the
feature that caused it.

**Bounded.** A tracker that kept every attribution forever would outgrow the
models it describes; it keeps a decayed summary per feature and the most recent
attributions in full.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "feature-attribution-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="feature-attribution-tracker",
    consumes=(
        "model-version", "bull-feature-vector", "bear-feature-vector",
        "bull-raw-conviction", "bear-raw-conviction",
    ),
    produces=("feature-attribution", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

BULL = "bull-bot"
BEAR = "bear-bot"


@dataclass(frozen=True)
class FeatureAttribution:
    """How much each feature moved one conviction, under one model version."""

    bot: str
    model_version: str
    venue_id: str
    symbol: str
    contributions: dict
    strongest: tuple | None
    features_used: int
    features_unusable: tuple
    probability: float
    recorded_at_ns: int

    @property
    def is_decomposable(self) -> bool:
        return bool(self.contributions)

    def share_of(self, feature: str) -> float | None:
        total = sum(abs(value) for value in self.contributions.values())
        if total <= 0:
            return None
        return abs(self.contributions.get(feature, 0.0)) / total


@dataclass(frozen=True)
class AttributionSummary:
    """What a feature has typically contributed, per bot and model version."""

    bot: str
    model_version: str
    feature: str
    mean_contribution: float
    contribution_spread: float
    share_of_total: float
    observations: int
    reason: str


@dataclass
class TrackerStanding:
    attributions_recorded: int = 0
    convictions_with_nothing_to_attribute: int = 0
    model_versions_tracked: int = 0
    features_tracked: int = 0
    by_bot: dict = field(default_factory=dict)
    strongest_feature_seen: str | None = None


class FeatureAttributionTracker:
    """Records what moved each conviction, per bot and per model version."""

    def __init__(
        self,
        half_life_observations: float,
        recent_kept: int,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if recent_kept < 1:
            raise ValueError(
                "keeping no attributions in full leaves nothing to inspect when a conviction "
                "needs explaining"
            )
        self._half_life = half_life_observations
        self._recent_kept = recent_kept
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._moments: dict[tuple[str, str, str], RunningMoments] = {}
        self._recent: list = []
        self._totals: dict[tuple[str, str], float] = {}
        self.standing = TrackerStanding()

    def record(self, bot: str, model_version: str, conviction) -> FeatureAttribution:
        """One conviction, decomposed into what each feature contributed.

        Per model version, because a retrained model is a different model and
        pooling across versions describes one that never existed.
        """
        belief = conviction.belief
        contributions = dict(belief.contributions)

        attribution = FeatureAttribution(
            bot=bot,
            model_version=model_version,
            venue_id=conviction.venue_id,
            symbol=conviction.symbol,
            contributions=contributions,
            strongest=belief.strongest_reason,
            features_used=belief.features_used,
            features_unusable=belief.features_unusable,
            probability=belief.probability,
            recorded_at_ns=self._now_ns(),
        )

        if not contributions:
            self.standing.convictions_with_nothing_to_attribute += 1
            return attribution

        self.standing.attributions_recorded += 1
        self.standing.by_bot[bot] = self.standing.by_bot.get(bot, 0) + 1

        total = sum(abs(value) for value in contributions.values())
        for feature, value in contributions.items():
            key = (bot, model_version, feature)
            self._moment_for(key).observe(value)
            self._totals[(bot, model_version)] = self._totals.get((bot, model_version), 0.0) + total

        if attribution.strongest is not None:
            self.standing.strongest_feature_seen = attribution.strongest[0]

        self._recent.append(attribution)
        del self._recent[: max(0, len(self._recent) - self._recent_kept)]
        self.standing.model_versions_tracked = len(
            {version for _, version, _ in self._moments}
        )
        self.standing.features_tracked = len({feature for _, _, feature in self._moments})
        return attribution

    def summary(self, bot: str, model_version: str, feature: str) -> AttributionSummary | None:
        """What this feature has typically contributed under this version."""
        moments = self._moments.get((bot, model_version, feature))
        if moments is None or moments.count < self._minimum:
            return None
        total = self._totals.get((bot, model_version), 0.0)
        share = abs(moments.mean) / (total / max(1, moments.count)) if total else 0.0
        return AttributionSummary(
            bot=bot,
            model_version=model_version,
            feature=feature,
            mean_contribution=moments.mean,
            contribution_spread=moments.deviation,
            share_of_total=share,
            observations=moments.count,
            reason=(
                f"{feature} has moved {bot}'s conviction by {moments.mean:+.4f} on average "
                f"under {model_version} over {moments.count} conviction(s), with a spread of "
                f"{moments.deviation:.4f}. This says what moved the conviction, not whether "
                f"moving it was right -- conflating the two makes a model's confident mistake "
                f"into evidence for the feature that caused it"
            ),
        )

    def summaries_for(self, bot: str, model_version: str) -> tuple:
        features = sorted(
            feature
            for kept_bot, kept_version, feature in self._moments
            if kept_bot == bot and kept_version == model_version
        )
        return tuple(
            summary
            for summary in (self.summary(bot, model_version, feature) for feature in features)
            if summary is not None
        )

    @property
    def recent(self) -> tuple:
        return tuple(self._recent)

    def _moment_for(self, key) -> RunningMoments:
        moments = self._moments.get(key)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            self._moments[key] = moments
        return moments


def describe_attribution(tracker: FeatureAttributionTracker) -> dict:
    return {
        "part_id": PART_ID,
        "attributions_recorded": tracker.standing.attributions_recorded,
        "convictions_with_nothing_to_attribute": (
            tracker.standing.convictions_with_nothing_to_attribute
        ),
        "model_versions_tracked": tracker.standing.model_versions_tracked,
        "features_tracked": tracker.standing.features_tracked,
        "by_bot": dict(sorted(tracker.standing.by_bot.items())),
        "strongest_feature_seen": tracker.standing.strongest_feature_seen,
        "recent_kept": len(tracker.recent),
        "attribution_is_importance": False,
    }


def run_feature_attribution_tracker(
    tracker: FeatureAttributionTracker, control_socket, read_convictions, publish_attributions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_attributions(
            tuple(
                tracker.record(bot, model_version, conviction)
                for bot, model_version, conviction in read_convictions(tracker)
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
