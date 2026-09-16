"""The model `knowledge-embedder` actually loads, against real captured text.

Until 2026-09-16 no embedding model was installed on this box, and the cost was
the whole block: `retrieval-index` served 5,467 queries against an empty index,
`context-assembler` published no context, `prompt-renderer` refused 84,228
requests for missing context, and none of the 26 LLM parts had ever called a
model. The setting `embedding_model_id` now names one.

Real captured Upstox news headlines (RL-063), because what the index has to
separate is stories about different companies, and invented sentences are
separable in ways real headlines are not.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from parts.llm_foundation.knowledge_embedder import EMBEDDED, NO_MODEL, KnowledgeEmbedder
from runtime.tape import read_payload, read_tape_index

NEWS = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/news/upstox-news-api"
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSIONS = 384


def captured_headlines(count: int) -> list[str]:
    headlines: list[str] = []
    for index_path in sorted(NEWS.glob("*.news.index"))[-1:]:
        blob_path = index_path.parent / index_path.name.replace(".index", ".blob")
        for record in read_tape_index(index_path):
            try:
                story = json.loads(read_payload(blob_path, record))
            except Exception:
                continue
            title = (story.get("title") or "").strip()
            if len(title) > 40 and title not in headlines:
                headlines.append(title)
            if len(headlines) >= count:
                return headlines
    return headlines


@pytest.fixture(scope="module")
def embed_one():
    from runtime.text_embedding import load_sentence_embedder

    try:
        return load_sentence_embedder(MODEL_ID, DIMENSIONS, threads=1)
    except Exception as failure:  # no weights cached and no network
        pytest.skip(f"{MODEL_ID} could not be loaded here: {type(failure).__name__}: {failure}")


def an_embedder(embed=None):
    subject = KnowledgeEmbedder(
        chunk_characters=512, overlap_characters=64, minimum_characters=10,
        dimensions=DIMENSIONS,
    )
    if embed is not None:
        subject.install_model(MODEL_ID, embed)
    return subject


def test_a_real_headline_embeds_to_the_declared_width(embed_one):
    headlines = captured_headlines(1)
    if not headlines:
        pytest.skip("no captured news on this machine to embed")
    outcome = an_embedder(embed_one).embed_text("source-document", "news://1", headlines[0])
    assert outcome.state == EMBEDDED
    assert outcome.embeddings
    vector = outcome.embeddings[0].vector
    assert len(vector) == DIMENSIONS
    # Length-normalised, which is what makes a cosine index comparable.
    assert abs(sum(value * value for value in vector) ** 0.5 - 1.0) < 1e-5
    assert outcome.embeddings[0].model_id == MODEL_ID


def test_two_stories_about_one_company_sit_closer_than_two_unrelated_ones(embed_one):
    """The property the index is for. A model that returned a constant vector
    would pass every assertion above and be useless here."""
    subject = an_embedder(embed_one)

    def vector_of(text):
        outcome = subject.embed_text("source-document", f"news://{hash(text)}", text)
        assert outcome.state == EMBEDDED
        return outcome.embeddings[0].vector

    def cosine(left, right):
        return sum(a * b for a, b in zip(left, right))

    reliance_one = vector_of(
        "Reliance Industries shares rise as refining margins widen on cheaper crude"
    )
    reliance_two = vector_of(
        "Reliance Industries gains after the refining business posts a stronger quarter"
    )
    unrelated = vector_of(
        "Nestle India declares an interim dividend as packaged foods volumes recover"
    )

    assert cosine(reliance_one, reliance_two) > cosine(reliance_one, unrelated)


def test_without_a_model_nothing_is_published_and_it_says_so():
    """The degraded state this box was in until 2026-09-16, kept reachable: a
    zero vector would sit in the index looking like a measurement."""
    outcome = an_embedder().embed_text("source-document", "news://1", "a" * 200)
    assert outcome.state == NO_MODEL
    assert outcome.embeddings == ()
    assert not outcome.is_usable


def test_a_query_retrieves_the_document_it_is_about(embed_one):
    """The end the whole block exists for: text in, the right passage out.

    `retrieval-index` holds document vectors and a `retrieval-query` is text, so
    the query is embedded with the same model the documents were. Before
    2026-09-16 there was no model at all and every query was answered
    `the-query-was-never-embedded` -- 5,467 of them against an empty index.
    """
    from parts.llm_foundation.knowledge_embedder import KnowledgeEmbedder
    from parts.llm_foundation.retrieval_index import RetrievalIndex

    embedder = KnowledgeEmbedder(
        chunk_characters=512, overlap_characters=64, minimum_characters=10,
        dimensions=DIMENSIONS,
    )
    embedder.install_model(MODEL_ID, embed_one)
    index = RetrievalIndex(
        duplicate_similarity=0.99, usefulness_weight=0.0, prior_usefulness=0.5,
    )

    passages = {
        "doc://margin": "A stop must be snapped to the exchange's freeze quantity before "
                        "it is sent, because an order larger than the venue accepts is "
                        "rejected or walked through the book.",
        "doc://theta": "An option loses time value every day it is held, and the loss "
                       "accelerates as expiry approaches, which is what theta measures.",
        "doc://monsoon": "The monsoon's arrival over Kerala sets the sowing calendar for "
                         "the kharif crop across most of India.",
    }
    for reference, text in passages.items():
        outcome = embedder.embed_text("source-document", reference, text)
        assert outcome.state == EMBEDDED
        for embedding in outcome.embeddings:
            index.observe_embedding(embedding)

    query = SimpleNamespaceQuery(
        query_id="q1",
        text="how fast does an option lose its time value before expiry",
        # The kinds asked for: a query states them and the index holds nothing
        # else, which is the filter that makes a retrieval answerable.
        wanted_kinds=("source-document",),
    )
    result = index.retrieve(query, embed_one(query.text))

    assert result.hits, f"no hits: {result.state}"
    assert result.hits[0].source_reference.startswith("doc://theta")
    assert index.active_model == MODEL_ID


class SimpleNamespaceQuery:
    """The two fields `retrieve` reads off a `RetrievalQuery`."""

    def __init__(self, query_id, text, wanted_kinds=(), maximum_hits=3,
                 minimum_similarity=0.0):
        self.query_id = query_id
        self.text = text
        self.wanted_kinds = wanted_kinds
        self.maximum_hits = maximum_hits
        self.minimum_similarity = minimum_similarity
