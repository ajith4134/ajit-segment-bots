"""episode-embedder: turning a trade's conditions into something comparable.

Recall asks "what happened last time it looked like this", and answering it needs
a way to compare situations. Comparing raw conditions works and is coarse: two
situations differing in one feature out of twelve are 92% alike by that measure
whatever the feature was.

So this part produces a vector, and every choice in it is about making the
distance mean something:

- **Standardised per feature, over a decayed window.** A raw vector is dominated
  by whichever feature is measured in the largest units, and the distance then
  measures the units rather than the situation.
- **Weighted by measured reliability.** A feature that has never predicted
  anything should not push two situations apart. Unweighted, the noisiest
  features dominate the distance precisely because they vary most.
- **Missing features are marked, not zeroed.** A zero is a value, and a situation
  where funding was unavailable would otherwise be recorded as one where funding
  was exactly average.
- **The vector is not the situation.** It is a lossy index into the episodes, and
  the episodic store falls back to the raw conditions when there is no embedding
  -- an embedder that went down would otherwise make the system believe nothing
  like this had ever happened.

**Deterministic.** The same conditions produce the same vector, so recall is
reproducible and a surprising recall can be investigated rather than re-rolled.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "episode-embedder"

PART_DECLARATION = PartDeclaration(
    part_id="episode-embedder",
    consumes=("trade-episode",),
    produces=("episode-embedding", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

EMBEDDED = "embedded"
TOO_FEW_FEATURES = "too-few-features-could-be-standardised"
NO_FEATURES = "the-episode-carries-no-conditions"


@dataclass(frozen=True)
class EpisodeEmbedding:
    """One episode's conditions as a vector, with what could not be included."""

    episode_id: str
    state: str
    vector: tuple
    dimensions: tuple
    features_missing: tuple
    features_unstandardisable: tuple
    reason: str
    embedded_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == EMBEDDED

    @property
    def is_complete(self) -> bool:
        return not self.features_missing and not self.features_unstandardisable


@dataclass
class EmbedderStanding:
    episodes_seen: int = 0
    embedded: int = 0
    refused_too_few_features: int = 0
    refused_no_features: int = 0
    features_tracked: int = 0
    missing_by_feature: dict = field(default_factory=dict)
    weighted_by_reliability: int = 0


class EpisodeEmbedder:
    """Turns conditions into a standardised, reliability-weighted vector."""

    def __init__(
        self,
        dimensions: tuple,
        half_life_observations: float,
        minimum_observations: int,
        minimum_features: int,
        default_reliability: float,
        now_ns=time.time_ns,
    ) -> None:
        if not dimensions:
            raise ValueError(
                "the dimensions are the closed set of features an embedding may contain; "
                "without them two vectors could describe different things and be compared"
            )
        if minimum_features < 1:
            raise ValueError("a vector from no features indexes nothing")
        self._dimensions = tuple(dimensions)
        self._half_life = half_life_observations
        self._minimum_observations = minimum_observations
        self._minimum_features = minimum_features
        self._default_reliability = default_reliability
        self._now_ns = now_ns
        self._moments: dict[str, RunningMoments] = {}
        self._reliability: dict[str, float] = {}
        self.standing = EmbedderStanding()

    def observe_conditions(self, conditions: dict) -> None:
        """Learn what normal is per feature, so the distance measures the situation."""
        for name in self._dimensions:
            value = conditions.get(name)
            if value is None:
                continue
            self._moment_for(name).observe(value)
        self.standing.features_tracked = len(self._moments)

    def observe_reliability(self, feature: str, reliability: float) -> None:
        """How much this feature has actually predicted anything.

        Unweighted, the noisiest features dominate the distance precisely
        because they vary most.
        """
        if reliability < 0:
            raise ValueError("reliability is a hit rate and cannot be negative")
        self._reliability[feature] = reliability
        self.standing.weighted_by_reliability = len(self._reliability)

    def embed(self, episode_id: str, conditions: dict) -> EpisodeEmbedding:
        """One episode's conditions as a vector. Deterministic, so recall is reproducible."""
        self.standing.episodes_seen += 1

        if not conditions:
            self.standing.refused_no_features += 1
            return self._embedding(
                episode_id, NO_FEATURES, (), (), tuple(self._dimensions), (),
                "the episode carries no conditions, so there is nothing to index it by",
            )

        vector = []
        used = []
        missing = []
        unstandardisable = []

        for name in self._dimensions:
            value = conditions.get(name)
            if value is None:
                # Marked, not zeroed: a zero is a value, and a situation where
                # funding was unavailable would be recorded as one where it was
                # exactly average.
                missing.append(name)
                self.standing.missing_by_feature[name] = (
                    self.standing.missing_by_feature.get(name, 0) + 1
                )
                continue

            moments = self._moments.get(name)
            standardised = (
                None
                if moments is None
                else moments.standardise(value, self._minimum_observations)
            )
            if standardised is None:
                unstandardisable.append(name)
                continue

            weight = self._reliability.get(name, self._default_reliability)
            vector.append(standardised * weight)
            used.append(name)

        if len(vector) < self._minimum_features:
            self.standing.refused_too_few_features += 1
            return self._embedding(
                episode_id, TOO_FEW_FEATURES, tuple(vector), tuple(used), tuple(missing),
                tuple(unstandardisable),
                f"{len(vector)} feature(s) of the {self._minimum_features} needed could be "
                f"standardised. The episodic store falls back to the raw conditions, so this "
                f"episode is still recallable -- an embedder that went down would otherwise "
                f"make the system believe nothing like this had ever happened",
            )

        self.standing.embedded += 1
        return self._embedding(
            episode_id, EMBEDDED, tuple(vector), tuple(used), tuple(missing),
            tuple(unstandardisable),
            f"{len(vector)} of {len(self._dimensions)} dimension(s), each standardised over a "
            f"decayed window and weighted by measured reliability"
            + (
                f"; {len(missing)} feature(s) were missing and are marked rather than zeroed"
                if missing
                else ""
            )
            + (
                f"; {len(unstandardisable)} had too little history to standardise"
                if unstandardisable
                else ""
            )
            + ". The vector is a lossy index into the episodes, not the situation itself",
        )

    def distance(self, left: EpisodeEmbedding, right: EpisodeEmbedding) -> float | None:
        """How far apart two situations are. None when they cannot be compared."""
        if not left.is_usable or not right.is_usable:
            return None
        if left.dimensions != right.dimensions:
            # Two vectors over different features describe different things,
            # and comparing them produces a number that means nothing.
            return None
        return math.sqrt(
            sum((a - b) ** 2 for a, b in zip(left.vector, right.vector))
        )

    def _moment_for(self, name: str) -> RunningMoments:
        moments = self._moments.get(name)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            self._moments[name] = moments
        return moments

    def _embedding(
        self, episode_id, state, vector, dimensions, missing, unstandardisable, reason
    ) -> EpisodeEmbedding:
        return EpisodeEmbedding(
            episode_id=episode_id,
            state=state,
            vector=vector,
            dimensions=dimensions,
            features_missing=missing,
            features_unstandardisable=unstandardisable,
            reason=reason,
            embedded_at_ns=self._now_ns(),
        )


def describe_embedding(embedder: EpisodeEmbedder) -> dict:
    return {
        "part_id": PART_ID,
        "dimensions": list(embedder._dimensions),
        "episodes_seen": embedder.standing.episodes_seen,
        "embedded": embedder.standing.embedded,
        "refused_too_few_features": embedder.standing.refused_too_few_features,
        "refused_no_features": embedder.standing.refused_no_features,
        "features_tracked": embedder.standing.features_tracked,
        "features_weighted_by_reliability": embedder.standing.weighted_by_reliability,
        "missing_by_feature": dict(sorted(embedder.standing.missing_by_feature.items())),
        "is_deterministic": True,
    }


def run_episode_embedder(
    embedder: EpisodeEmbedder, control_socket, read_episodes, publish_embeddings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        episodes = read_episodes(embedder)
        publish_embeddings(
            tuple(embedder.embed(episode_id, conditions) for episode_id, conditions in episodes)
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
