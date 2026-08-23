"""retrieval-index: what actually comes back, ranked by more than similarity.

Cosine similarity answers one question -- which stored text is worded most like the
query. That is not the same question as which stored text would help, and the gap
between them is where retrieval-augmented systems quietly fail. Three corrections
are applied here, all of them measured rather than assumed:

- **Similarity is a floor, not a ranking.** Everything below the query's floor is
  dropped outright. Above it, ranking blends similarity with what a source has
  actually been worth in past answers (the retrieval scorer's verdict), so a
  passage that keeps appearing in answers that turned out wrong stops winning on
  wording alone.
- **Near-duplicates are collapsed.** The same rule stated in three ingested
  documents fills a context window three times and crowds out everything else. One
  survives and the others are counted as suppressed, which is also the honest
  answer to "how corroborated is this" -- not three times.
- **Models are never mixed.** Vectors from a superseded embedding model are
  excluded rather than compared, because comparing them returns confident nonsense
  instead of failing.

An empty result is a first-class answer. A retrieval that finds nothing above the
floor says so, and the assembler is built to run without it -- the alternative is
returning the least-unrelated passages in the corpus and letting them be read as
evidence.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.llm_types import RetrievalHit
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "retrieval-index"

PART_DECLARATION = PartDeclaration(
    part_id="retrieval-index",
    consumes=("embedding", "retrieval-query", "retrieval-score"),
    produces=("retrieval-hit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RETRIEVED = "retrieved"
NOTHING_ABOVE_THE_FLOOR = "nothing-stored-is-close-enough-to-the-query"
EMPTY_INDEX = "nothing-is-indexed-for-the-kinds-asked-for"
NO_QUERY_VECTOR = "the-query-was-never-embedded"


@dataclass(frozen=True)
class RetrievalResult:
    query_id: str
    state: str
    hits: tuple
    considered: int
    below_floor: int
    duplicates_suppressed: int
    excluded_wrong_model: int
    reason: str
    retrieved_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == RETRIEVED and bool(self.hits)


@dataclass
class IndexStanding:
    embeddings_indexed: int = 0
    queries_served: int = 0
    hits_returned: int = 0
    empty_results: int = 0
    duplicates_suppressed: int = 0
    excluded_wrong_model: int = 0
    reranked_below_a_similarity_winner: int = 0


class RetrievalIndex:
    """Holds vectors and answers queries, ranking on similarity and past usefulness."""

    def __init__(
        self,
        duplicate_similarity: float,
        usefulness_weight: float,
        prior_usefulness: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < duplicate_similarity <= 1.0:
            raise ValueError("the duplicate threshold is a similarity inside (0, 1]")
        if not 0.0 <= usefulness_weight <= 1.0:
            raise ValueError(
                "the weight blends measured usefulness with wording similarity and is a "
                "fraction"
            )
        self._duplicate_similarity = duplicate_similarity
        self._usefulness_weight = usefulness_weight
        self._prior_usefulness = prior_usefulness
        self._now_ns = now_ns
        self._embeddings: list = []
        self._usefulness: dict[str, float] = {}
        self._active_model: str | None = None
        self._sequence = 0
        self.standing = IndexStanding()

    def observe_embedding(self, embedding) -> None:
        """The newest model seen becomes the active one; older vectors are excluded."""
        if self._active_model is None:
            self._active_model = embedding.model_id
        elif embedding.model_id != self._active_model:
            self._active_model = embedding.model_id
        self._embeddings.append(embedding)
        self.standing.embeddings_indexed += 1

    def observe_score(self, score) -> None:
        """What a source has been worth in answers that were graded afterwards."""
        if score.is_fitted:
            self._usefulness[score.source_reference] = score.usefulness

    def usefulness_of(self, source_reference: str) -> float:
        root = source_reference.split("#")[0]
        if source_reference in self._usefulness:
            return self._usefulness[source_reference]
        return self._usefulness.get(root, self._prior_usefulness)

    @staticmethod
    def similarity(left, right) -> float:
        if len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return dot / (left_norm * right_norm)

    def retrieve(self, query, query_vector) -> RetrievalResult:
        self.standing.queries_served += 1
        if not query_vector:
            return self._result(
                query.query_id, NO_QUERY_VECTOR, (), 0, 0, 0, 0,
                "the query was never embedded, so there is nothing to compare against",
            )

        candidates = []
        below_floor = 0
        excluded = 0
        for embedding in self._embeddings:
            if embedding.source_kind not in query.wanted_kinds:
                continue
            if self._active_model is not None and embedding.model_id != self._active_model:
                excluded += 1
                continue
            score = self.similarity(query_vector, embedding.vector)
            if score < query.minimum_similarity:
                below_floor += 1
                continue
            candidates.append((score, embedding))

        self.standing.excluded_wrong_model += excluded

        if not candidates and not below_floor and not excluded:
            self.standing.empty_results += 1
            return self._result(
                query.query_id, EMPTY_INDEX, (), 0, 0, 0, excluded,
                f"nothing is indexed for {', '.join(query.wanted_kinds)}",
            )

        if not candidates:
            self.standing.empty_results += 1
            return self._result(
                query.query_id, NOTHING_ABOVE_THE_FLOOR, (), below_floor, below_floor, 0,
                excluded,
                f"{below_floor} passage(s) considered, none above the "
                f"{query.minimum_similarity:.2f} floor. Returning the least-unrelated "
                f"passages instead would put them into a prompt as evidence",
            )

        # Rank on both: wording similarity blended with what the source has been worth.
        ranked = sorted(
            candidates,
            key=lambda pair: -(
                (1.0 - self._usefulness_weight) * pair[0]
                + self._usefulness_weight * self.usefulness_of(pair[1].source_reference)
            ),
        )
        if ranked and ranked[0][0] < max(score for score, _ in candidates):
            self.standing.reranked_below_a_similarity_winner += 1

        kept = []
        suppressed = 0
        for score, embedding in ranked:
            if any(
                self.similarity(embedding.vector, chosen.vector)
                >= self._duplicate_similarity
                for _, chosen in kept
            ):
                suppressed += 1
                continue
            kept.append((score, embedding))
            if len(kept) >= query.maximum_hits:
                break

        self.standing.duplicates_suppressed += suppressed
        hits = []
        for score, embedding in kept:
            self._sequence += 1
            hits.append(
                RetrievalHit(
                    hit_id=f"h-{self._sequence}",
                    query_id=query.query_id,
                    source_kind=embedding.source_kind,
                    source_reference=embedding.source_reference,
                    text=embedding.text,
                    similarity=score,
                    characters=len(embedding.text),
                    embedded_at_ns=embedding.embedded_at_ns,
                    retrieved_at_ns=self._now_ns(),
                )
            )
        self.standing.hits_returned += len(hits)

        return self._result(
            query.query_id, RETRIEVED, tuple(hits), len(candidates) + below_floor,
            below_floor, suppressed, excluded,
            f"{len(hits)} hit(s) from {len(candidates)} above the floor"
            + (
                f", {suppressed} near-duplicate(s) suppressed -- the same rule in three "
                f"documents is not three corroborations"
                if suppressed
                else ""
            )
            + (
                f", {excluded} vector(s) from a superseded embedding model excluded"
                if excluded
                else ""
            ),
        )

    def _result(
        self, query_id, state, hits, considered, below_floor, duplicates, excluded, reason,
    ) -> RetrievalResult:
        return RetrievalResult(
            query_id=query_id, state=state, hits=hits, considered=considered,
            below_floor=below_floor, duplicates_suppressed=duplicates,
            excluded_wrong_model=excluded, reason=reason, retrieved_at_ns=self._now_ns(),
        )


def describe_index(index: RetrievalIndex) -> dict:
    return {
        "part_id": PART_ID,
        "embeddings_indexed": index.standing.embeddings_indexed,
        "queries_served": index.standing.queries_served,
        "hits_returned": index.standing.hits_returned,
        "empty_results": index.standing.empty_results,
        "duplicates_suppressed": index.standing.duplicates_suppressed,
        "excluded_from_a_superseded_model": index.standing.excluded_wrong_model,
        "times_measured_usefulness_beat_similarity": (
            index.standing.reranked_below_a_similarity_winner
        ),
        "returns_the_least_unrelated_when_nothing_qualifies": False,
        "compares_vectors_across_models": False,
    }


def run_retrieval_index(
    index: RetrievalIndex, control_socket, read_embeddings, read_queries,
    publish_hits, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for embedding in read_embeddings():
            index.observe_embedding(embedding)
        for query, query_vector in read_queries():
            result = index.retrieve(query, query_vector)
            if result.is_usable:
                publish_hits(result.hits)

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

    Embeddings are indexed as they arrive and scores adjust a source's
    rank. A query needs a vector, and nothing this part consumes carries
    one for the query's text -- the embedder is on the document side -- so
    every query is answered NO_QUERY_VECTOR by name until the query itself
    arrives embedded.
    """
    from runtime.input_assembly import Batch

    embeddings = Batch(read=context.bus.reader("embedding"))
    queries = Batch(read=context.bus.reader("retrieval-query"))
    scores = Batch(read=context.bus.reader("retrieval-score"))
    publish_hits = context.bus.publisher_for("retrieval-hit")
    index = RetrievalIndex(
        duplicate_similarity=context.number("retrieval_duplicate_similarity"),
        usefulness_weight=context.number("retrieval_usefulness_weight"),
        prior_usefulness=context.number("learning_prior_hit_rate"),
    )

    def read_embeddings():
        for score in scores.payloads():
            index.observe_score(score)
        return embeddings.payloads()

    return run_retrieval_index(
        index=index,
        control_socket=context.control_socket,
        read_embeddings=read_embeddings,
        read_queries=lambda: tuple((query, None) for query in queries.payloads()),
        publish_hits=lambda hits: publish_hits(tuple(hits)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
