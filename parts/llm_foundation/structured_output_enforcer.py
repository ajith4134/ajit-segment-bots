"""structured-output-enforcer: nothing a model wrote gets in without passing here.

This is the boundary. On one side is generated text, which is fluent, confident and
occasionally invented; on the other is data this system acts on. Everything crosses
here or does not cross.

Two checks, and they are not the same check:

- **Structure.** The answer must match the shape the prompt version declared:
  required fields present, types right, enumerated values from the declared set,
  numbers inside declared bounds. A missing field is not filled with a default --
  a default is a value nobody measured wearing the appearance of an answer.
- **Support.** Every number in the prose must trace to a fact the request carried.
  A sentence whose numbers do not is removed, and what was removed is reported, so
  a caller sees that the model asserted something unsupported rather than seeing a
  quietly shorter answer.

Repair is bounded and counted. One malformed answer is a bad sample; the same prompt
producing malformed answers repeatedly is a prompt problem, and a part that retries
without limit converts that into an unbounded bill. So retries are capped, the cap is
a setting, and the retry count travels with the output -- an answer that took three
attempts is evidence about the prompt version, and the evaluator reads it.

A truncated answer is treated as failed even when it parses. `finish_reason` of
length means the model stopped mid-thought, and a JSON object that happens to close
before the truncation is the most dangerous shape this part sees.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.llm_types import LlmAnswerVerdict, ValidatedLlmOutput
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "structured-output-enforcer"

PART_DECLARATION = PartDeclaration(
    part_id="structured-output-enforcer",
    consumes=("llm-response", "prompt-version", "rendered-llm-request"),
    produces=("validated-llm-output", "llm-request", "part-health", "llm-answer-verdict"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

VALIDATED = "validated"
NOT_JSON = "the-answer-is-not-the-declared-structure"
MISSING_FIELD = "a-required-field-is-absent"
WRONG_TYPE = "a-field-has-the-wrong-type"
OUT_OF_BOUNDS = "a-number-is-outside-its-declared-bounds"
NOT_IN_THE_SET = "a-value-is-not-one-of-the-declared-options"
WAS_TRUNCATED = "the-model-stopped-mid-answer"
REPAIRS_EXHAUSTED = "the-prompt-keeps-producing-malformed-answers"
NOTHING_SUPPORTED = "every-sentence-asserted-something-unmeasured"


@dataclass(frozen=True)
class EnforcementOutcome:
    response_id: str
    state: str
    output: ValidatedLlmOutput | None
    failures: tuple
    repair_attempts: int
    retry_request: object | None
    reason: str
    enforced_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == VALIDATED and self.output is not None


@dataclass
class EnforcerStanding:
    responses_seen: int = 0
    validated: int = 0
    rejected_not_json: int = 0
    answers_arriving_in_a_code_fence: int = 0
    rejected_missing_field: int = 0
    rejected_wrong_type: int = 0
    rejected_out_of_bounds: int = 0
    rejected_not_in_set: int = 0
    rejected_truncated: int = 0
    repairs_requested: int = 0
    repairs_exhausted: int = 0
    sentences_removed: int = 0
    defaults_filled_in: int = 0
    verdicts_published: int = 0
    # A rejected answer that could not be repaired because the rendered request
    # it answers was not known -- no asker to budget a repair, no facts to ground it.
    repairs_not_possible: int = 0
    # A response whose rendered request -- and so whose facts -- never arrived
    # inside the hold. Dropped unjudged rather than judged against nothing: a
    # verdict against empty facts rejects every answer, which is what this part
    # did on the spine until 2026-09-13.
    responses_dropped_without_their_facts: int = 0


def verdict_of(outcome: EnforcementOutcome, response, version, now_ns=time.time_ns) -> LlmAnswerVerdict:
    """What `llm-model-picker` learns from: was this model's answer on this purpose usable.

    Published for every outcome, rejections included -- a picker that only heard
    about the answers that passed would learn every model is perfect.
    """
    return LlmAnswerVerdict(
        response_id=response.response_id,
        rendered_id=response.rendered_id,
        version_id=response.version_id,
        purpose=version.purpose,
        model_id=response.model_id,
        was_usable=outcome.is_usable,
        state=outcome.state,
        judged_at_ns=now_ns(),
    )


def json_text_of(answer: str) -> tuple[str, bool]:
    """The JSON in an answer, and whether it arrived inside a code fence.

    A model asked for JSON returns JSON inside ```json more often than not. The
    fence is not part of the answer and stripping it is not leniency about the
    structure -- everything inside is still checked field by field. Refusing the
    fence outright was refusing correct answers: the first real call this project
    ever made returned exactly the declared object, fenced, and was rejected as
    "not the declared structure".
    """
    text = (answer or "").strip()
    if not text.startswith("```"):
        return text, False
    without_opening = text.split("\n", 1)[1] if "\n" in text else ""
    closing = without_opening.rfind("```")
    inside = without_opening[:closing] if closing != -1 else without_opening
    return inside.strip(), True


class StructuredOutputEnforcer:
    """Checks shape and support, repairs a bounded number of times, defaults never."""

    def __init__(
        self,
        maximum_repairs: int,
        relative_tolerance: float,
        require_a_citation: bool,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_repairs < 0:
            raise ValueError("a negative repair budget is not a budget")
        self._maximum_repairs = maximum_repairs
        self._relative_tolerance = relative_tolerance
        self._require_a_citation = require_a_citation
        self._now_ns = now_ns
        self._sequence = 0
        self.standing = EnforcerStanding()

    def check_structure(self, value, schema) -> tuple:
        """Every way the answer fails the declared shape, not just the first."""
        failures = []
        for name, rule in schema.items():
            if name not in value:
                if rule.get("required", True):
                    failures.append((MISSING_FIELD, name))
                continue
            found = value[name]
            expected = rule.get("type")
            if expected == "number":
                if isinstance(found, bool) or not isinstance(found, (int, float)):
                    failures.append((WRONG_TYPE, name))
                    continue
                low, high = rule.get("minimum"), rule.get("maximum")
                if (low is not None and found < low) or (high is not None and found > high):
                    failures.append((OUT_OF_BOUNDS, name))
            elif expected == "string":
                if not isinstance(found, str):
                    failures.append((WRONG_TYPE, name))
                    continue
                options = rule.get("one_of")
                if options is not None and found not in options:
                    failures.append((NOT_IN_THE_SET, name))
            elif expected == "boolean":
                if not isinstance(found, bool):
                    failures.append((WRONG_TYPE, name))
            elif expected == "array":
                if not isinstance(found, (list, tuple)):
                    failures.append((WRONG_TYPE, name))
        return tuple(failures)

    def enforce(self, response, version, facts, rendered=None) -> EnforcementOutcome:
        """Judge one response against the version and facts its prompt was rendered with.

        `rendered` is the `RenderedLlmRequest` the response answers. Its
        `repair_attempt` is how many repairs the chain has already spent, which is
        what the bound is counted against: every repair is a new render with a new
        `rendered_id`, so until 2026-09-13, when attempts were counted per
        `rendered_id`, the bound was never reached. Without it no repair can be
        built -- a repair has to be the same question from the same asker -- and a
        rejection is final.
        """
        self.standing.responses_seen += 1
        attempts = int(getattr(rendered, "repair_attempt", 0) or 0)

        if response.was_cut_off:
            self.standing.rejected_truncated += 1
            return self._reject(
                response, WAS_TRUNCATED, ((WAS_TRUNCATED, "finish_reason"),), attempts,
                version, facts, rendered,
                "the model stopped mid-answer. A structure that happens to close before "
                "the truncation is the most dangerous shape this part sees, so a cut-off "
                "answer fails even when it parses",
            )

        text, was_fenced = json_text_of(response.text)
        if was_fenced:
            # Counted, not ignored. Every model wraps JSON in a fence when asked
            # for JSON, and a parser that refuses a fence refuses nearly every
            # real answer -- measured on the first real call this project ever
            # made, 2026-09-12, whose answer was a correct object inside
            # ```json. Tolerating it silently would be the other mistake: this
            # number says how often the prompt's "no code fence" is ignored.
            self.standing.answers_arriving_in_a_code_fence += 1
        try:
            value = json.loads(text) if text.startswith(("{", "[")) else None
        except json.JSONDecodeError:
            value = None

        if not isinstance(value, dict):
            self.standing.rejected_not_json += 1
            return self._reject(
                response, NOT_JSON, ((NOT_JSON, "root"),), attempts, version, facts, rendered,
                "the answer is not the declared structure",
            )

        failures = self.check_structure(value, version.output_schema)
        if failures:
            for kind, _ in failures:
                if kind == MISSING_FIELD:
                    self.standing.rejected_missing_field += 1
                elif kind == WRONG_TYPE:
                    self.standing.rejected_wrong_type += 1
                elif kind == OUT_OF_BOUNDS:
                    self.standing.rejected_out_of_bounds += 1
                elif kind == NOT_IN_THE_SET:
                    self.standing.rejected_not_in_set += 1
            return self._reject(
                response, failures[0][0], failures, attempts, version, facts, rendered,
                "; ".join(f"{kind} ({name})" for kind, name in failures)
                + ". No field is filled with a default: a default is a value nobody "
                  "measured wearing the appearance of an answer",
            )

        prose = value.get("text") if isinstance(value.get("text"), str) else response.text
        verified = verify_against_facts(
            prose, facts, relative_tolerance=self._relative_tolerance,
            require_a_citation=self._require_a_citation,
        )
        self.standing.sentences_removed += len(verified.removed_sentences)

        if verified.removed_sentences and not verified.kept_sentences:
            return self._reject(
                response, NOTHING_SUPPORTED,
                ((NOTHING_SUPPORTED, "text"),), attempts, version, facts, rendered,
                f"all {len(verified.removed_sentences)} sentence(s) asserted numbers that "
                f"trace to no measurement",
            )

        self._sequence += 1
        output = ValidatedLlmOutput(
            output_id=f"v-{self._sequence}",
            response_id=response.response_id,
            version_id=version.version_id,
            purpose=version.purpose,
            value=value,
            text=verified.text,
            removed_sentences=verified.removed_sentences,
            unsupported_claims=verified.unsupported_claims,
            repair_attempts=attempts,
            validated_at_ns=self._now_ns(),
        )
        self.standing.validated += 1

        return EnforcementOutcome(
            response_id=response.response_id, state=VALIDATED, output=output, failures=(),
            repair_attempts=attempts, retry_request=None,
            reason=(
                f"structure valid against {version.version_id}"
                + (
                    f", {len(verified.removed_sentences)} unsupported sentence(s) removed "
                    f"and named"
                    if verified.removed_sentences
                    else ", every sentence traced to a measured fact"
                )
                + (
                    f", after {attempts} repair attempt(s) -- which is evidence about the "
                    f"prompt version, not just about this answer"
                    if attempts
                    else ""
                )
            ),
            enforced_at_ns=self._now_ns(),
        )

    def _reject(
        self, response, state, failures, attempts, version, facts, rendered, reason,
    ) -> EnforcementOutcome:
        if attempts >= self._maximum_repairs:
            self.standing.repairs_exhausted += 1
            return EnforcementOutcome(
                response_id=response.response_id, state=REPAIRS_EXHAUSTED, output=None,
                failures=failures, repair_attempts=attempts, retry_request=None,
                reason=(
                    f"{reason}. {attempts} repair(s) already spent, at the limit. Retrying "
                    f"without bound converts a prompt problem into an unbounded bill"
                ),
                enforced_at_ns=self._now_ns(),
            )

        if rendered is None or not facts:
            # A repair is the same question from the same asker; without the
            # rendered request there is no asker to budget it and no facts to
            # ground it, and a request on `llm-request` that is not an
            # `LlmRequest` crashes the renderer and the picker.
            self.standing.repairs_not_possible += 1
            return EnforcementOutcome(
                response_id=response.response_id, state=state, output=None, failures=failures,
                repair_attempts=attempts, retry_request=None,
                reason=f"{reason}. Not repaired: the rendered request it answers is not known here",
                enforced_at_ns=self._now_ns(),
            )

        self.standing.repairs_requested += 1
        # An `LlmRequest`, the type every reader of `llm-request` reads -- until
        # 2026-09-13 this was a plain dict, and the first rejected answer on the
        # spine would have crashed `prompt-renderer` and `llm-model-picker`.
        retry = make_request(
            purpose=version.purpose,
            venue_id=rendered.venue_id,
            symbol=rendered.symbol,
            instruction=version.instruction,
            facts=dict(facts),
            maximum_sentences=max(int(rendered.maximum_sentences), 1),
            now_ns=self._now_ns,
            asked_by=rendered.asked_by,
            repair_of=rendered.repair_of or rendered.rendered_id,
            repair_attempt=attempts + 1,
            previous_failures=(
                "It failed these checks: "
                + "; ".join(f"{kind} ({name})" for kind, name in failures)
                + ". Return only the declared structure, using only the measured facts."
            ),
        )
        return EnforcementOutcome(
            response_id=response.response_id, state=state, output=None, failures=failures,
            repair_attempts=attempts + 1, retry_request=retry, reason=reason,
            enforced_at_ns=self._now_ns(),
        )


def describe_enforcement(enforcer: StructuredOutputEnforcer) -> dict:
    return {
        "part_id": PART_ID,
        "responses_seen": enforcer.standing.responses_seen,
        "validated": enforcer.standing.validated,
        "rejected_not_the_declared_structure": enforcer.standing.rejected_not_json,
        "answers_arriving_in_a_code_fence": (
            enforcer.standing.answers_arriving_in_a_code_fence
        ),
        "rejected_missing_field": enforcer.standing.rejected_missing_field,
        "rejected_wrong_type": enforcer.standing.rejected_wrong_type,
        "rejected_out_of_bounds": enforcer.standing.rejected_out_of_bounds,
        "rejected_value_not_in_the_declared_set": enforcer.standing.rejected_not_in_set,
        "rejected_truncated": enforcer.standing.rejected_truncated,
        "repairs_requested": enforcer.standing.repairs_requested,
        "repairs_exhausted": enforcer.standing.repairs_exhausted,
        "sentences_removed_as_unsupported": enforcer.standing.sentences_removed,
        "fills_missing_fields_with_defaults": False,
        "defaults_filled_in": enforcer.standing.defaults_filled_in,
        "verdicts_published": enforcer.standing.verdicts_published,
        "repairs_not_possible": enforcer.standing.repairs_not_possible,
        "responses_dropped_without_their_facts": (
            enforcer.standing.responses_dropped_without_their_facts
        ),
    }


def run_structured_output_enforcer(
    enforcer: StructuredOutputEnforcer, control_socket, read_responses, publish_outputs,
    publish_retries, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    publish_verdicts=None,
) -> int:
    def tick() -> None:
        for job in read_responses():
            response, version, source = job
            # `source` is the rendered request when the live part supplies it, or a
            # plain facts dict from a caller that has only the facts.
            if isinstance(source, dict):
                outcome = enforcer.enforce(response, version, source)
            else:
                outcome = enforcer.enforce(response, version, dict(source.facts), rendered=source)
            if publish_verdicts is not None:
                publish_verdicts(verdict_of(outcome, response, version))
                enforcer.standing.verdicts_published += 1
            if outcome.retry_request is not None:
                publish_retries(outcome.retry_request)
            if outcome.is_usable:
                publish_outputs(outcome.output)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_enforcement(enforcer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A response is enforced against the version that rendered it and the
    facts its rendered request carried, matched by `rendered_id`.

    **Until 2026-09-13 the rendered request was not on this part's inputs**
    and every response was enforced against `{}`, with `require_a_citation`
    on -- so every real answer on the spine would have been rejected, and
    once `llm-model-picker` learned from these verdicts it would have learned
    that every model is bad at every purpose. A response whose version or
    rendered request has not arrived is held; one still unmatched after
    `llm_enforcer_rendered_request_hold_seconds` is dropped and counted rather
    than judged against nothing.
    """
    from runtime.input_assembly import Batch

    responses = Batch(read=context.bus.reader("llm-response"))
    versions = Batch(read=context.bus.reader("prompt-version"))
    rendered = Batch(read=context.bus.reader("rendered-llm-request"))
    publish_outputs = context.bus.publisher_for("validated-llm-output")
    publish_verdicts = context.bus.publisher_for("llm-answer-verdict")
    publish_retries = context.bus.publisher_for("llm-request")
    hold_ns = int(context.number("llm_enforcer_rendered_request_hold_seconds") * 1_000_000_000)
    enforcer = StructuredOutputEnforcer(
        maximum_repairs=int(context.number("llm_maximum_repairs")),
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        require_a_citation=True,
    )
    version_by_id: dict[str, object] = {}
    # rendered_id -> (rendered request, seen_at_ns). Aged out, because most rendered
    # requests are refused by the router and never produce a response to
    # match -- without the bound this map is every refusal, forever.
    facts_by_rendered_id: dict[str, tuple[dict, int]] = {}
    held: list = []

    def read_responses():
        now = time.time_ns()
        for version in versions.payloads():
            version_by_id[version.version_id] = version
        for request in rendered.payloads():
            facts_by_rendered_id[request.rendered_id] = (request, now)
        for rendered_id in [
            key for key, (_, seen_at) in facts_by_rendered_id.items() if now - seen_at > hold_ns
        ]:
            del facts_by_rendered_id[rendered_id]
        held.extend((response, now) for response in responses.payloads())
        jobs, still_held = [], []
        for response, arrived_at in held:
            version = version_by_id.get(response.version_id)
            known = facts_by_rendered_id.get(response.rendered_id)
            if version is None or known is None:
                if now - arrived_at > hold_ns:
                    enforcer.standing.responses_dropped_without_their_facts += 1
                else:
                    still_held.append((response, arrived_at))
                continue
            jobs.append((response, version, known[0]))
        held[:] = still_held
        return tuple(jobs)

    return run_structured_output_enforcer(
        enforcer=enforcer,
        control_socket=context.control_socket,
        read_responses=read_responses,
        publish_outputs=lambda output: publish_outputs((output,)),
        publish_retries=lambda request: publish_retries((request,)),
        publish_verdicts=lambda verdict: publish_verdicts((verdict,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
