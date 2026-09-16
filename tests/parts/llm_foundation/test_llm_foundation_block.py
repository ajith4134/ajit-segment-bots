"""The llm-foundation block: everything that stands between a model and a decision.

A language model is the least trustworthy component here -- non-deterministic,
charged for, changing under its own version number, and fluent enough that a wrong
answer reads like a right one. These tests are about the machinery that makes each
of those visible: versioned prompts, facts given rather than recalled, structure
enforced, quality measured against outcomes rather than opinions, and every call
attributed to the part that made it.
"""

import importlib
import json

import pytest

from parts.llm_foundation.context_assembler import (
    ASSEMBLED, ContextAssembler, FACTS_DO_NOT_FIT, NO_BUDGET, NO_FACTS,
    SNAPSHOT_TOO_STALE,
)
from parts.llm_foundation.decision_cost_accountant import (
    DecisionCostAccountant, NOT_CLOSED, NO_TRADE, SETTLED,
)
from parts.llm_foundation.golden_case_keeper import (
    CONTAINS_THE_FUTURE, EXPECTATION_WAS_WRONG, GoldenCaseKeeper, KEPT,
    OUTCOME_NOT_KNOWN, RETIRED, WORLD_HAS_CHANGED,
)
from parts.llm_foundation.knowledge_embedder import (
    EMBEDDED, KnowledgeEmbedder, NO_MODEL, TOO_SHORT as EMBED_TOO_SHORT,
    WRONG_DIMENSIONS,
)
from parts.llm_foundation.part_token_budgeter import (
    EXHAUSTED, ISSUED, NOTHING_LEFT_TO_ALLOCATE, PartTokenBudgeter,
)
from parts.llm_foundation.prompt_drift_monitor import (
    DEGRADED, IMPROVED, MODEL_CHANGED, PromptDriftMonitor, WATCHED_DIMENSIONS,
)
from parts.llm_foundation.prompt_evaluator import (
    CaseResult, INCOMPLETE_RUN, NOT_THE_SAME_CASES, NO_CASES, PromptEvaluator,
    SCORED, TOO_FEW_CASES,
)
from parts.llm_foundation.prompt_promotion_gate import (
    A_DIMENSION_REGRESSED, BELOW_THE_ABSOLUTE_BAR, COSTS_TOO_MUCH_MORE,
    INSIDE_THE_MARGIN, PROMOTED, PromptPromotionGate, QUALITY_DIMENSIONS,
)
from parts.llm_foundation.prompt_registry import (
    ALREADY_ACTIVE, IDENTICAL_TO_AN_EXISTING_VERSION, NO_SUCH_TEMPLATE,
    PROMOTED as REGISTRY_PROMOTED, PromptRegistry, REGISTERED,
)
from parts.llm_foundation.prompt_renderer import (
    FACTS_HEADING, MISSING_CONTEXT, NO_ACTIVE_VERSION, NO_FACTS as RENDER_NO_FACTS,
    NO_OUTPUT_SCHEMA, PromptRenderer, RENDERED,
)
from parts.llm_foundation.prompt_template_author import (
    ASKS_FOR_A_TRADING_DECISION, ASKS_THE_MODEL_TO_RECALL, NO_CONTEXT_DECLARED,
    NO_EVIDENCE_TO_WRITE_FROM, NO_OUTPUT_SHAPE, PromptTemplateAuthor, UNCHANGED,
    WRITTEN,
)
from parts.llm_foundation.retrieval_index import (
    EMPTY_INDEX, NOTHING_ABOVE_THE_FLOOR, RETRIEVED, RetrievalIndex,
)
from parts.llm_foundation.retrieval_quality_scorer import (
    ALWAYS_PRESENT, HARMFUL, NOT_USEFUL, RetrievalQualityScorer, TOO_FEW_ANSWERS,
    USEFUL,
)
from parts.llm_foundation.retrieval_querier import (
    NOTHING_TO_ASK, NO_KINDS, NO_SUBJECT, QUERIED, RetrievalQuerier, TASK_WORDS,
)
from parts.llm_foundation.structured_output_enforcer import (
    MISSING_FIELD, NOTHING_SUPPORTED, NOT_IN_THE_SET, NOT_JSON, OUT_OF_BOUNDS,
    REPAIRS_EXHAUSTED, StructuredOutputEnforcer, VALIDATED, WAS_TRUNCATED, WRONG_TYPE,
)
from runtime.claim_verification import make_request
from runtime.external_research_types import ResearchFinding
from runtime.llm_types import (
    Embedding, LlmCallRecord, LlmPartBudget, LlmQuotaState, LlmResponse, LlmSpendState,
    METERED, PromptScore, PromptVersion, RetrievalHit, RetrievalQuery, RetrievalScore,
    SUBSCRIPTION, VerifiedSnapshot,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "prompt-template-author": "parts.llm_foundation.prompt_template_author",
    "prompt-registry": "parts.llm_foundation.prompt_registry",
    "retrieval-querier": "parts.llm_foundation.retrieval_querier",
    "knowledge-embedder": "parts.llm_foundation.knowledge_embedder",
    "retrieval-index": "parts.llm_foundation.retrieval_index",
    "context-assembler": "parts.llm_foundation.context_assembler",
    "prompt-renderer": "parts.llm_foundation.prompt_renderer",
    "structured-output-enforcer": "parts.llm_foundation.structured_output_enforcer",
    "golden-case-keeper": "parts.llm_foundation.golden_case_keeper",
    "prompt-evaluator": "parts.llm_foundation.prompt_evaluator",
    "prompt-promotion-gate": "parts.llm_foundation.prompt_promotion_gate",
    "prompt-drift-monitor": "parts.llm_foundation.prompt_drift_monitor",
    "part-token-budgeter": "parts.llm_foundation.part_token_budgeter",
    "decision-cost-accountant": "parts.llm_foundation.decision_cost_accountant",
    "retrieval-quality-scorer": "parts.llm_foundation.retrieval_quality_scorer",
}


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- prompt-template-author -------------------------------------------------

def an_author(minimum_evidence=1):
    author = PromptTemplateAuthor(minimum_evidence=minimum_evidence, now_ns=Clock())
    author.observe_finding(
        ResearchFinding(
            finding_id="f-1", topic="explain-a-trade", statement="s", evidence=("e",),
            source_references=("r",), confidence=0.5, would_be_refuted_by="x",
            is_testable_here=True, found_at_ns=0,
        )
    )
    return author


A_SCHEMA = {"verdict": {"type": "string", "one_of": ["yes", "no"]}}


def test_a_prompt_asking_the_model_to_recall_a_number_is_refused():
    """That is a request for a hallucination with a decimal point."""
    subject = an_author()
    written = subject.write(
        "t-1", "explain-a-trade", "What is the current funding rate for this symbol?",
        A_SCHEMA, ("verified-facts",),
    )
    assert written.state == ASKS_THE_MODEL_TO_RECALL


def test_a_prompt_asking_for_a_trading_decision_is_refused():
    subject = an_author()
    written = subject.write(
        "t-1", "explain-a-trade", "Given the facts, should we enter this position?",
        A_SCHEMA, ("verified-facts",),
    )
    assert written.state == ASKS_FOR_A_TRADING_DECISION


def test_a_prompt_without_an_output_shape_is_refused():
    subject = an_author()
    assert subject.write(
        "t-1", "explain-a-trade", "Describe what happened.", {}, ("verified-facts",),
    ).state == NO_OUTPUT_SHAPE


def test_a_prompt_declaring_no_context_is_refused():
    subject = an_author()
    assert subject.write(
        "t-1", "explain-a-trade", "Describe what happened.", A_SCHEMA, (),
    ).state == NO_CONTEXT_DECLARED


def test_a_template_with_no_measured_lineage_is_refused():
    subject = PromptTemplateAuthor(minimum_evidence=3, now_ns=Clock())
    assert subject.write(
        "t-1", "explain-a-trade", "Describe what happened.", A_SCHEMA, ("verified-facts",),
    ).state == NO_EVIDENCE_TO_WRITE_FROM


def test_a_valid_template_is_written_and_is_not_live():
    subject = an_author()
    written = subject.write(
        "t-1", "explain-a-trade", "Describe what happened using the measured facts.",
        A_SCHEMA, ("verified-facts",),
    )
    assert written.state == WRITTEN
    assert written.template.derived_from
    assert "only the promotion gate makes one active" in written.reason


def test_an_unchanged_rewrite_is_not_a_new_template():
    subject = an_author()
    instruction = "Describe what happened using the measured facts."
    subject.write("t-1", "explain-a-trade", instruction, A_SCHEMA, ("verified-facts",))
    assert subject.write(
        "t-1", "explain-a-trade", instruction, A_SCHEMA, ("verified-facts",)
    ).state == UNCHANGED


def test_the_author_does_not_activate_anything():
    assert importlib.import_module(
        BLOCK_PARTS["prompt-template-author"]
    ).describe_template_authoring(an_author())["activates_a_template"] is False


# ---- prompt-registry --------------------------------------------------------

class Template:
    def __init__(self, template_id="t-1", purpose="explain-a-trade",
                 instruction="describe", schema=None, context=("verified-facts",)):
        self.template_id = template_id
        self.purpose = purpose
        self.instruction = instruction
        self.output_schema = schema or A_SCHEMA
        self.required_context_kinds = context


class Promotion:
    def __init__(self, version_id):
        self.version_id = version_id


def a_registry():
    return PromptRegistry(now_ns=Clock())


def test_a_registered_version_is_not_active_until_it_is_promoted():
    subject = a_registry()
    subject.observe_template(Template())
    outcome = subject.register("t-1")
    assert outcome.state == REGISTERED
    assert outcome.version.is_active is False
    assert subject.active_version("explain-a-trade") is None


def test_promotion_is_the_only_way_a_version_goes_live():
    subject = a_registry()
    subject.observe_template(Template())
    version = subject.register("t-1").version
    assert subject.apply_promotion(Promotion(version.version_id)).state == REGISTRY_PROMOTED
    assert subject.active_version("explain-a-trade").version_id == version.version_id


def test_an_identical_version_is_not_registered_twice():
    subject = a_registry()
    subject.observe_template(Template())
    subject.register("t-1")
    assert subject.register("t-1").state == IDENTICAL_TO_AN_EXISTING_VERSION


def test_a_version_whose_template_is_unknown_is_refused():
    assert a_registry().register("t-missing").state == NO_SUCH_TEMPLATE


def test_a_version_cannot_be_edited():
    subject = a_registry()
    assert not hasattr(subject, "edit")
    assert not hasattr(subject, "amend")
    described = importlib.import_module(
        BLOCK_PARTS["prompt-registry"]
    ).describe_registry(subject)
    assert described["can_edit_a_version"] is False
    assert described["can_activate_without_a_promotion"] is False


def test_promoting_an_older_version_is_recorded_as_a_rollback():
    subject = a_registry()
    subject.observe_template(Template(instruction="first"))
    first = subject.register("t-1").version
    subject.apply_promotion(Promotion(first.version_id))
    subject.observe_template(Template(instruction="second"))
    second = subject.register("t-1").version
    subject.apply_promotion(Promotion(second.version_id))
    subject.apply_promotion(Promotion(first.version_id))
    assert subject.standing.rollbacks == 1
    assert subject.active_version("explain-a-trade").version_id == first.version_id


# ---- knowledge-embedder -----------------------------------------------------

def an_embedder(chunk=40, overlap=10, minimum=10, dimensions=3):
    return KnowledgeEmbedder(
        chunk_characters=chunk, overlap_characters=overlap,
        minimum_characters=minimum, dimensions=dimensions, now_ns=Clock(),
    )


def _a_vector(text):
    return [float(len(text)), 1.0, 0.5]


def test_every_vector_carries_the_model_that_made_it():
    subject = an_embedder()
    subject.install_model("model-a", _a_vector)
    outcome = subject.embed_text("skill", "s://1", "a" * 200)
    assert outcome.state == EMBEDDED
    assert all(embedding.model_id == "model-a" for embedding in outcome.embeddings)


def test_changing_the_model_makes_existing_vectors_stale_rather_than_old():
    subject = an_embedder()
    subject.install_model("model-a", _a_vector)
    subject.embed_text("skill", "s://1", "a" * 200)
    subject.install_model("model-b", _a_vector)
    assert subject.standing.model_changes == 1
    assert subject.stale_vector_count() > 0


def test_chunks_overlap_so_a_rule_spanning_a_boundary_still_retrieves():
    subject = an_embedder(chunk=40, overlap=10)
    chunks = subject.chunks_of("x" * 100)
    offsets = [offset for offset, _ in chunks]
    assert offsets == [0, 30, 60, 90] or offsets[1] - offsets[0] == 30


def test_the_same_text_is_embedded_once():
    subject = an_embedder()
    subject.install_model("model-a", _a_vector)
    subject.embed_text("skill", "s://1", "a" * 200)
    outcome = subject.embed_text("skill", "s://2", "a" * 200)
    assert outcome.reused > 0
    assert subject.standing.chunks_reused > 0


def test_a_wrong_length_vector_is_refused_rather_than_indexed():
    subject = an_embedder(dimensions=3)
    subject.install_model("model-a", lambda text: [1.0, 2.0])
    assert subject.embed_text("skill", "s://1", "a" * 200).state == WRONG_DIMENSIONS


def test_no_model_means_no_default():
    assert an_embedder().embed_text("skill", "s://1", "a" * 200).state == NO_MODEL


# ---- retrieval-querier ------------------------------------------------------

def a_querier(hits=5, floor=0.3, enough_facts=6, minimum_terms=2):
    return RetrievalQuerier(
        maximum_hits=hits, minimum_similarity=floor,
        facts_that_make_retrieval_unnecessary=enough_facts,
        minimum_subject_terms=minimum_terms, now_ns=Clock(),
    )


def a_request(instruction="Explain the funding settlement given the following facts",
              facts=None, purpose="explain-a-trade"):
    return make_request(
        purpose=purpose, venue_id="binance-usdm", symbol="BTCUSDT",
        instruction=instruction, facts=facts or {"funding": 0.0001},
        maximum_sentences=4, now_ns=Clock(),
    )


def test_task_vocabulary_is_dropped_before_querying():
    """Otherwise retrieval returns documents about explaining."""
    subject = a_querier()
    plan = subject.plan(a_request(), ("skill",))
    assert plan.state == QUERIED
    assert not set(plan.subject_terms) & TASK_WORDS
    assert plan.dropped_task_words


def test_a_request_carrying_enough_facts_needs_no_retrieval():
    subject = a_querier(enough_facts=2)
    plan = subject.plan(a_request(facts={"a": 1.0, "b": 2.0}), ("skill",))
    assert plan.state == NOTHING_TO_ASK
    assert "cheapest correct answer" in plan.reason


def test_a_query_names_the_corpora_it_wants():
    assert a_querier().plan(a_request(), ()).state == NO_KINDS


def test_a_similarity_floor_travels_with_every_query():
    plan = a_querier(floor=0.42).plan(a_request(), ("skill",))
    assert plan.query.minimum_similarity == 0.42


def test_a_request_with_no_subject_is_not_queried_on():
    subject = a_querier(minimum_terms=5)
    plan = subject.plan(a_request(instruction="Explain the following", purpose="x"), ("skill",))
    assert plan.state == NO_SUBJECT


# ---- retrieval-index --------------------------------------------------------

def an_index(duplicate=0.99, usefulness_weight=0.3, prior=0.5):
    return RetrievalIndex(
        duplicate_similarity=duplicate, usefulness_weight=usefulness_weight,
        prior_usefulness=prior, now_ns=Clock(),
    )


def an_embedding(reference, vector, kind="skill", model="model-a"):
    return Embedding(
        embedding_id=reference, source_kind=kind, source_reference=reference,
        text=f"text of {reference}", vector=tuple(vector), model_id=model,
        dimensions=len(vector), embedded_at_ns=0,
    )


def a_query(kinds=("skill",), maximum=5, floor=0.5):
    return RetrievalQuery(
        query_id="q-1", request_id="r-1", text="funding settlement",
        wanted_kinds=kinds, maximum_hits=maximum, minimum_similarity=floor,
        asked_at_ns=0,
    )


def test_nothing_above_the_floor_returns_nothing():
    """Returning the least-unrelated passages puts them into a prompt as evidence."""
    subject = an_index()
    subject.observe_embedding(an_embedding("a", [0.0, 1.0]))
    result = subject.retrieve(a_query(floor=0.9), [1.0, 0.0])
    assert result.state == NOTHING_ABOVE_THE_FLOOR
    assert result.hits == ()


def test_near_duplicates_are_collapsed():
    """The same rule in three documents is not three corroborations."""
    subject = an_index(duplicate=0.999)
    for name in ("a", "b", "c"):
        subject.observe_embedding(an_embedding(name, [1.0, 0.0]))
    result = subject.retrieve(a_query(floor=0.5), [1.0, 0.0])
    assert len(result.hits) == 1
    assert result.duplicates_suppressed == 2


def test_vectors_from_a_superseded_model_are_excluded_not_compared():
    subject = an_index()
    subject.observe_embedding(an_embedding("old", [1.0, 0.0], model="model-a"))
    subject.observe_embedding(an_embedding("new", [0.9, 0.1], model="model-b"))
    result = subject.retrieve(a_query(floor=0.5), [1.0, 0.0])
    assert result.excluded_wrong_model == 1
    assert [hit.source_reference for hit in result.hits] == ["new"]


def test_measured_usefulness_can_outrank_wording_similarity():
    subject = an_index(usefulness_weight=0.9, prior=0.0)
    subject.observe_embedding(an_embedding("wordy", [1.0, 0.0]))
    subject.observe_embedding(an_embedding("useful", [0.8, 0.6]))
    subject.observe_score(
        RetrievalScore(
            source_reference="useful", times_retrieved=10,
            times_the_answer_was_good=9, usefulness=0.9, is_fitted=True, scored_at_ns=0,
        )
    )
    result = subject.retrieve(a_query(floor=0.5), [1.0, 0.0])
    assert result.hits[0].source_reference == "useful"


def test_an_empty_index_says_so():
    assert an_index().retrieve(a_query(), [1.0, 0.0]).state == EMPTY_INDEX


def test_the_index_never_returns_the_least_unrelated():
    described = importlib.import_module(
        BLOCK_PARTS["retrieval-index"]
    ).describe_index(an_index())
    assert described["returns_the_least_unrelated_when_nothing_qualifies"] is False
    assert described["compares_vectors_across_models"] is False


# ---- context-assembler ------------------------------------------------------

def an_assembler(staleness=60.0):
    return ContextAssembler(maximum_staleness_seconds=staleness, now_ns=Clock())


def a_snapshot(facts=None, staleness=1.0, complete=True, missing=()):
    return VerifiedSnapshot(
        snapshot_id="s-1", venue_id="binance-usdm", symbol="BTCUSDT",
        facts=facts if facts is not None else {"mid": 70_000.0, "spread": 0.5},
        measured_at_ns=0, staleness_seconds=staleness, is_complete=complete,
        missing_facts=missing,
    )


def a_budget(characters=10_000, part_id="ai-brain"):
    return LlmPartBudget(
        part_id=part_id, calls_allowed=10, tokens_allowed=10_000,
        character_budget=characters, money_allowed=1.0, window_seconds=3600.0,
        calls_used=0, tokens_used=0, money_used=0.0, issued_at_ns=0,
    )


def a_hit(reference="h-1", similarity=0.9, characters=100):
    return RetrievalHit(
        hit_id=reference, query_id="q-1", source_kind="skill",
        source_reference=reference, text="x" * characters, similarity=similarity,
        characters=characters, embedded_at_ns=0, retrieved_at_ns=0,
    )


def test_verified_facts_are_placed_first_and_are_never_dropped():
    subject = an_assembler()
    assembled = subject.assemble("r-1", a_snapshot(), [a_hit()], a_budget())
    assert assembled.state == ASSEMBLED
    assert assembled.context.sections[0][0] == "verified-facts"
    assert assembled.context.verified_facts


def test_assembly_fails_rather_than_dropping_the_facts():
    subject = an_assembler()
    assembled = subject.assemble("r-1", a_snapshot(), [], a_budget(characters=5))
    assert assembled.state == FACTS_DO_NOT_FIT
    assert "nothing to check" in assembled.reason


def test_retrieved_passages_are_dropped_from_the_weakest_upward_and_named():
    subject = an_assembler()
    hits = [a_hit("strong", 0.95, 100), a_hit("weak", 0.55, 100)]
    assembled = subject.assemble("r-1", a_snapshot(), hits, a_budget(characters=200))
    assert "strong" in [label for _, label, _ in assembled.context.sections]
    assert assembled.context.dropped_sections == ("weak",)
    assert assembled.context.was_truncated


def test_a_part_with_no_budget_assembles_nothing():
    assert an_assembler().assemble("r-1", a_snapshot(), [], None).state == NO_BUDGET


def test_a_stale_snapshot_describes_a_market_that_has_moved():
    subject = an_assembler(staleness=10.0)
    assembled = subject.assemble("r-1", a_snapshot(staleness=99.0), [], a_budget())
    assert assembled.state == SNAPSHOT_TOO_STALE


def test_an_incomplete_snapshot_names_what_is_missing():
    subject = an_assembler()
    assembled = subject.assemble(
        "r-1", a_snapshot(complete=False, missing=("funding",)), [], a_budget()
    )
    assert assembled.state == NO_FACTS
    assert "funding" in assembled.reason


def test_nothing_is_summarised_to_fit():
    assert importlib.import_module(
        BLOCK_PARTS["context-assembler"]
    ).describe_assembly(an_assembler())["summarises_to_fit"] is False


# ---- prompt-renderer --------------------------------------------------------

def a_version(version_id="t-1:v1", context=("verified-facts",), schema=None):
    return PromptVersion(
        version_id=version_id, template_id="t-1", purpose="explain-a-trade",
        instruction="Describe what happened using the measured facts.",
        output_schema=schema if schema is not None else A_SCHEMA,
        required_context_kinds=context, is_active=True, promoted_at_ns=0,
        created_at_ns=0,
    )


def a_context(sections=None):
    from runtime.llm_types import PromptContext

    return PromptContext(
        context_id="c-1", request_id="r-1",
        sections=sections if sections is not None else (
            ("verified-facts", "binance-usdm:BTCUSDT", "mid = 70000.0"),
        ),
        characters=40, character_budget=1000, dropped_sections=(),
        verified_facts={"mid": 70_000.0}, assembled_at_ns=0,
    )


def test_facts_are_rendered_as_a_labelled_block_not_inside_prose():
    subject = PromptRenderer(now_ns=Clock())
    outcome = subject.render(a_request(), a_version(), a_context())
    assert outcome.state == RENDERED
    assert FACTS_HEADING in outcome.rendered.text


def test_two_calls_are_the_same_call_only_if_every_input_matches():
    subject = PromptRenderer(now_ns=Clock())
    first = subject.render(a_request(), a_version(), a_context()).rendered
    second = subject.render(a_request(), a_version(), a_context()).rendered
    third = subject.render(
        a_request(facts={"funding": 0.0002}), a_version(), a_context()
    ).rendered
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint


def test_a_version_missing_its_declared_context_is_not_rendered():
    subject = PromptRenderer(now_ns=Clock())
    outcome = subject.render(
        a_request(), a_version(context=("verified-facts", "retrieved")), a_context()
    )
    assert outcome.state == MISSING_CONTEXT
    assert "retrieved" in outcome.missing_context_kinds


def test_nothing_is_rendered_without_an_output_schema():
    subject = PromptRenderer(now_ns=Clock())
    assert subject.render(a_request(), a_version(schema={}), a_context()).state == NO_OUTPUT_SCHEMA


def test_nothing_is_rendered_without_an_active_version():
    subject = PromptRenderer(now_ns=Clock())
    assert subject.render(a_request(), None, a_context()).state == NO_ACTIVE_VERSION


# ---- structured-output-enforcer ---------------------------------------------

def an_enforcer(repairs=2, tolerance=0.01, citation=True):
    return StructuredOutputEnforcer(
        maximum_repairs=repairs, relative_tolerance=tolerance,
        require_a_citation=citation, now_ns=Clock(),
    )


ENFORCED_SCHEMA = {
    "verdict": {"type": "string", "one_of": ["yes", "no"]},
    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "text": {"type": "string"},
}


def a_response(payload, finish_reason="stop", rendered_id="r-1"):
    return LlmResponse(
        response_id="resp-1", rendered_id=rendered_id, version_id="t-1:v1",
        model_id="model-x", text=payload if isinstance(payload, str) else json.dumps(payload),
        finish_reason=finish_reason, input_tokens=100, output_tokens=50,
        latency_seconds=1.0, payment_kind=SUBSCRIPTION, was_cached=False,
        responded_at_ns=0,
    )


def test_a_truncated_answer_fails_even_when_it_parses():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response({"verdict": "yes", "confidence": 0.5, "text": "The mid was 70000.0."},
                   finish_reason="length"),
        a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == WAS_TRUNCATED


def test_a_missing_field_is_not_filled_with_a_default():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response({"confidence": 0.5, "text": "The mid was 70000.0."}),
        a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == MISSING_FIELD
    assert "nobody measured" in outcome.reason


def test_a_value_outside_the_declared_set_is_refused():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response({"verdict": "maybe", "confidence": 0.5, "text": "The mid was 70000.0."}),
        a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == NOT_IN_THE_SET


def test_a_number_outside_its_bounds_is_refused():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response({"verdict": "yes", "confidence": 4.0, "text": "The mid was 70000.0."}),
        a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == OUT_OF_BOUNDS


def test_an_unsupported_sentence_is_removed_and_named():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response({
            "verdict": "yes", "confidence": 0.5,
            "text": "The mid was 70000.0. The daily volume was 42000000.0.",
        }),
        a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == VALIDATED
    assert outcome.output.removed_sentences


def a_rendered_request(renderer, request, schema=ENFORCED_SCHEMA):
    """What the enforcer judges a response against on the spine: the rendered request."""
    outcome = renderer.render(request, a_version(schema=schema), a_context())
    assert outcome.state == RENDERED, outcome.reason
    return outcome.rendered


def an_asked_request(facts=None):
    return make_request(
        purpose="explain-a-trade", venue_id="binance-usdm", symbol="BTCUSDT",
        instruction="Describe what happened using the measured facts.",
        facts=facts or {"mid": 70_000.0}, maximum_sentences=4, now_ns=Clock(),
        asked_by="trade-narrative-writer",
    )


def test_a_repair_is_a_request_every_reader_of_the_wire_can_read():
    """Until 2026-09-13 it was a dict, and `request.purpose` crashed the renderer and picker."""
    renderer = PromptRenderer(now_ns=Clock())
    subject = an_enforcer(repairs=2)
    asked = an_asked_request()
    rendered = a_rendered_request(renderer, asked)

    outcome = subject.enforce(
        a_response("not json at all", rendered_id=rendered.rendered_id),
        a_version(schema=ENFORCED_SCHEMA), dict(rendered.facts), rendered=rendered,
    )

    repair = outcome.retry_request
    assert isinstance(repair, type(asked))
    assert repair.purpose == asked.purpose
    assert (repair.venue_id, repair.symbol) == (asked.venue_id, asked.symbol)
    assert repair.asked_by == "trade-narrative-writer"
    assert repair.facts == asked.facts
    assert repair.repair_of == rendered.rendered_id
    assert repair.repair_attempt == 1
    assert NOT_JSON in repair.previous_failures


def test_the_renderer_shows_the_model_why_its_last_answer_was_rejected():
    renderer = PromptRenderer(now_ns=Clock())
    rendered = a_rendered_request(renderer, an_asked_request())
    repair = an_enforcer().enforce(
        a_response("not json", rendered_id=rendered.rendered_id),
        a_version(schema=ENFORCED_SCHEMA), dict(rendered.facts), rendered=rendered,
    ).retry_request

    rerendered = a_rendered_request(renderer, repair)

    assert rerendered.rendered_id != rendered.rendered_id
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in rerendered.text
    assert NOT_JSON in rerendered.text
    assert rerendered.repair_of == rendered.rendered_id
    assert rerendered.repair_attempt == 1
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" not in rendered.text


def test_repairs_are_bounded_across_the_chain_not_per_render():
    """Every repair is a new render. Counted per render, the bound was never reached."""
    renderer = PromptRenderer(now_ns=Clock())
    subject = an_enforcer(repairs=2)
    version = a_version(schema=ENFORCED_SCHEMA)
    rendered = a_rendered_request(renderer, an_asked_request())
    states = []
    for _ in range(4):
        outcome = subject.enforce(
            a_response("still not json", rendered_id=rendered.rendered_id),
            version, dict(rendered.facts), rendered=rendered,
        )
        states.append(outcome.state)
        if outcome.retry_request is None:
            break
        rendered = a_rendered_request(renderer, outcome.retry_request)

    assert states == [NOT_JSON, NOT_JSON, REPAIRS_EXHAUSTED]
    assert subject.standing.repairs_requested == 2
    assert subject.standing.repairs_exhausted == 1


def test_the_repair_count_travels_with_a_validated_answer():
    renderer = PromptRenderer(now_ns=Clock())
    subject = an_enforcer(repairs=3)
    version = a_version(schema=ENFORCED_SCHEMA)
    rendered = a_rendered_request(renderer, an_asked_request())
    repair = subject.enforce(
        a_response("not json", rendered_id=rendered.rendered_id),
        version, dict(rendered.facts), rendered=rendered,
    ).retry_request
    rerendered = a_rendered_request(renderer, repair)

    outcome = subject.enforce(
        a_response({"verdict": "yes", "confidence": 0.5, "text": "The mid was 70000.0."},
                   rendered_id=rerendered.rendered_id),
        version, dict(rerendered.facts), rendered=rerendered,
    )
    assert outcome.output.repair_attempts == 1


def test_without_the_rendered_request_a_rejection_is_final_and_counted():
    subject = an_enforcer()
    outcome = subject.enforce(
        a_response("not json"), a_version(schema=ENFORCED_SCHEMA), {"mid": 70_000.0},
    )
    assert outcome.state == NOT_JSON
    assert outcome.retry_request is None
    assert subject.standing.repairs_not_possible == 1


def test_the_enforcer_fills_no_defaults():
    assert importlib.import_module(
        BLOCK_PARTS["structured-output-enforcer"]
    ).describe_enforcement(an_enforcer())["fills_missing_fields_with_defaults"] is False


# ---- golden-case-keeper -----------------------------------------------------

def a_keeper(tolerance=0.3, minimum=2):
    return GoldenCaseKeeper(
        balance_tolerance=tolerance, minimum_cases_before_balance_matters=minimum,
        now_ns=Clock(),
    )


def _keep(subject, case_id="g-1", fact_time=100, decided_at=200, profitable=True):
    return subject.keep(
        case_id=case_id, purpose="explain-a-trade", facts={"mid": 70_000.0},
        fact_times_ns={"mid": fact_time}, context_sections=(),
        expected_value={"verdict": "yes"}, decided_at_ns=decided_at,
        outcome_known_at_ns=decided_at + 1000, was_profitable=profitable,
        source_reference="trade:1",
    )


def test_a_fact_measured_after_the_decision_is_refused():
    """A prompt evaluated on it scores perfectly and is useless live."""
    subject = a_keeper()
    outcome = _keep(subject, fact_time=500, decided_at=200)
    assert outcome.state == CONTAINS_THE_FUTURE
    assert outcome.offending_facts == ("mid",)


def test_an_open_trade_has_no_right_answer():
    subject = a_keeper()
    outcome = subject.keep(
        case_id="g-1", purpose="p", facts={}, fact_times_ns={}, context_sections=(),
        expected_value={}, decided_at_ns=100, outcome_known_at_ns=None,
        was_profitable=None, source_reference="trade:1",
    )
    assert outcome.state == OUTCOME_NOT_KNOWN


def test_an_unbalanced_set_is_reported():
    subject = a_keeper(tolerance=0.1, minimum=2)
    for index in range(5):
        _keep(subject, f"g-{index}", profitable=True)
    assert subject.is_balanced("explain-a-trade") is False


def test_a_case_is_retired_for_a_reason_about_the_world():
    subject = a_keeper()
    _keep(subject)
    outcome = subject.retire("g-1", WORLD_HAS_CHANGED)
    assert outcome.state == RETIRED
    assert subject.cases_for("explain-a-trade") == ()


def test_a_case_is_never_retired_for_scoring_badly():
    subject = a_keeper()
    _keep(subject)
    with pytest.raises(ValueError):
        subject.retire("g-1", "the-prompt-did-badly-on-it")


def test_cases_cannot_be_edited():
    subject = a_keeper()
    assert not hasattr(subject, "edit")
    described = importlib.import_module(
        BLOCK_PARTS["golden-case-keeper"]
    ).describe_golden_cases(subject)
    assert described["can_edit_a_case"] is False
    assert described["retires_a_case_for_scoring_badly"] is False


# ---- prompt-evaluator -------------------------------------------------------

class Case:
    def __init__(self, case_id):
        self.case_id = case_id


def an_evaluator(minimum=2):
    return PromptEvaluator(minimum_cases=minimum, now_ns=Clock())


def a_case_result(case_id, version_id="v1", valid=True, unsupported=0, total=4,
                  agreed=True, tokens=100, latency=1.0):
    return CaseResult(
        case_id=case_id, version_id=version_id, was_schema_valid=valid,
        unsupported_sentences=unsupported, total_sentences=total,
        agreed_with_outcome=agreed, output_tokens=tokens, latency_seconds=latency,
        repair_attempts=0,
    )


def test_the_four_numbers_are_kept_apart():
    subject = an_evaluator()
    cases = [Case("g-1"), Case("g-2")]
    subject.observe_result(a_case_result("g-1", valid=True, unsupported=0, agreed=True))
    subject.observe_result(a_case_result("g-2", valid=False, unsupported=2, agreed=False))
    evaluation = subject.evaluate("v1", "explain-a-trade", cases)
    assert evaluation.state == SCORED
    assert evaluation.score.schema_valid_fraction == 0.5
    assert evaluation.score.agreement_with_outcome == 0.5
    assert evaluation.score.factually_supported_fraction < 1.0


def test_a_version_that_did_not_answer_every_case_is_not_scored():
    """Scoring the subset rewards a version for the cases it survived."""
    subject = an_evaluator()
    subject.observe_result(a_case_result("g-1"))
    evaluation = subject.evaluate("v1", "explain-a-trade", [Case("g-1"), Case("g-2")])
    assert evaluation.state == INCOMPLETE_RUN
    assert evaluation.cases_missing == ("g-2",)


def test_versions_scored_on_different_case_sets_are_not_comparable():
    subject = an_evaluator()
    for case_id in ("g-1", "g-2"):
        subject.observe_result(a_case_result(case_id, version_id="v1"))
        subject.observe_result(a_case_result(case_id, version_id="v2"))
    subject.observe_result(a_case_result("g-3", version_id="v2"))
    subject.evaluate("v1", "explain-a-trade", [Case("g-1"), Case("g-2")])
    evaluation = subject.evaluate(
        "v2", "explain-a-trade", [Case("g-1"), Case("g-2"), Case("g-3")]
    )
    assert evaluation.state == NOT_THE_SAME_CASES


def test_no_golden_cases_means_unmeasured_not_good():
    evaluation = an_evaluator().evaluate("v1", "explain-a-trade", [])
    assert evaluation.state == NO_CASES
    assert "unmeasured, not good" in evaluation.reason


def test_the_evaluator_never_grades_against_another_model():
    assert importlib.import_module(
        BLOCK_PARTS["prompt-evaluator"]
    ).describe_evaluation(an_evaluator())["grades_against_another_model"] is False


# ---- prompt-promotion-gate --------------------------------------------------

ABSOLUTE_BAR = {
    "schema_valid_fraction": 0.9,
    "factually_supported_fraction": 0.9,
    "agreement_with_outcome": 0.55,
}


def a_gate(margin=0.05, tolerance=0.0, cost_ratio=1.5):
    return PromptPromotionGate(
        required_margin=margin, regression_tolerance=tolerance,
        maximum_cost_ratio=cost_ratio, absolute_bar=ABSOLUTE_BAR, now_ns=Clock(),
    )


def a_score(version_id, agreement=0.7, schema=1.0, support=1.0, tokens=100.0, cases=40):
    return PromptScore(
        version_id=version_id, purpose="explain-a-trade", cases_run=cases,
        schema_valid_fraction=schema, factually_supported_fraction=support,
        agreement_with_outcome=agreement, mean_output_tokens=tokens,
        mean_latency_seconds=1.0, is_fitted=True, scored_at_ns=0,
    )


def test_a_difference_inside_the_margin_is_noise():
    subject = a_gate(margin=0.05)
    subject.observe_score(a_score("v1", agreement=0.70))
    subject.observe_score(a_score("v2", agreement=0.72))
    assert subject.decide("v2", "v1", "t-1").state == INSIDE_THE_MARGIN


def test_a_version_may_not_buy_agreement_with_structural_validity():
    subject = a_gate()
    subject.observe_score(a_score("v1", agreement=0.60, schema=1.0))
    subject.observe_score(a_score("v2", agreement=0.80, schema=0.7))
    decision = subject.decide("v2", "v1", "t-1")
    assert decision.state == A_DIMENSION_REGRESSED
    assert "schema_valid_fraction" in decision.regressions


def test_a_much_more_expensive_winner_has_not_won():
    subject = a_gate(margin=0.02, cost_ratio=1.5)
    subject.observe_score(a_score("v1", agreement=0.70, tokens=100.0))
    subject.observe_score(a_score("v2", agreement=0.74, tokens=400.0))
    decision = subject.decide("v2", "v1", "t-1")
    assert decision.state == COSTS_TOO_MUCH_MORE
    assert decision.cost_ratio == pytest.approx(4.0)


def test_a_clear_winner_is_promoted():
    subject = a_gate(margin=0.05, cost_ratio=2.0)
    subject.observe_score(a_score("v1", agreement=0.60))
    subject.observe_score(a_score("v2", agreement=0.75))
    decision = subject.decide("v2", "v1", "t-1")
    assert decision.state == PROMOTED
    assert decision.promotion.replaces == "v1"


def test_the_first_version_must_clear_an_absolute_bar():
    """Being the only candidate is not being good enough."""
    subject = a_gate()
    subject.observe_score(a_score("v1", agreement=0.3, schema=0.5, support=0.5))
    assert subject.decide("v1", None, "t-1").state == BELOW_THE_ABSOLUTE_BAR
    subject.observe_score(a_score("v2", agreement=0.7))
    assert subject.decide("v2", None, "t-1").state == PROMOTED


def test_the_bar_must_cover_every_quality_dimension():
    with pytest.raises(ValueError):
        PromptPromotionGate(
            required_margin=0.05, regression_tolerance=0.0, maximum_cost_ratio=1.5,
            absolute_bar={"schema_valid_fraction": 0.9},
        )
    assert len(QUALITY_DIMENSIONS) == 3


# ---- prompt-drift-monitor ---------------------------------------------------

def a_monitor(window=10, threshold=2.0, minimum=3):
    return PromptDriftMonitor(
        baseline_window=window, deviation_threshold=threshold,
        minimum_baseline=minimum, now_ns=Clock(),
    )


def _feed(monitor, version_id, agreements, model_id=None):
    alerts = []
    for value in agreements:
        alerts.extend(monitor.observe(a_score(version_id, agreement=value), model_id))
    return alerts


def test_an_unexplained_improvement_alerts_too():
    """Only the degradation gets noticed by anyone watching quality."""
    subject = a_monitor(threshold=2.0, minimum=3)
    _feed(subject, "v1", [0.70, 0.71, 0.70, 0.71, 0.70])
    alerts = _feed(subject, "v1", [0.95])
    assert any(alert.state == IMPROVED for alert in alerts)


def test_a_degradation_names_the_dimension_and_the_baseline():
    subject = a_monitor(threshold=2.0, minimum=3)
    _feed(subject, "v1", [0.70, 0.71, 0.70, 0.71, 0.70])
    alerts = _feed(subject, "v1", [0.20])
    degraded = [alert for alert in alerts if alert.state == DEGRADED]
    assert degraded
    assert degraded[0].dimension in WATCHED_DIMENSIONS
    assert degraded[0].baseline_mean is not None


def test_a_model_change_is_a_known_cause_not_drift_to_investigate():
    subject = a_monitor()
    _feed(subject, "v1", [0.70, 0.71, 0.70], model_id="model-a")
    alerts = _feed(subject, "v1", [0.70], model_id="model-b")
    assert any(alert.state == MODEL_CHANGED for alert in alerts)
    assert subject.baseline_for("v1", "agreement_with_outcome") is None


def test_a_promotion_resets_the_baseline():
    subject = a_monitor()
    _feed(subject, "v1", [0.70, 0.71, 0.70])
    subject.reset_baseline("v1")
    assert subject.standing.baselines_reset == 1
    assert subject.baseline_for("v1", "agreement_with_outcome") is None


def test_the_threshold_is_in_deviations_not_percent():
    described = importlib.import_module(
        BLOCK_PARTS["prompt-drift-monitor"]
    ).describe_drift_monitoring(a_monitor())
    assert described["uses_a_fixed_percentage_threshold"] is False
    assert described["rolls_anything_back"] is False


# ---- part-token-budgeter ----------------------------------------------------

def a_budgeter(window=3600.0, starting=0.2, characters_per_token=4.0):
    return PartTokenBudgeter(
        window_seconds=window, starting_share=starting, prior_usefulness=0.5,
        prior_weight=4.0, half_life_observations=200,
        minimum_usefulness_observations=5,
        characters_per_token=characters_per_token, now_ns=Clock(),
    )


def _with_pool(budgeter, calls=100, tokens=100_000, ceiling=10.0, spent=0.0):
    budgeter.observe_quota(
        LlmQuotaState(
            window_seconds=3600.0, calls_used=0, calls_allowed=calls, tokens_used=0,
            tokens_allowed=tokens, resets_at_ns=0, measured_at_ns=0,
        )
    )
    budgeter.observe_spend(
        LlmSpendState(
            period_seconds=86_400.0, spent=spent, ceiling=ceiling, calls=0,
            period_started_at_ns=0, measured_at_ns=0,
        )
    )
    return budgeter


def a_call_record(part_id="ai-brain", money=0.0, kind=SUBSCRIPTION, tokens=100):
    return LlmCallRecord(
        call_id="c-1", part_id=part_id, purpose="explain-a-trade", version_id="v1",
        model_id="model-x", payment_kind=kind, input_tokens=tokens, output_tokens=tokens,
        money_spent=money, quota_spent=1.0, latency_seconds=1.0, was_cached=False,
        succeeded=True, called_at_ns=0,
    )


def test_a_part_that_produced_value_gets_a_larger_share():
    subject = _with_pool(a_budgeter())
    for _ in range(20):
        subject.observe_usefulness("useful-part", True)
        subject.observe_usefulness("wasteful-part", False)
    useful = subject.issue("useful-part")
    wasteful = subject.issue("wasteful-part")
    assert useful.share > wasteful.share
    assert useful.is_fitted


def test_a_new_part_is_told_its_share_is_a_starting_allocation():
    subject = _with_pool(a_budgeter())
    issued = subject.issue("brand-new-part")
    assert issued.is_fitted is False
    assert "starting allocation" in issued.reason


def test_nothing_is_allocated_from_a_pool_that_is_empty():
    subject = a_budgeter()
    subject.observe_quota(
        LlmQuotaState(
            window_seconds=3600.0, calls_used=100, calls_allowed=100, tokens_used=1000,
            tokens_allowed=1000, resets_at_ns=0, measured_at_ns=0,
        )
    )
    issued = subject.issue("ai-brain")
    assert issued.state == NOTHING_LEFT_TO_ALLOCATE
    assert issued.permits_a_call is False


def test_an_exhausted_part_stops_asking_rather_than_queueing():
    subject = _with_pool(a_budgeter(), calls=2, tokens=100)
    for _ in range(5):
        subject.observe_call(a_call_record("ai-brain", tokens=100))
    issued = subject.issue("ai-brain")
    assert issued.state == EXHAUSTED
    assert "rather than queueing" in issued.reason


def test_a_character_budget_is_derived_from_the_token_budget():
    subject = _with_pool(a_budgeter(characters_per_token=4.0))
    issued = subject.issue("ai-brain")
    assert issued.budget.character_budget == issued.budget.tokens_allowed * 4


def test_the_budgeter_grants_no_exceptions():
    described = importlib.import_module(
        BLOCK_PARTS["part-token-budgeter"]
    ).describe_budgeting(a_budgeter())
    assert described["grants_exceptions"] is False
    assert described["queues_a_part_that_is_out_of_budget"] is False


# ---- decision-cost-accountant -----------------------------------------------

def an_accountant():
    return DecisionCostAccountant(now_ns=Clock())


def test_reasoning_that_cost_more_than_the_trade_returned_is_visible():
    """No PnL statement would have shown it."""
    subject = an_accountant()
    for index in range(5):
        subject.observe_call("d-1", a_call_record(money=1.0, kind=METERED))
    subject.observe_intent("d-1", True)
    subject.observe_realised("d-1", 0.50)
    account = subject.account("d-1")
    assert account.state == SETTLED
    assert account.cost.cost_exceeded_the_gain
    assert account.was_worth_making is False


def test_the_two_currencies_are_never_netted():
    subject = an_accountant()
    subject.observe_call("d-1", a_call_record(money=2.0, kind=METERED))
    subject.observe_call("d-1", a_call_record(kind=SUBSCRIPTION))
    subject.observe_intent("d-1", True)
    subject.observe_realised("d-1", 10.0)
    cost = subject.account("d-1").cost
    assert cost.money_spent == 2.0
    assert cost.quota_spent == 1.0


def test_an_open_position_is_not_judged():
    subject = an_accountant()
    subject.observe_call("d-1", a_call_record(money=1.0, kind=METERED))
    subject.observe_intent("d-1", True)
    account = subject.account("d-1")
    assert account.state == NOT_CLOSED
    assert account.was_worth_making is None


def test_a_decision_that_produced_no_trade_still_cost_something():
    subject = an_accountant()
    subject.observe_call("d-1", a_call_record(money=3.0, kind=METERED))
    subject.observe_intent("d-1", False)
    account = subject.account("d-1")
    assert account.state == NO_TRADE
    assert subject.standing.total_money_spent_on_no_trade == 3.0


def test_cost_is_attributed_to_the_parts_that_made_the_calls():
    subject = an_accountant()
    subject.observe_call("d-1", a_call_record("ai-brain"))
    subject.observe_call("d-1", a_call_record("bull-bot"))
    subject.observe_call("d-1", a_call_record("bull-bot"))
    subject.observe_intent("d-1", True)
    subject.observe_realised("d-1", 1.0)
    assert subject.account("d-1").calls_by_part == {"ai-brain": 1, "bull-bot": 2}


# ---- retrieval-quality-scorer -----------------------------------------------

def a_retrieval_scorer(minimum=2, margin=0.1):
    return RetrievalQualityScorer(
        minimum_answers=minimum, useful_margin=margin, prior_quality=0.5,
        prior_weight=4.0, half_life_observations=200, now_ns=Clock(),
    )


def test_a_source_dropped_before_the_prompt_is_not_credited():
    """Crediting it would reward a passage nobody read."""
    subject = a_retrieval_scorer()
    for index in range(5):
        subject.observe_hit(f"a-{index}", a_hit("dropped"), made_it_into_the_prompt=False)
        subject.observe_answer(f"a-{index}", was_good=True)
    standing = subject.score("dropped")
    assert standing.state == TOO_FEW_ANSWERS
    assert subject.standing.sources_dropped_before_the_prompt == 5


def test_a_source_present_in_every_answer_cannot_be_measured():
    subject = a_retrieval_scorer()
    for index in range(5):
        subject.observe_hit(f"a-{index}", a_hit("always"), True)
        subject.observe_answer(f"a-{index}", was_good=True)
    standing = subject.score("always")
    assert standing.state == ALWAYS_PRESENT
    assert "not the same as excellent" in standing.reason


def test_usefulness_is_measured_against_answers_without_the_source():
    subject = a_retrieval_scorer(minimum=2, margin=0.2)
    for index in range(5):
        subject.observe_hit(f"good-{index}", a_hit("helpful"), True)
        subject.observe_answer(f"good-{index}", was_good=True)
    for index in range(5):
        subject.observe_answer(f"plain-{index}", was_good=False)
    standing = subject.score("helpful")
    assert standing.state == USEFUL
    assert standing.difference > 0


def test_a_source_that_drags_answers_down_is_reported_not_removed():
    subject = a_retrieval_scorer(minimum=2, margin=0.2)
    for index in range(5):
        subject.observe_hit(f"bad-{index}", a_hit("harmful"), True)
        subject.observe_answer(f"bad-{index}", was_good=False)
    for index in range(5):
        subject.observe_answer(f"plain-{index}", was_good=True)
    standing = subject.score("harmful")
    assert standing.state == HARMFUL
    assert standing.should_be_demoted
    described = importlib.import_module(
        BLOCK_PARTS["retrieval-quality-scorer"]
    ).describe_retrieval_scoring(subject)
    assert described["removes_anything"] is False
    assert described["sources_removed"] == 0


def test_a_source_inside_the_margin_is_matched_wording_not_help():
    subject = a_retrieval_scorer(minimum=2, margin=0.5)
    for index in range(4):
        subject.observe_hit(f"a-{index}", a_hit("neutral"), True)
        subject.observe_answer(f"a-{index}", was_good=index % 2 == 0)
    for index in range(4):
        subject.observe_answer(f"b-{index}", was_good=index % 2 == 0)
    assert subject.score("neutral").state == NOT_USEFUL


def test_a_purposes_first_template_is_written_before_any_evidence_exists():
    """Otherwise the chain can never start, and nothing says so.

    The evidence bar's own reason is about rewriting -- "rewriting from opinion is
    how a prompt gets worse in a way nothing detects" -- which is right for a
    replacement and cannot apply to a template that does not exist. And waiting
    does not help: on this system the evidence is skills, and a skill can only be
    distilled by a model that cannot be called until a template exists.

    Measured on the live spine 2026-09-07: `prompt_minimum_evidence` 3, evidence
    held 0, `prompt-renderer` refusing all 132 requests it saw, and
    `llm-request-router` holding 12,154 it could not route.
    """
    author = PromptTemplateAuthor(minimum_evidence=3)

    written = author.write(
        template_id="template:distil-a-source-into-structure",
        purpose="distil-a-source-into-structure",
        instruction="Using only the measured facts given, state what they show.",
        output_schema={"venue_id": "str", "symbol": "str", "text": "str"},
        required_context_kinds=("verified-facts",),
        is_the_first_for_this_purpose=True,
    )

    assert written.state == WRITTEN, written.reason
    assert written.is_usable
    assert author.standing.first_templates_written_before_any_evidence == 1
    # Counted apart from an evidence-derived template, because they are different
    # objects and a board showing them as one would hide which is which.
    assert author.standing.rejected_no_evidence == 0


def test_every_other_guard_still_applies_to_a_first_template():
    """Only the evidence count is waived. A first template that asks the model to
    decide something this system decides is still refused."""
    author = PromptTemplateAuthor(minimum_evidence=3)

    refused = author.write(
        template_id="template:x", purpose="argue-against-this-trade",
        instruction="Given the facts, should we buy this one? Pick the position size.",
        output_schema={"text": "str"},
        required_context_kinds=("verified-facts",),
        is_the_first_for_this_purpose=True,
    )

    assert refused.state != WRITTEN and not refused.is_usable
    assert author.standing.rejected_asks_to_decide == 1


def test_a_rewrite_still_needs_evidence():
    """The bar it was written for is untouched: a replacement derived from nothing
    measured is exactly the drift the counter exists to catch."""
    author = PromptTemplateAuthor(minimum_evidence=3)

    refused = author.write(
        template_id="template:x", purpose="argue-against-this-trade",
        instruction="Using only the measured facts given, state what they show.",
        output_schema={"text": "str"},
        required_context_kinds=("verified-facts",),
        is_the_first_for_this_purpose=False,
    )

    assert refused.state != WRITTEN and not refused.is_usable
    assert author.standing.rejected_no_evidence == 1


def test_a_context_is_still_a_context_with_no_retrieved_passage():
    """Retrieval finding nothing relevant must not cancel the prompt.

    Measured live 2026-09-16, once an embedding model was installed and the index
    began answering: 67 of 75 queries had nothing above the 0.5 similarity floor
    against a corpus of 14 arxiv chunks, so 0 contexts were assembled and
    prompt-renderer refused 901 of 904 requests -- while 84,217 verified
    snapshots sat unused. This part's own priority says passages are the
    compressible part and facts are what must never be dropped, so a context of
    facts alone is the valid end state it already drops passages towards.
    """
    subject = an_assembler()
    assembled = subject.assemble("r-1", a_snapshot(), (), a_budget())

    assert assembled.state == ASSEMBLED
    assert assembled.context.verified_facts
    assert assembled.context.sections[0][0] == "verified-facts"
    assert all(kind != "retrieved-passages" for kind, _ in assembled.context.sections[1:])


def test_with_no_passage_and_no_facts_it_still_refuses():
    """The case the docstring is about is unchanged: an answer with nothing
    measured to check it against is the one a reader believes hardest."""
    subject = an_assembler()
    assembled = subject.assemble("r-1", None, (), a_budget())
    assert assembled.state == NO_FACTS
    assert assembled.context is None
