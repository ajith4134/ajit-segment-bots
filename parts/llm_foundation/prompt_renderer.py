"""prompt-renderer: the exact bytes that will be sent, frozen and fingerprinted.

Between a request and a call there is a step where the versioned instruction, the
assembled context and the request's own facts become one piece of text. Doing that
implicitly -- building the string inside the caller -- destroys two things: the
ability to say afterwards what was actually sent, and the ability to know that two
calls were identical.

This part makes the rendered text an object. It carries the version that produced
it, the context it used, the facts its answer will be checked against, and a
fingerprint over all of it. The fingerprint is what makes a response cache safe:
two requests are the same call only if every input is the same, and "same purpose
and same symbol" is not that.

Three refusals:

- **A version whose declared context kinds are not present is not rendered.** The
  version says what it needs; supplying less produces an answer built on an absence,
  which reads exactly like an answer built on evidence.
- **Facts are never interpolated into free prose.** They are rendered as a labelled
  block, because a number inside a sentence cannot be matched back reliably, and
  matching back is how the enforcer checks the answer.
- **Nothing is rendered without an output schema.** The enforcer needs a shape, and
  a rendered request that cannot be enforced is a request for unchecked text.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from runtime.llm_types import RenderedLlmRequest
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "prompt-renderer"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-renderer",
    consumes=("llm-request", "prompt-version", "prompt-context"),
    produces=("rendered-llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RENDERED = "rendered"
NO_ACTIVE_VERSION = "no-active-prompt-version-for-this-purpose"
MISSING_CONTEXT = "the-version-declares-context-that-was-not-supplied"
NO_OUTPUT_SCHEMA = "the-version-declares-no-output-shape"
NO_FACTS = "the-request-carries-no-facts-to-check-the-answer-against"

FACTS_HEADING = "MEASURED FACTS (the only numbers you may use)"
CONTEXT_HEADING = "RETRIEVED MATERIAL (may be wrong; the facts above are not)"
INSTRUCTION_HEADING = "INSTRUCTION"


@dataclass(frozen=True)
class RenderOutcome:
    request_id: str
    state: str
    rendered: RenderedLlmRequest | None
    missing_context_kinds: tuple
    reason: str
    rendered_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == RENDERED and self.rendered is not None


@dataclass
class RendererStanding:
    requests_seen: int = 0
    rendered: int = 0
    refused_no_version: int = 0
    refused_missing_context: int = 0
    refused_no_schema: int = 0
    refused_no_facts: int = 0
    identical_fingerprints: int = 0


class PromptRenderer:
    """Freezes version, context and facts into one fingerprinted piece of text."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._sequence = 0
        self._fingerprints: dict[str, int] = {}
        self.standing = RendererStanding()

    def render(self, request, version, context) -> RenderOutcome:
        self.standing.requests_seen += 1
        request_id = getattr(request, "request_id", request.purpose)

        if version is None:
            self.standing.refused_no_version += 1
            return self._outcome(
                request_id, NO_ACTIVE_VERSION, None, (),
                f"no active prompt version for {request.purpose}. Rendering against an "
                f"unversioned instruction makes the answer untraceable",
            )

        if not version.output_schema:
            self.standing.refused_no_schema += 1
            return self._outcome(
                request_id, NO_OUTPUT_SCHEMA, None, (),
                "the version declares no output shape, so the answer could not be "
                "enforced. A rendered request that cannot be enforced is a request for "
                "unchecked text",
            )

        if not request.facts:
            self.standing.refused_no_facts += 1
            return self._outcome(
                request_id, NO_FACTS, None, (),
                "the request carries no facts. Every number in the answer is matched back "
                "to these, and with none there is nothing to match against",
            )

        supplied = {kind for kind, _, _ in (context.sections if context else ())}
        missing = tuple(
            sorted(set(version.required_context_kinds) - supplied)
        )
        if missing:
            self.standing.refused_missing_context += 1
            return self._outcome(
                request_id, MISSING_CONTEXT, None, missing,
                f"the version declares {', '.join(missing)} and it was not supplied. An "
                f"answer built on an absence reads exactly like one built on evidence",
            )

        text = self._render_text(request, version, context)
        fingerprint = self._fingerprint(version, request.facts, context, text)
        if fingerprint in self._fingerprints:
            self.standing.identical_fingerprints += 1
        self._fingerprints[fingerprint] = self._fingerprints.get(fingerprint, 0) + 1

        self._sequence += 1
        rendered = RenderedLlmRequest(
            rendered_id=f"r-{self._sequence}",
            request_id=request_id,
            version_id=version.version_id,
            purpose=version.purpose,
            text=text,
            output_schema=dict(version.output_schema),
            facts=dict(request.facts),
            context_id=context.context_id if context else None,
            characters=len(text),
            fingerprint=fingerprint,
            rendered_at_ns=self._now_ns(),
        )
        self.standing.rendered += 1
        return self._outcome(
            request_id, RENDERED, rendered, (),
            f"{len(text)} character(s) under {version.version_id}, fingerprinted over the "
            f"version, the facts and the context. Two calls are the same call only if all "
            f"of that matches",
        )

    def times_seen(self, fingerprint: str) -> int:
        return self._fingerprints.get(fingerprint, 0)

    @staticmethod
    def _render_text(request, version, context) -> str:
        blocks = [f"{INSTRUCTION_HEADING}\n{version.instruction}"]
        # Facts as a labelled block, never interpolated into prose: a number inside
        # a sentence cannot be matched back reliably.
        facts = "\n".join(f"{name} = {value}" for name, value in sorted(request.facts.items()))
        blocks.append(f"{FACTS_HEADING}\n{facts}")
        if context and context.sections:
            retrieved = "\n\n".join(
                f"[{label}]\n{body}"
                for kind, label, body in context.sections
                if kind != "verified-facts"
            )
            if retrieved:
                blocks.append(f"{CONTEXT_HEADING}\n{retrieved}")
        return "\n\n".join(blocks)

    @staticmethod
    def _fingerprint(version, facts, context, text) -> str:
        payload = json.dumps(
            {
                "version": version.version_id,
                "facts": {name: str(value) for name, value in sorted(facts.items())},
                "context": context.context_id if context else None,
                "text": text,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _outcome(self, request_id, state, rendered, missing, reason) -> RenderOutcome:
        return RenderOutcome(
            request_id=request_id, state=state, rendered=rendered,
            missing_context_kinds=missing, reason=reason, rendered_at_ns=self._now_ns(),
        )


def describe_rendering(renderer: PromptRenderer) -> dict:
    return {
        "part_id": PART_ID,
        "requests_seen": renderer.standing.requests_seen,
        "rendered": renderer.standing.rendered,
        "refused_no_active_version": renderer.standing.refused_no_version,
        "refused_missing_context": renderer.standing.refused_missing_context,
        "refused_no_output_schema": renderer.standing.refused_no_schema,
        "refused_no_facts": renderer.standing.refused_no_facts,
        "identical_fingerprints_seen": renderer.standing.identical_fingerprints,
        "interpolates_facts_into_prose": False,
    }


def run_prompt_renderer(
    renderer: PromptRenderer, control_socket, read_jobs, publish_rendered,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for request, version, context in read_jobs():
            outcome = renderer.render(request, version, context)
            if outcome.is_usable:
                publish_rendered(outcome.rendered)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_rendering(renderer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A request is rendered with the active version for its purpose and the
    context assembled for it; with no active version it is refused by name
    inside the renderer, and with no context yet it is rendered with none,
    which the version's own declaration then judges.
    """
    from runtime.input_assembly import Batch, LatestByKey

    requests = Batch(read=context.bus.reader("llm-request"))
    versions = Batch(read=context.bus.reader("prompt-version"))
    contexts = LatestByKey(read=context.bus.reader("prompt-context"), key_of=lambda c: c.request_id, maximum_age_seconds=context.number("llm_request_context_maximum_age_seconds"))
    publish_rendered = context.bus.publisher_for("rendered-llm-request")
    renderer = PromptRenderer()
    active_by_purpose: dict[str, object] = {}

    def request_id_of(request) -> str:
        return f"{request.purpose}:{request.venue_id}:{request.symbol}:{request.requested_at_ns}"

    def read_jobs():
        for version in versions.payloads():
            if version.is_active:
                active_by_purpose[version.purpose] = version
            elif active_by_purpose.get(version.purpose) is not None and active_by_purpose[version.purpose].version_id == version.version_id:
                del active_by_purpose[version.purpose]
        by_request = contexts.mapping()
        return tuple(
            (request, active_by_purpose.get(request.purpose), by_request.get(request_id_of(request)))
            for request in requests.payloads()
        )

    return run_prompt_renderer(
        renderer=renderer,
        control_socket=context.control_socket,
        read_jobs=read_jobs,
        publish_rendered=lambda rendered: publish_rendered((rendered,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
