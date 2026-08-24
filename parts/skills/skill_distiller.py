"""skill-distiller: turning a source into structure, never into a summary.

A summary of a trading book is a shorter trading book, and it is useless: the
system cannot act on prose, and every reader of the summary has to re-extract
what it means. What the system can act on is structure, so distillation produces
exactly three things and refuses anything that produces none of them:

- **Frameworks** -- how the source says to think about a problem. Named, so two
  sources offering the same framework can be recognised as agreeing.
- **Decision rules** -- when-then statements. The only part a bot can be given
  directly, and the part a summary always loses first.
- **Anti-patterns** -- what the source says not to do. Systematically more useful
  than what it says to do, and systematically the first thing dropped in
  compression.

**Sections, not one blob.** A skill can be large and its use small: the loader
takes only the section a question needs, which is what makes a 400-page book
affordable to consult.

**A model may phrase; it may not invent.** Every rule and anti-pattern must be
traceable to a span of the source, and one that cannot be is dropped and counted.
A distiller that let a model add plausible rules would fill the system with
trading advice nobody wrote.

**A source that distils to nothing is reported as such.** Most writing about
markets contains no decision rules at all, and recording that honestly is more
useful than manufacturing three.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request
from runtime.knowledge_types import Skill, SourceDocument
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-distiller"

PART_DECLARATION = PartDeclaration(
    part_id="skill-distiller",
    consumes=("source-document", "validated-llm-output"),
    produces=("skill", "part-health", "llm-request"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "distil-a-source-into-structure"

INSTRUCTION = (
    "Extract from this source, in its own words wherever possible: the frameworks it "
    "offers, its decision rules as when-then statements, and the things it says not to do. "
    "Quote or closely paraphrase; do not add anything the source does not say. If it "
    "contains none of these, say so."
)

DISTILLED = "distilled"
NOTHING_TO_DISTIL = "the-source-contains-no-frameworks-rules-or-anti-patterns"
NOT_TRACEABLE = "what-came-back-cannot-be-traced-to-the-source"

FRAMEWORK = "framework"
DECISION_RULE = "decision-rule"
ANTI_PATTERN = "anti-pattern"


@dataclass(frozen=True)
class DistilledItem:
    """One framework, rule or anti-pattern, with where in the source it came from."""

    kind: str
    text: str
    source_span: str
    section: str

    @property
    def is_traceable(self) -> bool:
        return bool(self.source_span)


@dataclass
class DistillerStanding:
    sources_seen: int = 0
    skills_distilled: int = 0
    nothing_to_distil: int = 0
    items_extracted: int = 0
    items_dropped_as_untraceable: int = 0
    by_kind: dict = field(default_factory=dict)
    largest_skill_sections: int = 0


class SkillDistiller:
    """Extracts structure from a source, and drops anything not traceable to it."""

    def __init__(self, minimum_items: int, now_ns=time.time_ns) -> None:
        if minimum_items < 1:
            raise ValueError(
                "a skill with no frameworks, no rules and no anti-patterns is a summary "
                "wearing the type"
            )
        self._minimum_items = minimum_items
        self._now_ns = now_ns
        self.standing = DistillerStanding()

    def request(self, document: SourceDocument):
        """What to ask, with the source's own text as the only permitted material."""
        return make_request(
            purpose=PURPOSE,
            venue_id="",
            symbol="",
            instruction=INSTRUCTION,
            facts={"document_id": document.document_id, "length": len(document.content)},
            maximum_sentences=60,
            now_ns=self._now_ns,
        )

    def traceable_to(self, item_text: str, document: SourceDocument) -> str:
        """The span of the source this came from, or empty if it is not there.

        A distiller that let a model add plausible rules would fill the system
        with trading advice nobody wrote.
        """
        content = document.content.lower()
        words = [word for word in item_text.lower().split() if len(word) > 4]
        if not words:
            return ""
        # A run of the item's distinctive words appearing in the source is the
        # trace. Loose enough for a paraphrase, tight enough to catch invention.
        found = [word for word in words if word in content]
        if len(found) < max(2, len(words) // 3):
            return ""
        first = min(content.find(word) for word in found if content.find(word) >= 0)
        return document.content[max(0, first - 40) : first + 160]

    def distil(
        self, document: SourceDocument, items: tuple, version: str = "1"
    ) -> tuple[Skill | None, str]:
        """One source into one skill, keeping only what traces back to it."""
        self.standing.sources_seen += 1

        kept = []
        for kind, text, section in items:
            span = self.traceable_to(text, document)
            if not span:
                self.standing.items_dropped_as_untraceable += 1
                continue
            kept.append(DistilledItem(kind=kind, text=text, source_span=span, section=section))
            self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1

        self.standing.items_extracted += len(kept)

        if len(kept) < self._minimum_items:
            # Most writing about markets contains no decision rules at all, and
            # recording that honestly is more useful than manufacturing three.
            self.standing.nothing_to_distil += 1
            return None, NOTHING_TO_DISTIL

        sections: dict[str, str] = {}
        for item in kept:
            sections.setdefault(item.section, "")
            sections[item.section] += (
                ("\n" if sections[item.section] else "") + f"[{item.kind}] {item.text}"
            )

        self.standing.largest_skill_sections = max(
            self.standing.largest_skill_sections, len(sections)
        )
        self.standing.skills_distilled += 1

        return (
            Skill(
                skill_id=f"skill:{document.document_id}",
                title=document.title,
                sections=sections,
                frameworks=tuple(item.text for item in kept if item.kind == FRAMEWORK),
                decision_rules=tuple(item.text for item in kept if item.kind == DECISION_RULE),
                anti_patterns=tuple(item.text for item in kept if item.kind == ANTI_PATTERN),
                source_reference=document.source_reference,
                version=version,
                distilled_at_ns=self._now_ns(),
            ),
            DISTILLED,
        )


def describe_distillation(distiller: SkillDistiller) -> dict:
    return {
        "part_id": PART_ID,
        "sources_seen": distiller.standing.sources_seen,
        "skills_distilled": distiller.standing.skills_distilled,
        "sources_with_nothing_to_distil": distiller.standing.nothing_to_distil,
        "items_extracted": distiller.standing.items_extracted,
        "items_dropped_as_untraceable": distiller.standing.items_dropped_as_untraceable,
        "by_kind": dict(sorted(distiller.standing.by_kind.items())),
        "largest_skill_sections": distiller.standing.largest_skill_sections,
        "produces_summaries": False,
    }


def run_skill_distiller(
    distiller: SkillDistiller, control_socket, read_documents, publish_skills,
    publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        skills = []
        requests = []
        for document, items in read_documents(distiller):
            if items is None:
                requests.append(distiller.request(document))
                continue
            skill, _ = distiller.distil(document, items)
            if skill is not None:
                skills.append(skill)
        publish_skills(tuple(skills))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_distillation(distiller),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A document is held and a request for its distillation goes out; the
    items come back as a validated output for this part's purpose whose
    value names the document and lists items. No model is configured in
    phase 1, so every document waits.
    """
    from runtime.input_assembly import Batch

    documents = Batch(read=context.bus.reader("source-document"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_skills = context.bus.publisher_for("skill")
    publish_requests = context.bus.publisher_for("llm-request")
    distiller = SkillDistiller(minimum_items=int(context.number("skill_minimum_distilled_items")))
    waiting: dict[str, object] = {}

    def items_from(value: dict) -> tuple:
        items = []
        for raw in value.get("items", ()) or ():
            if not isinstance(raw, dict):
                continue
            items.append(DistilledItem(
                kind=str(raw.get("kind", DECISION_RULE)), text=str(raw.get("text", "")),
                source_span=str(raw.get("source_span", "")), section=str(raw.get("section", "rules")),
            ))
        return tuple(items)

    def read_documents(_distiller):
        jobs = []
        for document in documents.payloads():
            waiting[document.document_id] = document
            jobs.append((document, None))
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            document = waiting.pop(str(value.get("document_id", "")), None)
            if document is not None:
                jobs.append((document, items_from(value)))
        return tuple(jobs)

    def publish_some(publish):
        return lambda items: publish(tuple(items)) if items else None

    return run_skill_distiller(
        distiller=distiller,
        control_socket=context.control_socket,
        read_documents=read_documents,
        publish_skills=publish_some(publish_skills),
        publish_requests=publish_some(publish_requests),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
