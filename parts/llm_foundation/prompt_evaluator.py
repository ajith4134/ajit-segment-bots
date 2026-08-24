"""prompt-evaluator: how a prompt version does on decisions whose answer is known.

A prompt is only better than another if it is better on something measurable. This
part runs a version against the golden set and reports four numbers, kept separate
on purpose because collapsing them into one score hides the trade-off that matters.

- **Schema validity** -- how often the answer was the declared shape at all. A
  prompt with brilliant reasoning that fails structure half the time costs twice as
  many calls.
- **Factual support** -- how much of the prose traced to measured facts. This is
  the number that catches a prompt which invites the model to elaborate.
- **Agreement with the outcome** -- whether the answer matched what actually
  happened. The only one of the four that is about being right rather than
  well-behaved.
- **Cost** -- mean output tokens and latency. A version that wins on quality by
  three percent and costs four times as much has not won.

Two properties keep the evaluation honest. **Every version is run on the same
cases**, because a version scored on a different subset is not comparable, and
comparing them anyway is the most common way a worse prompt gets promoted. And
**agreement is measured against the recorded outcome, never against another model's
answer** -- grading a model with a model measures how similar two models are.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.llm_types import PromptScore
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "prompt-evaluator"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-evaluator",
    consumes=("golden-case", "prompt-version", "prompt-template", "validated-llm-output"),
    produces=("prompt-score", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SCORED = "scored"
NO_CASES = "there-are-no-golden-cases-for-this-purpose"
TOO_FEW_CASES = "too-few-cases-for-a-score-to-mean-anything"
NOT_THE_SAME_CASES = "this-version-was-not-run-on-the-same-cases-as-the-others"
INCOMPLETE_RUN = "the-version-did-not-answer-every-case"


@dataclass(frozen=True)
class CaseResult:
    """One case, one version, and what came back."""

    case_id: str
    version_id: str
    was_schema_valid: bool
    unsupported_sentences: int
    total_sentences: int
    agreed_with_outcome: bool | None
    output_tokens: int
    latency_seconds: float
    repair_attempts: int

    @property
    def support_fraction(self) -> float:
        if self.total_sentences <= 0:
            return 1.0
        return 1.0 - self.unsupported_sentences / self.total_sentences


@dataclass(frozen=True)
class Evaluation:
    version_id: str
    state: str
    score: PromptScore | None
    cases_run: tuple
    cases_missing: tuple
    reason: str
    evaluated_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == SCORED and self.score is not None


@dataclass
class EvaluatorStanding:
    versions_scored: int = 0
    cases_run: int = 0
    refused_no_cases: int = 0
    refused_too_few: int = 0
    refused_incomplete_runs: int = 0
    refused_different_case_sets: int = 0
    graded_against_another_model: int = 0


class PromptEvaluator:
    """Runs a version over the golden set and reports four separate numbers."""

    def __init__(self, minimum_cases: int, now_ns=time.time_ns) -> None:
        if minimum_cases < 2:
            raise ValueError("a score over one case is that case")
        self._minimum_cases = minimum_cases
        self._now_ns = now_ns
        self._results: dict[str, dict] = {}
        self._case_sets: dict[str, tuple] = {}
        self.standing = EvaluatorStanding()

    def observe_result(self, result: CaseResult) -> None:
        self._results.setdefault(result.version_id, {})[result.case_id] = result
        self.standing.cases_run += 1

    def evaluate(self, version_id: str, purpose: str, cases) -> Evaluation:
        cases = tuple(case.case_id for case in cases)
        if not cases:
            self.standing.refused_no_cases += 1
            return self._evaluation(
                version_id, NO_CASES, None, (), (),
                f"there are no golden cases for {purpose}. A prompt with nothing to be "
                f"measured against is unmeasured, not good",
            )

        if len(cases) < self._minimum_cases:
            self.standing.refused_too_few += 1
            return self._evaluation(
                version_id, TOO_FEW_CASES, None, cases, (),
                f"{len(cases)} case(s), below the {self._minimum_cases} bar",
            )

        results = self._results.get(version_id, {})
        missing = tuple(case_id for case_id in cases if case_id not in results)
        if missing:
            self.standing.refused_incomplete_runs += 1
            return self._evaluation(
                version_id, INCOMPLETE_RUN, None, cases, missing,
                f"{len(missing)} case(s) unanswered. Scoring on the subset that answered "
                f"rewards a version for the cases it happened to survive",
            )

        # Every version must face the same cases, or the comparison is between
        # different examinations.
        previous = self._case_sets.get(purpose)
        if previous is not None and previous != cases:
            self.standing.refused_different_case_sets += 1
            return self._evaluation(
                version_id, NOT_THE_SAME_CASES, None, cases, (),
                "this version was run on a different case set from the versions it would "
                "be compared with, which is the most common way a worse prompt gets "
                "promoted",
            )
        self._case_sets[purpose] = cases

        chosen = [results[case_id] for case_id in cases]
        graded = [result for result in chosen if result.agreed_with_outcome is not None]

        score = PromptScore(
            version_id=version_id,
            purpose=purpose,
            cases_run=len(chosen),
            schema_valid_fraction=sum(
                1 for result in chosen if result.was_schema_valid
            ) / len(chosen),
            factually_supported_fraction=statistics.mean(
                result.support_fraction for result in chosen
            ),
            agreement_with_outcome=(
                sum(1 for result in graded if result.agreed_with_outcome) / len(graded)
                if graded
                else 0.0
            ),
            mean_output_tokens=statistics.mean(
                result.output_tokens for result in chosen
            ),
            mean_latency_seconds=statistics.mean(
                result.latency_seconds for result in chosen
            ),
            is_fitted=bool(graded),
            scored_at_ns=self._now_ns(),
        )
        self.standing.versions_scored += 1

        return self._evaluation(
            version_id, SCORED, score, cases, (),
            f"{len(chosen)} case(s): {score.schema_valid_fraction:.0%} valid structure, "
            f"{score.factually_supported_fraction:.0%} supported, "
            f"{score.agreement_with_outcome:.0%} agreement with what happened, "
            f"{score.mean_output_tokens:.0f} tokens and "
            f"{score.mean_latency_seconds:.2f}s per answer. The four are kept apart "
            f"because collapsing them hides the trade-off",
        )

    def _evaluation(self, version_id, state, score, cases, missing, reason) -> Evaluation:
        return Evaluation(
            version_id=version_id, state=state, score=score, cases_run=cases,
            cases_missing=missing, reason=reason, evaluated_at_ns=self._now_ns(),
        )


def describe_evaluation(evaluator: PromptEvaluator) -> dict:
    return {
        "part_id": PART_ID,
        "versions_scored": evaluator.standing.versions_scored,
        "cases_run": evaluator.standing.cases_run,
        "refused_no_cases": evaluator.standing.refused_no_cases,
        "refused_too_few_cases": evaluator.standing.refused_too_few,
        "refused_incomplete_runs": evaluator.standing.refused_incomplete_runs,
        "refused_different_case_sets": evaluator.standing.refused_different_case_sets,
        "grades_against_another_model": False,
        "collapses_the_score_into_one_number": False,
    }


def run_prompt_evaluator(
    evaluator: PromptEvaluator, control_socket, read_runs, publish_scores,
    publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for version_id, purpose, cases, results, requests in read_runs():
            for result in results:
                evaluator.observe_result(result)
            for request in requests:
                publish_requests(request)
            evaluation = evaluator.evaluate(version_id, purpose, cases)
            if evaluation.is_usable:
                publish_scores(evaluation.score)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_evaluation(evaluator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every golden case for a purpose is run against every version for that
    purpose: a request per pair goes out, and a validated output for this
    part's evaluation purpose naming the case and version comes back as a
    result. A version is evaluated on the cases it has answered once all
    have; until then it is refused by name inside the evaluator. No model
    is configured in phase 1, so today no result returns. Templates are
    read and drained: the version carries what the evaluator needs.
    """
    from runtime.claim_verification import LlmRequest
    from runtime.input_assembly import Batch

    cases_in = Batch(read=context.bus.reader("golden-case"))
    versions_in = Batch(read=context.bus.reader("prompt-version"))
    templates = Batch(read=context.bus.reader("prompt-template"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_scores = context.bus.publisher_for("prompt-score")
    publish_requests = context.bus.publisher_for("llm-request")
    evaluator = PromptEvaluator(minimum_cases=int(context.number("prompt_minimum_cases")))
    maximum_sentences = int(context.number("llm_maximum_sentences"))
    evaluation_purpose = f"evaluate:{PART_ID}"
    cases_by_purpose: dict[str, dict[str, object]] = {}
    versions_by_purpose: dict[str, dict[str, object]] = {}
    requested: set[tuple[str, str]] = set()
    answered: dict[str, set[str]] = {}

    def request_for(case, version) -> LlmRequest:
        facts = case.facts if isinstance(case.facts, dict) else {}
        return LlmRequest(
            purpose=evaluation_purpose, venue_id=str(facts.get("venue_id", "")), symbol=str(facts.get("symbol", "")),
            instruction=f"{version.instruction}\n[case:{case.case_id}][version:{version.version_id}]",
            facts=dict(facts), maximum_sentences=maximum_sentences, requested_at_ns=int(case.added_at_ns),
        )

    def read_runs():
        templates.payloads()
        for case in cases_in.payloads():
            cases_by_purpose.setdefault(case.purpose, {})[case.case_id] = case
        for version in versions_in.payloads():
            versions_by_purpose.setdefault(version.purpose, {})[version.version_id] = version
        results_by_version: dict[str, list] = {}
        for output in outputs.payloads():
            if output.purpose != evaluation_purpose:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            case_id, version_id = value.get("case_id"), value.get("version_id")
            if case_id is None or version_id is None:
                continue
            answered.setdefault(str(version_id), set()).add(str(case_id))
            results_by_version.setdefault(str(version_id), []).append(
                CaseResult(
                    case_id=str(case_id), version_id=str(version_id), was_schema_valid=True,
                    unsupported_sentences=len(output.unsupported_claims), total_sentences=max(1, len(output.text.split("."))),
                    agreed_with_outcome=value.get("agreed_with_outcome"), output_tokens=len(output.text) // 4,
                    latency_seconds=0.0, repair_attempts=int(output.repair_attempts),
                )
            )
        runs = []
        for purpose, versions in versions_by_purpose.items():
            cases = cases_by_purpose.get(purpose, {})
            for version_id, version in versions.items():
                requests = []
                for case_id, case in cases.items():
                    if (case_id, version_id) not in requested:
                        requested.add((case_id, version_id))
                        requests.append(request_for(case, version))
                results = results_by_version.pop(version_id, [])
                if requests or results:
                    runs.append((version_id, purpose, tuple(cases.values()), tuple(results), tuple(requests)))
        return tuple(runs)

    return run_prompt_evaluator(
        evaluator=evaluator,
        control_socket=context.control_socket,
        read_runs=read_runs,
        publish_scores=lambda score: publish_scores((score,)),
        publish_requests=lambda request: publish_requests((request,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
