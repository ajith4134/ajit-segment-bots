"""retrieval-querier: turns a request into what should actually be looked up.

Retrieving on the raw text of a request is the default and it is wrong in a specific
way: the request is mostly instruction, and the instruction words are the ones every
document shares. "Explain why this trade was entered given the following facts"
retrieves documents about explaining, not about the facts.

So the querier separates the parts of a request that identify subject matter from
the parts that describe the task, and queries on the first. Three further decisions,
each because the naive version fails:

- **A query names the kinds it wants.** Asking one undifferentiated question across
  skills, journals and source documents returns whichever corpus is largest. Kinds
  are asked for explicitly and the caller sees which were searched.
- **A similarity floor travels with the query.** Retrieval always returns its top-k
  -- a floorless query on an empty subject returns the k least-unrelated passages,
  which then get pasted into a prompt as evidence.
- **A request with enough facts of its own may need no retrieval at all.** That is a
  real outcome and the cheapest one: this part says so rather than manufacturing a
  query to justify a lookup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import RetrievalQuery
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "retrieval-querier"

PART_DECLARATION = PartDeclaration(
    part_id="retrieval-querier",
    consumes=("llm-request",),
    produces=("retrieval-query", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

QUERIED = "queried"
NOTHING_TO_ASK = "the-request-carries-enough-facts-of-its-own"
NO_SUBJECT = "nothing-in-the-request-identifies-a-subject"
NO_KINDS = "no-corpus-was-asked-for"

# Words that describe the task rather than the subject. Retrieving on these returns
# whatever corpus talks most about explaining and summarising.
TASK_WORDS = frozenset(
    """explain describe summarise summarize write draft phrase produce output return
    given following below above using based answer respond consider analyse analyze
    provide list state""".split()
)


@dataclass(frozen=True)
class QueryPlan:
    request_id: str
    state: str
    query: RetrievalQuery | None
    subject_terms: tuple
    dropped_task_words: tuple
    reason: str
    planned_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == QUERIED and self.query is not None


@dataclass
class QuerierStanding:
    requests_seen: int = 0
    queries_made: int = 0
    needed_no_retrieval: int = 0
    without_a_subject: int = 0
    task_words_dropped: int = 0


class RetrievalQuerier:
    """Builds queries from the subject of a request, not from its instruction."""

    def __init__(
        self,
        maximum_hits: int,
        minimum_similarity: float,
        facts_that_make_retrieval_unnecessary: int,
        minimum_subject_terms: int,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_hits < 1:
            raise ValueError("a query allowed zero hits retrieves nothing")
        if not 0.0 < minimum_similarity < 1.0:
            raise ValueError(
                "a floorless query returns the k least-unrelated passages, which then "
                "get pasted into a prompt as evidence"
            )
        if facts_that_make_retrieval_unnecessary < 1:
            raise ValueError(
                "some number of carried facts makes a lookup unnecessary; zero means "
                "always retrieve, which is the habit this part exists to break"
            )
        if minimum_subject_terms < 1:
            raise ValueError("a query with no subject term is a query about instructions")
        self._maximum_hits = maximum_hits
        self._minimum_similarity = minimum_similarity
        self._enough_facts = facts_that_make_retrieval_unnecessary
        self._minimum_subject_terms = minimum_subject_terms
        self._now_ns = now_ns
        self._sequence = 0
        self.standing = QuerierStanding()

    def subject_of(self, request) -> tuple:
        """The words that identify what this is about, task vocabulary removed."""
        words = [
            word.strip(".,;:()[]\"'").lower()
            for word in f"{request.purpose} {request.instruction}".split()
        ]
        subject = tuple(
            word for word in words if word and word not in TASK_WORDS and len(word) > 2
        )
        return subject

    def plan(self, request, wanted_kinds) -> QueryPlan:
        self.standing.requests_seen += 1
        wanted_kinds = tuple(wanted_kinds or ())

        if not wanted_kinds:
            return self._plan(
                request, NO_KINDS, None, (), (),
                "no corpus was asked for. One undifferentiated question across every "
                "corpus returns whichever is largest",
            )

        if len(request.facts) >= self._enough_facts:
            self.standing.needed_no_retrieval += 1
            return self._plan(
                request, NOTHING_TO_ASK, None, (), (),
                f"the request already carries {len(request.facts)} measured fact(s), at or "
                f"above the {self._enough_facts} that make a lookup unnecessary. Not "
                f"retrieving is the cheapest correct answer",
            )

        all_words = [
            word.strip(".,;:()[]\"'").lower()
            for word in f"{request.purpose} {request.instruction}".split()
        ]
        subject = self.subject_of(request)
        dropped = tuple(word for word in all_words if word in TASK_WORDS)
        self.standing.task_words_dropped += len(dropped)

        if len(subject) < self._minimum_subject_terms:
            self.standing.without_a_subject += 1
            return self._plan(
                request, NO_SUBJECT, None, subject, dropped,
                "nothing in the request identifies a subject once task vocabulary is "
                "removed. Querying on what is left would retrieve documents about "
                "explaining",
            )

        self._sequence += 1
        query = RetrievalQuery(
            query_id=f"q-{self._sequence}",
            request_id=getattr(request, "request_id", request.purpose),
            text=" ".join(subject),
            wanted_kinds=wanted_kinds,
            maximum_hits=self._maximum_hits,
            minimum_similarity=self._minimum_similarity,
            asked_at_ns=self._now_ns(),
        )
        self.standing.queries_made += 1
        return self._plan(
            request, QUERIED, query, subject, dropped,
            f"{len(subject)} subject term(s) across {len(wanted_kinds)} corpus(es), "
            f"{len(dropped)} task word(s) dropped, floor {self._minimum_similarity:.2f}",
        )

    def _plan(self, request, state, query, subject, dropped, reason) -> QueryPlan:
        return QueryPlan(
            request_id=getattr(request, "request_id", request.purpose),
            state=state, query=query, subject_terms=subject, dropped_task_words=dropped,
            reason=reason, planned_at_ns=self._now_ns(),
        )


def describe_querying(querier: RetrievalQuerier) -> dict:
    return {
        "part_id": PART_ID,
        "requests_seen": querier.standing.requests_seen,
        "queries_made": querier.standing.queries_made,
        "requests_that_needed_no_retrieval": querier.standing.needed_no_retrieval,
        "requests_without_a_subject": querier.standing.without_a_subject,
        "task_words_dropped": querier.standing.task_words_dropped,
        "queries_on_the_raw_request_text": False,
        "issues_a_query_without_a_similarity_floor": False,
    }


def run_retrieval_querier(
    querier: RetrievalQuerier, control_socket, read_requests, publish_queries,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for request, wanted_kinds in read_requests():
            plan = querier.plan(request, wanted_kinds)
            if plan.is_usable:
                publish_queries(plan.query)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
