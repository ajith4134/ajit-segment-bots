"""source-ingester: what comes in, and what is refused at the door.

Everything this system reads from outside passes through here, which makes it the
one place that can keep the rest clean. Its job is not to understand a source --
that is the distiller's -- but to establish that a source is a source:

- **It has a reference that can be gone back to.** A document with no URL, DOI or
  file identifier cannot be rechecked, and a fact derived from it can never be
  corrected. That is refused at the door rather than discovered later.
- **It is not something already ingested.** The same paper arriving from three
  places is one source, and counting it three times makes agreement look like
  corroboration.
- **Its content is data, never instruction.** Text that reads like a command is
  text. Nothing here executes, obeys, or strips anything: the content is stored
  as it arrived and travels as a quotation.
- **It is large enough to contain something and small enough to hold.** A
  three-word source contains no framework, and an unbounded one is a memory
  problem in the shape of a document.

**A refresh replaces rather than duplicates.** The same reference ingested again
supersedes, with the previous version kept -- a source that has been revised is
information, and a store that duplicated it would let both be believed.

**Nothing is fetched here.** The readers fetch; this admits. Separating them means
a bad source is refused once rather than in three fetchers.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.knowledge_types import SourceDocument
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "source-ingester"

PART_DECLARATION = PartDeclaration(
    part_id="source-ingester",
    consumes=("research-finding", "skill-refresh-request"),
    produces=("source-document", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

INGESTED = "ingested"
REFRESHED = "it-supersedes-an-earlier-version-of-the-same-source"
NO_REFERENCE = "nothing-could-be-gone-back-to"
ALREADY_INGESTED = "the-same-content-is-already-held"
TOO_SHORT = "too-short-to-contain-a-framework-a-rule-or-an-anti-pattern"
TOO_LARGE = "larger-than-this-system-will-hold"

BOOK = "book"
PAPER = "paper"
CHAT = "community-chat"
TRANSCRIPT = "video-transcript"
POST = "post"

SOURCE_KINDS = (BOOK, PAPER, CHAT, TRANSCRIPT, POST)


@dataclass
class IngesterStanding:
    offered: int = 0
    ingested: int = 0
    refreshed: int = 0
    refused_no_reference: int = 0
    refused_duplicate: int = 0
    refused_too_short: int = 0
    refused_too_large: int = 0
    characters_held: int = 0
    by_kind: dict = field(default_factory=dict)


class SourceIngester:
    """Admits sources that can be rechecked, and refuses the rest at the door."""

    def __init__(
        self,
        minimum_characters: int,
        maximum_characters: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_characters < 1:
            raise ValueError("a source with no content is not a source")
        if maximum_characters <= minimum_characters:
            raise ValueError(
                "an unbounded source is a memory problem in the shape of a document"
            )
        self._minimum = minimum_characters
        self._maximum = maximum_characters
        self._now_ns = now_ns
        self._by_reference: dict[str, SourceDocument] = {}
        self._digests: dict[str, str] = {}
        self._superseded: dict[str, list] = {}
        self.standing = IngesterStanding()

    def digest_of(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def ingest(
        self,
        title: str,
        content: str,
        kind: str,
        source_reference: str,
        author: str | None = None,
        published_at_ns: int | None = None,
    ) -> tuple[SourceDocument | None, str]:
        """One source, admitted only if it can be gone back to and is worth holding."""
        self.standing.offered += 1

        if not source_reference:
            # Refused at the door rather than discovered later: a fact derived
            # from an unreferenced source can never be corrected.
            self.standing.refused_no_reference += 1
            return None, NO_REFERENCE

        if len(content) < self._minimum:
            self.standing.refused_too_short += 1
            return None, TOO_SHORT

        if len(content) > self._maximum:
            self.standing.refused_too_large += 1
            return None, TOO_LARGE

        digest = self.digest_of(content)
        existing = self._by_reference.get(source_reference)

        if existing is not None and self._digests.get(source_reference) == digest:
            # The same paper from three places is one source, and counting it
            # three times makes agreement look like corroboration.
            self.standing.refused_duplicate += 1
            return None, ALREADY_INGESTED

        for reference, held_digest in self._digests.items():
            if held_digest == digest and reference != source_reference:
                self.standing.refused_duplicate += 1
                return None, ALREADY_INGESTED

        outcome = INGESTED
        if existing is not None:
            # A revised source is information; duplicating it would let both be
            # believed.
            self._superseded.setdefault(source_reference, []).append(existing)
            self.standing.refreshed += 1
            outcome = REFRESHED

        document = SourceDocument(
            document_id=f"source:{digest}",
            title=title,
            # Stored as it arrived. Nothing here executes, obeys or strips: text
            # that reads like a command is text.
            content=content,
            kind=kind,
            source_reference=source_reference,
            fetched_at_ns=self._now_ns(),
            author=author,
            published_at_ns=published_at_ns,
        )

        self._by_reference[source_reference] = document
        self._digests[source_reference] = digest
        self.standing.ingested += 1
        self.standing.characters_held += len(content)
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1
        return document, outcome

    def document_for(self, source_reference: str) -> SourceDocument | None:
        return self._by_reference.get(source_reference)

    def superseded_versions(self, source_reference: str) -> tuple:
        return tuple(self._superseded.get(source_reference, ()))

    @property
    def documents_held(self) -> int:
        return len(self._by_reference)


def describe_ingestion(ingester: SourceIngester) -> dict:
    return {
        "part_id": PART_ID,
        "offered": ingester.standing.offered,
        "ingested": ingester.standing.ingested,
        "refreshed": ingester.standing.refreshed,
        "refused_no_reference": ingester.standing.refused_no_reference,
        "refused_duplicate": ingester.standing.refused_duplicate,
        "refused_too_short": ingester.standing.refused_too_short,
        "refused_too_large": ingester.standing.refused_too_large,
        "documents_held": ingester.documents_held,
        "characters_held": ingester.standing.characters_held,
        "by_kind": dict(sorted(ingester.standing.by_kind.items())),
        "executes_or_obeys_content": False,
        "fetches_anything": False,
    }


def run_source_ingester(
    ingester: SourceIngester, control_socket, read_findings, publish_documents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        documents = []
        for offer in read_findings(ingester):
            document, _ = ingester.ingest(**offer)
            if document is not None:
                documents.append(document)
        publish_documents(tuple(documents))

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

    A research finding is ingested as a paper-kind source under its first
    reference: its statement and its evidence are the content. A refresh
    request names a source to go back to, and no fetcher reaches this
    part, so requests are read and drained -- the fetchers upstream answer
    them.
    """
    from runtime.input_assembly import Batch

    findings = Batch(read=context.bus.reader("research-finding"))
    refreshes = Batch(read=context.bus.reader("skill-refresh-request"))
    publish_documents = context.bus.publisher_for("source-document")
    ingester = SourceIngester(
        minimum_characters=int(context.number("embedding_minimum_characters")),
        maximum_characters=int(context.number("source_maximum_characters")),
    )

    def read_findings(_ingester):
        refreshes.payloads()
        offers = []
        for finding in findings.payloads():
            references = tuple(str(r) for r in finding.source_references)
            if not references:
                continue
            evidence = "\n".join(str(item) for item in finding.evidence)
            offers.append({
                "title": finding.topic, "content": f"{finding.statement}\n{evidence}", "kind": PAPER,
                "source_reference": references[0], "published_at_ns": int(finding.found_at_ns),
            })
        return tuple(offers)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_documents(kept)

    return run_source_ingester(
        ingester=ingester,
        control_socket=context.control_socket,
        read_findings=read_findings,
        publish_documents=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
