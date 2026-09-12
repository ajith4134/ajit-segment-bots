"""prompt-template-author: writes the prompts, from what has already been measured.

A prompt is code that this system cannot compile. It changes behaviour, it is
edited by hand, and unlike code it fails silently -- a badly worded instruction
produces confident output rather than an exception. So the authoring of prompts is
treated here as a build step with inputs and constraints, not as writing.

The inputs are the only things a template may be derived from: findings that were
tested, skills that were scored, and the scores of prompt versions that already ran.
That closes the loop -- a prompt that lost on the golden set is evidence about what
to write next, and without it every rewrite starts from opinion.

Five constraints every template must satisfy before it is emitted, each because
violating it produces a specific failure downstream:

- **It declares its output shape.** An unstructured answer cannot be enforced or
  compared, so the enforcer would have nothing to check against.
- **It names the context kinds it needs.** The assembler is not allowed to guess,
  and a prompt that silently needs a fact it was never given produces a fluent
  answer built on nothing.
- **It never asks the model to recall a number.** Numbers come from the snapshot.
  A prompt containing "what is the current funding rate" is asking for a
  hallucination with a decimal point.
- **It never asks for a decision this system makes elsewhere.** A prompt that says
  "should we enter" moves a trading decision into an unversioned, unbacktested
  component.
- **It is derived from something.** A template with no lineage cannot be compared
  against what it replaced.

The author writes templates. It does not activate them: a template becomes a
version, a version is scored, and only the promotion gate makes one live.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request
from runtime.llm_types import PromptTemplate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "prompt-template-author"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-template-author",
    consumes=("research-finding", "skill", "prompt-score", "validated-llm-output", "llm-request"),
    produces=("prompt-template", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WRITTEN = "written"
NO_EVIDENCE_TO_WRITE_FROM = "nothing-measured-to-derive-a-template-from"
NO_OUTPUT_SHAPE = "it-declares-no-output-shape"
NO_CONTEXT_DECLARED = "it-names-no-context-it-needs"
ASKS_THE_MODEL_TO_RECALL = "it-asks-the-model-for-a-number-instead-of-giving-it"
ASKS_FOR_A_TRADING_DECISION = "it-asks-the-model-to-decide-something-this-system-decides"
UNCHANGED = "it-is-identical-to-the-template-it-would-replace"

# Phrasings that ask a model to produce a fact rather than phrase one. Matching is
# on intent words plus a quantity word, because "what is the price" and "estimate
# the funding rate" are the same mistake in different clothes.
RECALL_VERBS = ("what is", "what was", "how much", "estimate the", "recall", "tell me the")
QUANTITY_WORDS = (
    "price", "rate", "volume", "spread", "funding", "volatility", "balance",
    "position", "leverage", "fee", "size",
)

# Words that mean a trading decision. A model may describe, compare and phrase; it
# may not choose.
DECISION_WORDS = (
    "should we enter", "should we exit", "should we buy", "should we sell",
    "decide whether to trade", "pick the position size", "choose the leverage",
    "how much should we risk",
)


@dataclass(frozen=True)
class AuthoredTemplate:
    template: PromptTemplate | None
    state: str
    derived_from: tuple
    violations: tuple
    request: object | None
    reason: str
    written_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == WRITTEN and self.template is not None


@dataclass
class AuthorStanding:
    templates_attempted: int = 0
    templates_written: int = 0
    rejected_no_output_shape: int = 0
    rejected_no_context: int = 0
    rejected_asks_to_recall: int = 0
    rejected_asks_to_decide: int = 0
    rejected_no_evidence: int = 0
    # A purpose's first template, written before any evidence bore on it. Counted
    # rather than folded into templates_written: a template derived from measured
    # findings and one written to get the chain moving are different objects, and
    # a board that showed them as one would hide which is which.
    first_templates_written_before_any_evidence: int = 0
    unchanged_rewrites: int = 0
    requests_made: int = 0


class PromptTemplateAuthor:
    """Writes templates from measured evidence, refusing the five known mistakes."""

    def __init__(self, minimum_evidence: int, now_ns=time.time_ns) -> None:
        if minimum_evidence < 1:
            raise ValueError(
                "a template with no lineage cannot be compared against what it replaced"
            )
        self._minimum_evidence = minimum_evidence
        self._now_ns = now_ns
        self._findings: dict[str, object] = {}
        self._skills: dict[str, object] = {}
        self._scores: dict[str, object] = {}
        self._written: dict[str, PromptTemplate] = {}
        self.standing = AuthorStanding()

    def observe_finding(self, finding) -> None:
        if finding.can_be_acted_on:
            self._findings[finding.finding_id] = finding

    def observe_skill(self, skill) -> None:
        self._skills[skill.skill_id] = skill

    def observe_score(self, score) -> None:
        """What a previous version actually did. The loop closes here."""
        if score.is_usable:
            self._scores[score.version_id] = score

    def evidence_for(self, purpose: str) -> tuple:
        """Everything measured that bears on this purpose, as references."""
        evidence = []
        evidence.extend(
            finding.finding_id
            for finding in self._findings.values()
            if purpose.split("-")[0] in finding.topic or finding.topic in purpose
        )
        evidence.extend(skill.skill_id for skill in self._skills.values())
        evidence.extend(
            version_id
            for version_id, score in self._scores.items()
            if score.purpose == purpose
        )
        return tuple(sorted(set(evidence)))

    def violations_in(self, instruction: str, output_schema, context_kinds) -> tuple:
        found = []
        lowered = instruction.lower()

        if not output_schema:
            found.append(NO_OUTPUT_SHAPE)
        if not context_kinds:
            found.append(NO_CONTEXT_DECLARED)

        for verb in RECALL_VERBS:
            if verb in lowered and any(word in lowered for word in QUANTITY_WORDS):
                found.append(ASKS_THE_MODEL_TO_RECALL)
                break

        if any(phrase in lowered for phrase in DECISION_WORDS):
            found.append(ASKS_FOR_A_TRADING_DECISION)

        return tuple(found)

    def write(
        self, template_id: str, purpose: str, instruction: str, output_schema,
        required_context_kinds, written_by: str = PART_ID,
        is_the_first_for_this_purpose: bool = False,
    ) -> AuthoredTemplate:
        """One template, refusing the five known mistakes.

        `is_the_first_for_this_purpose` exempts a purpose's very first template
        from the evidence bar, and nothing else.

        **The bar's own reason is about rewriting**: "rewriting from opinion is
        how a prompt gets worse in a way nothing detects". That is exactly right
        for a replacement and cannot apply to a template that does not exist --
        there is nothing to make worse, and no amount of waiting produces the
        evidence, because on this system the evidence *is* skills and a skill can
        only be distilled by a model that cannot be called until a template
        exists. Measured 2026-09-07: `prompt_minimum_evidence` is 3, the author
        held 0, and `prompt-renderer` refused all 132 requests it saw while
        `llm-request-router` held 12,154 it could not route.

        A first template is not ungrounded. It declares `required_context_kinds`,
        and the facts it reasons over arrive with each request -- the grounding is
        per call, not prior research. Every other guard still applies to it: it
        must declare an output shape, name the context it needs, not ask the model
        to recall a number, and not ask it to decide something this system
        decides. Only the evidence count is waived, and it is counted separately
        so a board can tell a template written from evidence from one written
        before any existed.
        """
        self.standing.templates_attempted += 1
        evidence = self.evidence_for(purpose)

        if is_the_first_for_this_purpose and len(evidence) < self._minimum_evidence:
            self.standing.first_templates_written_before_any_evidence += 1
        elif len(evidence) < self._minimum_evidence:
            self.standing.rejected_no_evidence += 1
            return self._authored(
                None, NO_EVIDENCE_TO_WRITE_FROM, evidence, (), None,
                f"{len(evidence)} piece(s) of measured evidence bear on {purpose}, below "
                f"the {self._minimum_evidence} needed. Rewriting from opinion is how a "
                f"prompt gets worse in a way nothing detects",
            )

        violations = self.violations_in(
            instruction, output_schema, tuple(required_context_kinds)
        )
        if violations:
            for violation in violations:
                if violation == NO_OUTPUT_SHAPE:
                    self.standing.rejected_no_output_shape += 1
                elif violation == NO_CONTEXT_DECLARED:
                    self.standing.rejected_no_context += 1
                elif violation == ASKS_THE_MODEL_TO_RECALL:
                    self.standing.rejected_asks_to_recall += 1
                elif violation == ASKS_FOR_A_TRADING_DECISION:
                    self.standing.rejected_asks_to_decide += 1
            return self._authored(
                None, violations[0], evidence, violations, None,
                "; ".join(self._explain(violation) for violation in violations),
            )

        existing = self._written.get(template_id)
        if existing is not None and existing.instruction == instruction:
            self.standing.unchanged_rewrites += 1
            return self._authored(
                existing, UNCHANGED, evidence, (), None,
                "identical to the template it would replace. An unchanged rewrite adds a "
                "version whose score differs only by noise",
            )

        template = PromptTemplate(
            template_id=template_id,
            purpose=purpose,
            instruction=instruction,
            required_context_kinds=tuple(required_context_kinds),
            output_schema=dict(output_schema),
            written_at_ns=self._now_ns(),
            written_by=written_by,
            derived_from=evidence,
        )
        self._written[template_id] = template
        self.standing.templates_written += 1
        return self._authored(
            template, WRITTEN, evidence, (), None,
            f"derived from {len(evidence)} measured input(s), declaring "
            f"{len(template.required_context_kinds)} context kind(s) and an output shape. "
            f"It is not live: a template becomes a version, a version is scored, and only "
            f"the promotion gate makes one active",
        )

    def ask_for_a_draft(self, purpose: str, venue_id: str, symbol: str) -> object:
        """A model may draft wording; the constraints above still decide admission."""
        evidence = self.evidence_for(purpose)
        request = make_request(
            purpose=f"draft-a-prompt-for-{purpose}",
            venue_id=venue_id,
            symbol=symbol,
            instruction=(
                "Draft instruction wording only. Do not ask for any number, do not ask "
                "for a trading decision, and name every piece of context the instruction "
                "would need."
            ),
            facts={"measured-inputs": float(len(evidence))},
            maximum_sentences=6,
            now_ns=self._now_ns,
            asked_by=PART_ID,
        )
        self.standing.requests_made += 1
        return request

    @staticmethod
    def _explain(violation: str) -> str:
        return {
            NO_OUTPUT_SHAPE: (
                "it declares no output shape, so the enforcer would have nothing to "
                "check the answer against"
            ),
            NO_CONTEXT_DECLARED: (
                "it names no context, so the assembler would have to guess and a prompt "
                "silently missing a fact still produces a fluent answer"
            ),
            ASKS_THE_MODEL_TO_RECALL: (
                "it asks the model for a number instead of giving it one. That is a "
                "request for a hallucination with a decimal point"
            ),
            ASKS_FOR_A_TRADING_DECISION: (
                "it asks the model to decide something this system decides elsewhere, "
                "moving a trading decision into an unversioned component"
            ),
        }.get(violation, violation)

    def _authored(
        self, template, state, evidence, violations, request, reason,
    ) -> AuthoredTemplate:
        return AuthoredTemplate(
            template=template, state=state, derived_from=evidence, violations=violations,
            request=request, reason=reason, written_at_ns=self._now_ns(),
        )


def describe_template_authoring(author: PromptTemplateAuthor) -> dict:
    return {
        "part_id": PART_ID,
        "templates_attempted": author.standing.templates_attempted,
        "templates_written": author.standing.templates_written,
        "rejected_no_output_shape": author.standing.rejected_no_output_shape,
        "rejected_no_context_declared": author.standing.rejected_no_context,
        "rejected_asks_the_model_to_recall": author.standing.rejected_asks_to_recall,
        "rejected_asks_for_a_trading_decision": author.standing.rejected_asks_to_decide,
        "rejected_no_evidence": author.standing.rejected_no_evidence,
        "first_templates_written_before_any_evidence": (
            author.standing.first_templates_written_before_any_evidence
        ),
        "first_templates_written_before_any_evidence": (
            author.standing.first_templates_written_before_any_evidence
        ),
        "unchanged_rewrites": author.standing.unchanged_rewrites,
        "requests_made": author.standing.requests_made,
        "activates_a_template": False,
    }


def run_prompt_template_author(
    author: PromptTemplateAuthor, control_socket, read_work, publish_templates,
    publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_work(author):
            authored = author.write(**job)
            if authored.request is not None:
                publish_requests(authored.request)
            if authored.is_usable:
                publish_templates(authored.template)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_template_authoring(author),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Findings, skills and scores are the evidence. A template is written for
    a purpose once enough evidence names it: its instruction is the
    evidence's own statements, it declares the verified-facts context, and
    its output shape is the one every purpose here shares -- a venue, a
    symbol and a text. A model's draft arrives as a validated output for
    the drafting purpose; none is configured in phase 1.
    """
    from runtime.input_assembly import Batch

    requests = Batch(read=context.bus.reader("llm-request"))
    findings = Batch(read=context.bus.reader("research-finding"))
    skills = Batch(read=context.bus.reader("skill"))
    scores = Batch(read=context.bus.reader("prompt-score"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_templates = context.bus.publisher_for("prompt-template")
    publish_requests = context.bus.publisher_for("llm-request")
    author = PromptTemplateAuthor(minimum_evidence=int(context.number("prompt_minimum_evidence")))
    purposes_with_evidence: dict[str, int] = {}
    # Every purpose this system has actually been asked to answer, learned from
    # the requests themselves rather than from a list that would go stale the day
    # a twelfth part was added.
    purposes_asked_for: set[str] = set()
    written_for: set[str] = set()
    # **In `structured-output-enforcer`'s own vocabulary**, which is a rule per
    # field and not a type name. Until 2026-09-12 this wrote `{"venue_id": "str"}`
    # and the enforcer does `rule.get("type")`, so every template this part has
    # ever written raised `AttributeError: 'str' object has no attribute 'get'`
    # the moment a real answer reached the enforcer -- which is to say it would
    # have crash-looped the first time this system ever got a reply, and nothing
    # could show it while no reply had ever arrived.
    output_schema = {
        "venue_id": {"type": "string", "required": True},
        "symbol": {"type": "string", "required": True},
        "text": {"type": "string", "required": True},
    }

    def read_work(_author):
        # **What a template is needed FOR comes from what is actually asked**
        # (2026-09-07). This part keyed its purposes off a finding's topic and a
        # skill's title -- free text out of research -- while every request that
        # will ever be made names one of eleven fixed purposes declared as a
        # constant by the part making it (`distil-a-source-into-structure`,
        # `argue-against-this-trade`, ...). Those two sets cannot intersect, so no
        # template this part wrote could ever answer a real request: measured on
        # the live spine that day, `prompt-renderer` refused all 132 requests it
        # saw for `refused_no_active_version` while `llm-request-router` held
        # 12,154 it could not route. Breaking the bootstrap cycle would not have
        # fixed it -- it would have produced a template for a purpose nobody asks.
        #
        # The purpose is on the request. Nothing else here has to change: the
        # evidence, the declared `verified-facts` context and the output schema
        # are the same whichever purpose is being written for.
        for request in requests.payloads():
            purpose = getattr(request, "purpose", "")
            # This part's own drafting requests come back round on the same wire;
            # writing a template for "draft-a-prompt-for-x" would be a prompt for
            # writing the prompt it is already writing.
            if purpose and not purpose.startswith("draft-a-prompt-for-"):
                purposes_asked_for.add(purpose)
        for finding in findings.payloads():
            author.observe_finding(finding)
            purposes_with_evidence[finding.topic] = purposes_with_evidence.get(finding.topic, 0) + 1
        for skill in skills.payloads():
            author.observe_skill(skill)
            purposes_with_evidence[skill.title] = purposes_with_evidence.get(skill.title, 0) + 1
        for score in scores.payloads():
            author.observe_score(score)
        drafts = {}
        for output in outputs.payloads():
            if output.purpose.startswith("draft-a-prompt-for-"):
                drafts[output.purpose[len("draft-a-prompt-for-"):]] = output.text
        jobs = []
        # A purpose somebody asked for, or a purpose evidence named. The first is
        # what makes the chain carry; the second is kept because a topic worth
        # writing about before anything asks is still worth writing about.
        for purpose in (*purposes_asked_for, *purposes_with_evidence):
            if purpose in written_for:
                continue
            evidence = author.evidence_for(purpose)
            # A purpose that has actually been asked for gets its first template
            # whether or not evidence bears on it yet; anything else still waits
            # for evidence, because a template nobody asked for and nothing
            # measured is decoration.
            if not evidence and purpose not in purposes_asked_for:
                continue
            written_for.add(purpose)
            instruction = drafts.get(purpose) or (
                "Using only the measured facts given, state what they show about "
                + purpose + "."
                + (" " + " ".join(str(item) for item in evidence) if evidence else "")
            )
            jobs.append({
                "template_id": f"template:{purpose}", "purpose": purpose, "instruction": instruction,
                "output_schema": dict(output_schema), "required_context_kinds": ("verified-facts",),
                "is_the_first_for_this_purpose": True,
            })
        return tuple(jobs)

    return run_prompt_template_author(
        author=author,
        control_socket=context.control_socket,
        read_work=read_work,
        publish_templates=lambda template: publish_templates((template,)),
        publish_requests=lambda request: publish_requests((request,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
