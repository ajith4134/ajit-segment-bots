"""episodic-trade-store: what actually happened, appended and never revised.

The second memory tier. Its whole value is that it is **not** the current belief:
a system that revised its episodes to match what it now thinks would lose the only
record of having been wrong, which is the record every correction is made from.

- **Append only.** No episode is ever edited or deleted. Not as a policy but as
  the design: there is no method to do it, so no future part can decide that one
  inconvenient episode was mistaken.
- **Keyed on the conditions observed, not on the outcome.** Recall answers "what
  happened last time it looked like this", which is the question a decision asks.
  Keying on outcome answers "what worked", which is the question that produces
  survivorship bias.
- **Recall returns the losses too.** A store that surfaced only profitable
  precedents would make every situation look like an opportunity, and the
  situations where the system lost are the ones it most needs the precedent for.
- **Recall is bounded and says what it left out.** An unbounded recall is a
  latency problem; a truncated one that does not say so is a bias nobody sees.

**Similarity is measured on the conditions, using the embedding when there is one
and the raw conditions when there is not.** A store that could only recall with
an embedding would go silent whenever the embedder was off, and silence reads as
"nothing like this has happened".
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.knowledge_types import TradeEpisode
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "episodic-trade-store"

PART_DECLARATION = PartDeclaration(
    part_id="episodic-trade-store",
    consumes=("trade-episode", "episode-embedding"),
    produces=("recalled-episode", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECALLED = "recalled"
NOTHING_SIMILAR = "nothing-like-this-has-happened-here"
TRUNCATED = "more-matched-than-were-returned"


@dataclass(frozen=True)
class RecalledEpisode:
    """One past episode, with how alike it is and how it turned out."""

    episode: TradeEpisode
    similarity: float
    matched_on: str
    reason: str


@dataclass(frozen=True)
class Recall:
    """What the store found, and what it left out."""

    venue_id: str
    symbol: str
    state: str
    episodes: tuple
    profitable_returned: int
    losing_returned: int
    matched_total: int
    returned: int
    reason: str
    recalled_at_ns: int

    @property
    def found_anything(self) -> bool:
        return self.state == RECALLED

    @property
    def was_truncated(self) -> bool:
        return self.matched_total > self.returned


@dataclass
class StoreStanding:
    episodes_appended: int = 0
    recalls: int = 0
    empty_recalls: int = 0
    truncated_recalls: int = 0
    embedded_episodes: int = 0
    matched_on_embedding: int = 0
    matched_on_conditions: int = 0
    losses_returned: int = 0
    by_symbol: dict = field(default_factory=dict)


class EpisodicTradeStore:
    """Appends episodes immutably and recalls by what the conditions looked like."""

    def __init__(
        self,
        maximum_returned: int,
        minimum_similarity: float,
        maximum_held: int,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_returned < 1:
            raise ValueError("a recall that returns nothing cannot inform a decision")
        if not 0.0 < minimum_similarity <= 1.0:
            raise ValueError(
                "without a similarity floor every episode matches, and recall stops meaning "
                "'what happened last time it looked like this'"
            )
        self._maximum_returned = maximum_returned
        self._minimum_similarity = minimum_similarity
        self._maximum_held = maximum_held
        self._now_ns = now_ns
        self._episodes: list = []
        self._embeddings: dict[str, tuple] = {}
        self.standing = StoreStanding()

    def append(self, episode: TradeEpisode) -> None:
        """One episode. There is no method to edit or delete it, by design."""
        self._episodes.append(episode)
        del self._episodes[: max(0, len(self._episodes) - self._maximum_held)]
        self.standing.episodes_appended += 1
        self.standing.by_symbol[episode.symbol] = (
            self.standing.by_symbol.get(episode.symbol, 0) + 1
        )

    def observe_embedding(self, episode_id: str, embedding) -> None:
        self._embeddings[episode_id] = tuple(embedding)
        self.standing.embedded_episodes = len(self._embeddings)

    def similarity(self, conditions: dict, episode: TradeEpisode, embedding=None) -> tuple:
        """How alike two situations are, on the conditions rather than the outcome.

        Keying on outcome answers "what worked", which is the question that
        produces survivorship bias.
        """
        if embedding is not None and episode.episode_id in self._embeddings:
            stored = self._embeddings[episode.episode_id]
            if len(stored) == len(embedding):
                return self._cosine(embedding, stored), "embedding"

        # Falls back to the raw conditions rather than going silent: silence
        # reads as "nothing like this has happened".
        shared = set(conditions) & set(episode.conditions)
        if not shared:
            return 0.0, "conditions"
        agreements = 0
        for name in shared:
            left, right = conditions[name], episode.conditions[name]
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                larger = max(abs(left), abs(right), 1e-12)
                agreements += 1.0 - min(1.0, abs(left - right) / larger)
            else:
                agreements += 1.0 if left == right else 0.0
        return agreements / len(set(conditions) | set(episode.conditions)), "conditions"

    def recall(self, venue_id: str, symbol: str, conditions: dict, embedding=None) -> Recall:
        """What happened last time it looked like this -- wins and losses alike."""
        self.standing.recalls += 1

        matched = []
        for episode in self._episodes:
            if (episode.venue_id, episode.symbol) != (venue_id, symbol):
                continue
            score, matched_on = self.similarity(conditions, episode, embedding)
            if score >= self._minimum_similarity:
                matched.append((score, matched_on, episode))

        if not matched:
            self.standing.empty_recalls += 1
            return Recall(
                venue_id=venue_id, symbol=symbol, state=NOTHING_SIMILAR, episodes=(),
                profitable_returned=0, losing_returned=0, matched_total=0, returned=0,
                reason=(
                    f"no episode in {symbol} is at least {self._minimum_similarity:.0%} alike. "
                    f"That is a real answer: nothing like this has happened here"
                ),
                recalled_at_ns=self._now_ns(),
            )

        matched.sort(key=lambda entry: -entry[0])
        returned = matched[: self._maximum_returned]

        episodes = tuple(
            RecalledEpisode(
                episode=episode,
                similarity=score,
                matched_on=matched_on,
                reason=(
                    f"{score:.0%} alike on {matched_on}; it "
                    + ("made " if episode.was_profitable else "lost ")
                    + f"{abs(episode.realised):.2%} over {episode.seconds_held:.0f}s"
                ),
            )
            for score, matched_on, episode in returned
        )
        for _, matched_on, _ in returned:
            if matched_on == "embedding":
                self.standing.matched_on_embedding += 1
            else:
                self.standing.matched_on_conditions += 1

        profitable = sum(1 for entry in episodes if entry.episode.was_profitable)
        losing = len(episodes) - profitable
        self.standing.losses_returned += losing

        truncated = len(matched) > len(returned)
        if truncated:
            self.standing.truncated_recalls += 1

        return Recall(
            venue_id=venue_id,
            symbol=symbol,
            state=RECALLED,
            episodes=episodes,
            profitable_returned=profitable,
            losing_returned=losing,
            matched_total=len(matched),
            returned=len(returned),
            reason=(
                f"{len(returned)} episode(s) at least {self._minimum_similarity:.0%} alike: "
                f"{profitable} that made money and {losing} that did not. Both, because a "
                f"store surfacing only profitable precedents makes every situation look like "
                f"an opportunity"
                + (
                    f". {len(matched) - len(returned)} more matched and were not returned -- "
                    f"said out loud, because a truncated recall that stays quiet is a bias "
                    f"nobody sees"
                    if truncated
                    else ""
                )
            ),
            recalled_at_ns=self._now_ns(),
        )

    def _cosine(self, left, right) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return max(0.0, min(1.0, dot / (left_norm * right_norm)))

    @property
    def episodes_held(self) -> int:
        return len(self._episodes)


def describe_episodic_store(store: EpisodicTradeStore) -> dict:
    return {
        "part_id": PART_ID,
        "episodes_appended": store.standing.episodes_appended,
        "episodes_held": store.episodes_held,
        "recalls": store.standing.recalls,
        "empty_recalls": store.standing.empty_recalls,
        "truncated_recalls": store.standing.truncated_recalls,
        "embedded_episodes": store.standing.embedded_episodes,
        "matched_on_embedding": store.standing.matched_on_embedding,
        "matched_on_conditions": store.standing.matched_on_conditions,
        "losing_episodes_returned": store.standing.losses_returned,
        "by_symbol": dict(sorted(store.standing.by_symbol.items())),
        "episodes_can_be_edited": False,
    }


def run_episodic_trade_store(
    store: EpisodicTradeStore, control_socket, read_episodes, publish_recalls,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        queries = read_episodes(store)
        publish_recalls(
            tuple(
                store.recall(venue_id, symbol, conditions, embedding)
                for venue_id, symbol, conditions, embedding in queries
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
