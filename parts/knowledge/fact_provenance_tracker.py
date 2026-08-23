"""fact-provenance-tracker: where each thing this system believes came from.

A fact whose source cannot be named cannot be rechecked, and a system that cannot
recheck cannot correct. That is the whole argument for this part, and it is worth
the storage because the alternative is a body of beliefs that can only grow.

What it records, and why each matters:

- **The source, specifically enough to go back to it.** "A paper" is not
  provenance; a URL, a document identifier or a measurement run is.
- **The chain.** A fact inferred from other facts is only as good as its
  weakest ancestor, and a chain that is not recorded cannot be walked when the
  ancestor turns out to be wrong. This is how one bad source quietly poisons
  everything derived from it.
- **When it was established, and when it was last confirmed.** Those are
  different: a fact established two years ago and confirmed yesterday is fresh,
  and one established yesterday from a two-year-old document is not.
- **Whether the source still exists.** A URL that has gone is a fact that can no
  longer be rechecked, and it should be treated differently from one that can.

**Provenance is never inferred.** A fact arriving without a source is recorded as
having none, which is a state, rather than being attributed to whoever last
touched it.

**A source found to be wrong invalidates everything derived from it**, and this
part is what makes that possible: the chain runs both ways.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "fact-provenance-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="fact-provenance-tracker",
    consumes=("semantic-fact", "research-finding", "source-document"),
    produces=("fact-provenance", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TRACKED = "tracked"
NO_SOURCE = "this-fact-arrived-without-a-source"
SOURCE_IS_GONE = "the-source-can-no-longer-be-reached"
INVALIDATED = "an-ancestor-was-found-to-be-wrong"


@dataclass(frozen=True)
class FactProvenance:
    """Where one fact came from, and everything it depends on."""

    fact_key: str
    state: str
    source: str | None
    source_reference: str | None
    derived_from: tuple
    established_at_ns: int
    last_confirmed_at_ns: int | None
    source_still_reachable: bool
    weakest_ancestor: str | None
    reason: str
    tracked_at_ns: int

    @property
    def can_be_rechecked(self) -> bool:
        return self.source_reference is not None and self.source_still_reachable

    @property
    def is_derived(self) -> bool:
        return bool(self.derived_from)

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.established_at_ns) / 1e9

    def seconds_since_confirmed(self, now_ns: int) -> float | None:
        if self.last_confirmed_at_ns is None:
            return None
        return (now_ns - self.last_confirmed_at_ns) / 1e9


@dataclass
class TrackerStanding:
    facts_tracked: int = 0
    facts_without_a_source: int = 0
    sources_gone: int = 0
    facts_invalidated: int = 0
    longest_chain: int = 0
    confirmations: int = 0
    by_source_kind: dict = field(default_factory=dict)


class FactProvenanceTracker:
    """Records where every belief came from, and what it depends on."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._provenance: dict[str, FactProvenance] = {}
        self._derived_from: dict[str, tuple] = {}
        self._reachable: dict[str, bool] = {}
        self._invalidated: set[str] = set()
        self.standing = TrackerStanding()

    def record(
        self,
        fact_key: str,
        source: str | None,
        source_reference: str | None = None,
        derived_from=(),
        source_kind: str = "unknown",
    ) -> FactProvenance:
        """One fact's origin. Never inferred: a fact with no source has none."""
        self.standing.facts_tracked += 1
        self.standing.by_source_kind[source_kind] = (
            self.standing.by_source_kind.get(source_kind, 0) + 1
        )

        derived_from = tuple(derived_from)
        self._derived_from[fact_key] = derived_from
        chain = self.chain_for(fact_key)
        self.standing.longest_chain = max(self.standing.longest_chain, len(chain))

        if source is None and not derived_from:
            self.standing.facts_without_a_source += 1
            state = NO_SOURCE
        elif fact_key in self._invalidated:
            state = INVALIDATED
        else:
            state = TRACKED

        weakest = self._weakest_ancestor(fact_key)

        provenance = FactProvenance(
            fact_key=fact_key,
            state=state,
            source=source,
            source_reference=source_reference,
            derived_from=derived_from,
            established_at_ns=self._now_ns(),
            last_confirmed_at_ns=None,
            source_still_reachable=self._reachable.get(source_reference or "", True),
            weakest_ancestor=weakest,
            reason=(
                f"{fact_key} came from "
                + (
                    f"{source} ({source_reference})"
                    if source_reference
                    else (source or "nothing recorded")
                )
                + (
                    f", derived from {len(derived_from)} other fact(s) through a chain of "
                    f"{len(chain)}"
                    if derived_from
                    else ""
                )
                + (
                    f"; its weakest ancestor is {weakest}, and a derived fact is only as good "
                    f"as that"
                    if weakest
                    else ""
                )
                + (
                    ". No source was recorded, which is a state rather than an attribution to "
                    "whoever last touched it"
                    if state == NO_SOURCE
                    else ""
                )
            ),
            tracked_at_ns=self._now_ns(),
        )
        self._provenance[fact_key] = provenance
        return provenance

    def confirm(self, fact_key: str) -> FactProvenance | None:
        """This fact was checked again. Establishment and confirmation are different."""
        provenance = self._provenance.get(fact_key)
        if provenance is None:
            return None
        self.standing.confirmations += 1
        confirmed = FactProvenance(
            fact_key=provenance.fact_key, state=provenance.state, source=provenance.source,
            source_reference=provenance.source_reference,
            derived_from=provenance.derived_from,
            established_at_ns=provenance.established_at_ns,
            last_confirmed_at_ns=self._now_ns(),
            source_still_reachable=provenance.source_still_reachable,
            weakest_ancestor=provenance.weakest_ancestor,
            reason=(
                f"{provenance.reason}. Confirmed again: a fact established long ago and "
                f"confirmed today is fresh, and one established today from an old document "
                f"is not"
            ),
            tracked_at_ns=self._now_ns(),
        )
        self._provenance[fact_key] = confirmed
        return confirmed

    def source_is_gone(self, source_reference: str) -> tuple:
        """A source that can no longer be reached. Its facts can no longer be rechecked."""
        self._reachable[source_reference] = False
        affected = []
        for fact_key, provenance in list(self._provenance.items()):
            if provenance.source_reference != source_reference:
                continue
            self.standing.sources_gone += 1
            self._provenance[fact_key] = FactProvenance(
                fact_key=provenance.fact_key, state=SOURCE_IS_GONE, source=provenance.source,
                source_reference=provenance.source_reference,
                derived_from=provenance.derived_from,
                established_at_ns=provenance.established_at_ns,
                last_confirmed_at_ns=provenance.last_confirmed_at_ns,
                source_still_reachable=False,
                weakest_ancestor=provenance.weakest_ancestor,
                reason=(
                    f"{provenance.reason}. The source can no longer be reached, so this fact "
                    f"can no longer be rechecked -- which is different from it being wrong"
                ),
                tracked_at_ns=self._now_ns(),
            )
            affected.append(self._provenance[fact_key])
        return tuple(affected)

    def invalidate(self, fact_key: str) -> tuple:
        """A fact found wrong, and everything derived from it.

        The chain runs both ways: this is how one bad source stops quietly
        poisoning everything below it.
        """
        self._invalidated.add(fact_key)
        affected = [fact_key]
        changed = True
        while changed:
            changed = False
            for candidate, ancestors in self._derived_from.items():
                if candidate in self._invalidated:
                    continue
                if any(ancestor in self._invalidated for ancestor in ancestors):
                    self._invalidated.add(candidate)
                    affected.append(candidate)
                    changed = True
        self.standing.facts_invalidated += len(affected)
        return tuple(affected)

    def chain_for(self, fact_key: str) -> tuple:
        """Every fact this one rests on, walked to the root."""
        chain = []
        frontier = [fact_key]
        seen = {fact_key}
        while frontier:
            current = frontier.pop(0)
            for ancestor in self._derived_from.get(current, ()):
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                chain.append(ancestor)
                frontier.append(ancestor)
        return tuple(chain)

    def provenance_of(self, fact_key: str) -> FactProvenance | None:
        return self._provenance.get(fact_key)

    def _weakest_ancestor(self, fact_key: str) -> str | None:
        """The ancestor with no source of its own, if there is one."""
        for ancestor in self.chain_for(fact_key):
            provenance = self._provenance.get(ancestor)
            if provenance is None or provenance.source_reference is None:
                return ancestor
        return None


def describe_provenance(tracker: FactProvenanceTracker) -> dict:
    return {
        "part_id": PART_ID,
        "facts_tracked": tracker.standing.facts_tracked,
        "facts_without_a_source": tracker.standing.facts_without_a_source,
        "facts_whose_source_is_gone": tracker.standing.sources_gone,
        "facts_invalidated": tracker.standing.facts_invalidated,
        "longest_derivation_chain": tracker.standing.longest_chain,
        "confirmations": tracker.standing.confirmations,
        "by_source_kind": dict(sorted(tracker.standing.by_source_kind.items())),
        "infers_provenance": False,
    }


def run_fact_provenance_tracker(
    tracker: FactProvenanceTracker, control_socket, read_facts, publish_provenance,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_provenance(
            tuple(tracker.record(**record) for record in read_facts(tracker))
        )

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

    A semantic fact is tracked under its venue, symbol and key with the
    source it named; a research finding under its id, derived from the
    sources it cites; a source document under its id, with its reference.
    """
    from runtime.input_assembly import Batch

    facts = Batch(read=context.bus.reader("semantic-fact"))
    findings = Batch(read=context.bus.reader("research-finding"))
    documents = Batch(read=context.bus.reader("source-document"))
    publish_provenance = context.bus.publisher_for("fact-provenance")
    tracker = FactProvenanceTracker()

    def read_facts(_tracker):
        records = []
        for document in documents.payloads():
            records.append({
                "fact_key": f"document:{document.document_id}", "source": document.kind,
                "source_reference": document.source_reference, "derived_from": (), "source_kind": "source-document",
            })
        for finding in findings.payloads():
            references = tuple(str(r) for r in finding.source_references)
            records.append({
                "fact_key": f"finding:{finding.finding_id}", "source": finding.topic,
                "source_reference": references[0] if references else None,
                "derived_from": tuple(f"document:{r}" for r in references), "source_kind": "research-finding",
            })
        for fact in facts.payloads():
            records.append({
                "fact_key": f"{fact.venue_id}:{fact.symbol}:{fact.key}", "source": fact.source,
                "source_reference": fact.source_reference, "derived_from": (), "source_kind": fact.source,
            })
        return tuple(records)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_provenance(kept)

    return run_fact_provenance_tracker(
        tracker=tracker,
        control_socket=context.control_socket,
        read_facts=read_facts,
        publish_provenance=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
