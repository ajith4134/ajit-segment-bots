"""knowledge-embedder: text turned into vectors, with the model stamped on each one.

Embeddings are the one place in this system where two numbers can look identical and
mean nothing to each other. A vector from one model and a vector from another are
not comparable, and a mixed index does not fail -- it returns confident nonsense.
So every vector here carries the model that produced it, and the index refuses to
compare across models rather than trusting the caller to remember.

What is embedded matters as much as how. Three decisions:

- **Long documents are chunked, and the chunk keeps its position.** A 300-page book
  embedded as one vector is a vector of nothing in particular; retrieved without its
  position, a passage cannot be checked against its source.
- **Chunks overlap.** A rule that spans a chunk boundary is otherwise split into two
  halves, neither of which retrieves. The overlap is a setting, not a constant.
- **The same text is embedded once.** Re-embedding is the largest avoidable cost in
  this block, and duplicate vectors also skew retrieval by making a repeated passage
  win on count.

Re-embedding on a model change is a real requirement rather than an optimisation:
when the model changes, every old vector becomes incomparable, so the embedder
reports how many vectors are stale rather than mixing them into the same index.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.llm_types import Embedding
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "knowledge-embedder"

PART_DECLARATION = PartDeclaration(
    part_id="knowledge-embedder",
    consumes=("source-document", "journal-entry", "skill"),
    produces=("embedding", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

EMBEDDED = "embedded"
ALREADY_EMBEDDED = "this-text-is-already-embedded-by-this-model"
TOO_SHORT = "too-short-to-carry-meaning"
NO_MODEL = "no-embedding-model-is-installed"
EMBED_FAILED = "the-model-failed"
WRONG_DIMENSIONS = "the-model-returned-a-vector-of-the-wrong-length"


@dataclass(frozen=True)
class EmbeddingOutcome:
    source_reference: str
    state: str
    embeddings: tuple
    chunks: int
    reused: int
    reason: str
    embedded_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == EMBEDDED and bool(self.embeddings)


@dataclass
class EmbedderStanding:
    documents_seen: int = 0
    chunks_embedded: int = 0
    chunks_reused: int = 0
    too_short: int = 0
    failures: int = 0
    wrong_dimensions: int = 0
    vectors_made_stale_by_a_model_change: int = 0
    model_changes: int = 0


class KnowledgeEmbedder:
    """Chunks text, embeds each chunk once, and stamps the model on every vector."""

    def __init__(
        self,
        chunk_characters: int,
        overlap_characters: int,
        minimum_characters: int,
        dimensions: int,
        now_ns=time.time_ns,
    ) -> None:
        if chunk_characters < 1:
            raise ValueError("a chunk of zero characters embeds nothing")
        if not 0 <= overlap_characters < chunk_characters:
            raise ValueError(
                "the overlap must be smaller than the chunk, and it must exist: a rule "
                "spanning a boundary is otherwise split into two halves that never retrieve"
            )
        if minimum_characters < 1:
            raise ValueError("a floor of zero embeds whitespace")
        if dimensions < 1:
            raise ValueError("a vector needs a length")
        self._chunk_characters = chunk_characters
        self._overlap_characters = overlap_characters
        self._minimum_characters = minimum_characters
        self._dimensions = dimensions
        self._now_ns = now_ns
        self._model_id: str | None = None
        self._embed = None
        # (model_id, text digest) -> Embedding, so the same text is embedded once.
        self._by_text: dict[tuple, Embedding] = {}
        self.standing = EmbedderStanding()

    def install_model(self, model_id: str, embed) -> None:
        """Changing the model makes every existing vector incomparable, not merely old."""
        if self._model_id is not None and model_id != self._model_id:
            self.standing.model_changes += 1
            self.standing.vectors_made_stale_by_a_model_change += len(self._by_text)
        self._model_id = model_id
        self._embed = embed

    def stale_vector_count(self) -> int:
        """Vectors from a superseded model. They must be rebuilt, never mixed in."""
        return sum(
            1
            for (model_id, _), _ in self._by_text.items()
            if model_id != self._model_id
        )

    def chunks_of(self, text: str) -> tuple:
        """Overlapping windows, each keeping the offset it came from."""
        chunks = []
        step = self._chunk_characters - self._overlap_characters
        position = 0
        while position < len(text):
            piece = text[position : position + self._chunk_characters]
            if len(piece.strip()) >= self._minimum_characters:
                chunks.append((position, piece))
            if position + self._chunk_characters >= len(text):
                break
            position += step
        return tuple(chunks)

    def embed_text(
        self, source_kind: str, source_reference: str, text: str,
    ) -> EmbeddingOutcome:
        self.standing.documents_seen += 1
        if self._embed is None or self._model_id is None:
            return self._outcome(
                source_reference, NO_MODEL, (), 0, 0,
                "no embedding model is installed. Which model produced a vector is part "
                "of the vector, so there is no default",
            )

        if len(text.strip()) < self._minimum_characters:
            self.standing.too_short += 1
            return self._outcome(
                source_reference, TOO_SHORT, (), 0, 0,
                f"{len(text.strip())} character(s), below the {self._minimum_characters} "
                f"floor",
            )

        chunks = self.chunks_of(text)
        embeddings = []
        reused = 0
        for offset, piece in chunks:
            digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            key = (self._model_id, digest)
            existing = self._by_text.get(key)
            if existing is not None:
                reused += 1
                self.standing.chunks_reused += 1
                embeddings.append(existing)
                continue

            try:
                vector = tuple(float(value) for value in self._embed(piece))
            except Exception as failure:
                self.standing.failures += 1
                return self._outcome(
                    source_reference, EMBED_FAILED, tuple(embeddings), len(chunks), reused,
                    f"the model failed on chunk at offset {offset} "
                    f"({type(failure).__name__}). Partial vectors are returned rather than "
                    f"discarded: what was embedded is still usable, and what was not is "
                    f"visibly absent",
                )

            if len(vector) != self._dimensions:
                self.standing.wrong_dimensions += 1
                return self._outcome(
                    source_reference, WRONG_DIMENSIONS, tuple(embeddings), len(chunks),
                    reused,
                    f"the model returned {len(vector)} dimension(s) where "
                    f"{self._dimensions} was declared. A vector of the wrong length in a "
                    f"shared index is a silent corruption",
                )

            embedding = Embedding(
                embedding_id=f"{self._model_id}:{digest[:16]}",
                source_kind=source_kind,
                source_reference=f"{source_reference}#{offset}",
                text=piece,
                vector=vector,
                model_id=self._model_id,
                dimensions=self._dimensions,
                embedded_at_ns=self._now_ns(),
            )
            self._by_text[key] = embedding
            embeddings.append(embedding)
            self.standing.chunks_embedded += 1

        if not embeddings:
            self.standing.too_short += 1
            return self._outcome(
                source_reference, TOO_SHORT, (), len(chunks), reused,
                "nothing in the text was long enough to embed",
            )

        return self._outcome(
            source_reference, EMBEDDED, tuple(embeddings), len(chunks), reused,
            f"{len(embeddings)} chunk(s) at {self._chunk_characters} characters with "
            f"{self._overlap_characters} overlap, {reused} reused. Every vector carries "
            f"{self._model_id}: vectors from two models are not comparable",
        )

    def _outcome(
        self, source_reference, state, embeddings, chunks, reused, reason,
    ) -> EmbeddingOutcome:
        return EmbeddingOutcome(
            source_reference=source_reference, state=state, embeddings=embeddings,
            chunks=chunks, reused=reused, reason=reason, embedded_at_ns=self._now_ns(),
        )


def describe_embedding(embedder: KnowledgeEmbedder) -> dict:
    return {
        "part_id": PART_ID,
        "documents_seen": embedder.standing.documents_seen,
        "chunks_embedded": embedder.standing.chunks_embedded,
        "chunks_reused": embedder.standing.chunks_reused,
        "too_short": embedder.standing.too_short,
        "failures": embedder.standing.failures,
        "wrong_dimension_responses": embedder.standing.wrong_dimensions,
        "model_changes": embedder.standing.model_changes,
        "stale_vectors": embedder.stale_vector_count(),
        "mixes_models_in_one_index": False,
    }


def run_knowledge_embedder(
    embedder: KnowledgeEmbedder, control_socket, read_texts, publish_embeddings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for source_kind, source_reference, text in read_texts():
            outcome = embedder.embed_text(source_kind, source_reference, text)
            if outcome.embeddings:
                publish_embeddings(outcome.embeddings)

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

    No embedding model is installed on this box, so every text is answered
    NO_MODEL by name and no vector is published. Documents, journal entries
    with text in their payload, and skills are the texts offered;
    `install_model` is the one way a model gets in.
    """
    from runtime.input_assembly import Batch

    documents = Batch(read=context.bus.reader("source-document"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    skills = Batch(read=context.bus.reader("skill"))
    publish_embeddings = context.bus.publisher_for("embedding")
    embedder = KnowledgeEmbedder(
        chunk_characters=int(context.number("embedding_chunk_characters")),
        overlap_characters=int(context.number("embedding_overlap_characters")),
        minimum_characters=int(context.number("embedding_minimum_characters")),
        dimensions=int(context.number("embedding_vector_dimensions")),
    )

    def read_texts():
        texts = []
        for document in documents.payloads():
            texts.append(("source-document", document.source_reference, f"{document.title}\n{document.content}"))
        for entry in entries.payloads():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            text = payload.get("narrative") or payload.get("text")
            if text:
                texts.append(("journal-entry", f"journal:{getattr(entry, 'sequence', getattr(entry, 'entry_id', ''))}", str(text)))
        for skill in skills.payloads():
            body = "\n".join(str(section) for section in skill.sections)
            texts.append(("skill", skill.source_reference or skill.skill_id, f"{skill.title}\n{body}"))
        return tuple(texts)

    return run_knowledge_embedder(
        embedder=embedder,
        control_socket=context.control_socket,
        read_texts=read_texts,
        publish_embeddings=lambda embeddings: publish_embeddings(tuple(embeddings)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
