"""context-assembler: everything a prompt gets, inside the budget it must fit.

Assembly is where prompts silently break. The pieces are gathered, they exceed the
window, and something falls off the end -- almost always the last thing appended,
which in a naive implementation is the measured facts. The model then answers
fluently from retrieved prose with no numbers to be checked against, and every
downstream check passes because there is nothing left to contradict.

So this part inverts the priority and makes the loss visible:

- **Verified facts are placed first and are never dropped.** They are the ground
  truth the answer will be checked against; a context without them cannot produce a
  checkable answer, so assembly fails rather than proceeding.
- **Retrieved passages are the compressible part**, dropped from the weakest
  upward, and every drop is named in the result. A caller can see the answer was
  formed without a section.
- **The budget comes from the part's own allocation.** One part's context cannot
  quietly consume what another part needs, and a part with no budget assembles
  nothing rather than defaulting to generous.
- **Nothing is summarised to fit.** Summarising to fit means a model rewrote the
  evidence before another model reasoned over it, and the rewrite is unversioned
  and unchecked.

The staleness of the facts travels with them. A snapshot measured four minutes ago
is not the same evidence as one measured now, and a prompt that cannot tell the
difference produces answers about a market that has moved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import PromptContext
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "context-assembler"

PART_DECLARATION = PartDeclaration(
    part_id="context-assembler",
    consumes=("retrieval-hit", "verified-snapshot", "llm-part-budget"),
    produces=("prompt-context", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ASSEMBLED = "assembled"
FACTS_DO_NOT_FIT = "the-verified-facts-alone-exceed-the-budget"
NO_FACTS = "there-is-no-verified-snapshot-to-check-an-answer-against"
NO_BUDGET = "this-part-has-no-budget-to-assemble-into"
SNAPSHOT_TOO_STALE = "the-measured-facts-describe-a-market-that-has-moved"

# The order sections are placed in, and therefore the reverse order they are lost in.
FACTS_SECTION = "verified-facts"
RETRIEVED_SECTION = "retrieved"


@dataclass(frozen=True)
class ContextSection:
    kind: str
    label: str
    text: str
    characters: int
    similarity: float | None
    is_droppable: bool


@dataclass(frozen=True)
class AssembledContext:
    request_id: str
    state: str
    context: PromptContext | None
    sections: tuple
    dropped: tuple
    characters: int
    budget: int
    reason: str
    assembled_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ASSEMBLED and self.context is not None


@dataclass
class AssemblerStanding:
    contexts_assembled: int = 0
    contexts_refused_no_facts: int = 0
    contexts_refused_no_budget: int = 0
    contexts_refused_facts_too_large: int = 0
    contexts_refused_stale: int = 0
    passages_dropped: int = 0
    characters_dropped: int = 0
    times_facts_were_dropped: int = 0


class ContextAssembler:
    """Places facts first, drops only retrieved prose, and names what it dropped."""

    def __init__(self, maximum_staleness_seconds: float, now_ns=time.time_ns) -> None:
        if maximum_staleness_seconds <= 0:
            raise ValueError(
                "facts have an age, and a prompt that cannot tell four-minute-old numbers "
                "from current ones answers about a market that has moved"
            )
        self._maximum_staleness = maximum_staleness_seconds
        self._now_ns = now_ns
        self._sequence = 0
        self.standing = AssemblerStanding()

    def assemble(self, request_id: str, snapshot, hits, budget) -> AssembledContext:
        if budget is None or budget.character_budget <= 0:
            self.standing.contexts_refused_no_budget += 1
            return self._assembled(
                request_id, NO_BUDGET, None, (), (), 0, 0,
                "this part has no character budget. Assembling generously by default is "
                "how one part consumes what a whole segment needs",
            )

        if snapshot is None or not snapshot.can_be_used_as_ground_truth:
            self.standing.contexts_refused_no_facts += 1
            return self._assembled(
                request_id, NO_FACTS, None, (), (), 0, budget.character_budget,
                "there is no complete verified snapshot. Without measured facts the answer "
                "cannot be checked against anything, and an uncheckable answer is the one "
                "a reader believes hardest"
                + (
                    f" (missing: {', '.join(snapshot.missing_facts)})"
                    if snapshot is not None and snapshot.missing_facts
                    else ""
                ),
            )

        if snapshot.staleness_seconds > self._maximum_staleness:
            self.standing.contexts_refused_stale += 1
            return self._assembled(
                request_id, SNAPSHOT_TOO_STALE, None, (), (), 0, budget.character_budget,
                f"the snapshot is {snapshot.staleness_seconds:.1f}s old against a "
                f"{self._maximum_staleness:.1f}s limit",
            )

        facts_text = self._render_facts(snapshot)
        facts_section = ContextSection(
            kind=FACTS_SECTION,
            label=f"{snapshot.venue_id}:{snapshot.symbol}",
            text=facts_text,
            characters=len(facts_text),
            similarity=None,
            is_droppable=False,
        )

        if facts_section.characters > budget.character_budget:
            self.standing.contexts_refused_facts_too_large += 1
            return self._assembled(
                request_id, FACTS_DO_NOT_FIT, None, (), (), facts_section.characters,
                budget.character_budget,
                f"the verified facts alone are {facts_section.characters} character(s) "
                f"against a {budget.character_budget} budget. Dropping them to fit is what "
                f"produces a fluent answer with nothing to check",
            )

        remaining = budget.character_budget - facts_section.characters
        # Strongest first, so what is lost is the weakest evidence rather than
        # whatever happened to be appended last.
        ordered = sorted(hits or (), key=lambda hit: -hit.similarity)
        kept = [facts_section]
        dropped = []
        for hit in ordered:
            section = ContextSection(
                kind=RETRIEVED_SECTION,
                label=hit.source_reference,
                text=hit.text,
                characters=hit.characters,
                similarity=hit.similarity,
                is_droppable=True,
            )
            if section.characters <= remaining:
                kept.append(section)
                remaining -= section.characters
            else:
                dropped.append(section)

        self.standing.passages_dropped += len(dropped)
        self.standing.characters_dropped += sum(section.characters for section in dropped)

        characters = sum(section.characters for section in kept)
        self._sequence += 1
        context = PromptContext(
            context_id=f"c-{self._sequence}",
            request_id=request_id,
            sections=tuple(
                (section.kind, section.label, section.text) for section in kept
            ),
            characters=characters,
            character_budget=budget.character_budget,
            dropped_sections=tuple(section.label for section in dropped),
            verified_facts=dict(snapshot.facts),
            assembled_at_ns=self._now_ns(),
        )
        self.standing.contexts_assembled += 1

        return self._assembled(
            request_id, ASSEMBLED, context, tuple(kept), tuple(dropped), characters,
            budget.character_budget,
            f"{len(kept)} section(s) in {characters}/{budget.character_budget} characters, "
            f"facts first and never droppable"
            + (
                f", {len(dropped)} passage(s) dropped from the weakest upward and named"
                if dropped
                else ""
            )
            + ". Nothing was summarised to fit: that would be a model rewriting the "
              "evidence before another model reasons over it",
        )

    @staticmethod
    def _render_facts(snapshot) -> str:
        lines = [
            f"measured {snapshot.staleness_seconds:.1f}s ago for "
            f"{snapshot.venue_id}:{snapshot.symbol}"
        ]
        for name, value in sorted(snapshot.facts.items()):
            lines.append(f"{name} = {value}")
        return "\n".join(lines)

    def _assembled(
        self, request_id, state, context, sections, dropped, characters, budget, reason,
    ) -> AssembledContext:
        return AssembledContext(
            request_id=request_id, state=state, context=context, sections=sections,
            dropped=dropped, characters=characters, budget=budget, reason=reason,
            assembled_at_ns=self._now_ns(),
        )


def describe_assembly(assembler: ContextAssembler) -> dict:
    return {
        "part_id": PART_ID,
        "contexts_assembled": assembler.standing.contexts_assembled,
        "refused_no_verified_facts": assembler.standing.contexts_refused_no_facts,
        "refused_no_budget": assembler.standing.contexts_refused_no_budget,
        "refused_facts_larger_than_the_budget": (
            assembler.standing.contexts_refused_facts_too_large
        ),
        "refused_stale_snapshot": assembler.standing.contexts_refused_stale,
        "passages_dropped": assembler.standing.passages_dropped,
        "characters_dropped": assembler.standing.characters_dropped,
        "times_facts_were_dropped": assembler.standing.times_facts_were_dropped,
        "summarises_to_fit": False,
    }


def run_context_assembler(
    assembler: ContextAssembler, control_socket, read_jobs, publish_contexts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for request_id, snapshot, hits, budget in read_jobs():
            assembled = assembler.assemble(request_id, snapshot, hits, budget)
            if assembled.is_usable:
                publish_contexts(assembled.context)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
