"""idea-generator: proposing things nobody here has tried, and counting every one.

The system's only source of genuinely new hypotheses. Everything else refines
what exists; this proposes what does not, from four sources that fail in
different ways:

- **What the open web read**, which is untested anywhere here.
- **What this system's own trades suggest**, which is the richest source and the
  most prone to fitting its own history.
- **What the scorecards say is missing** -- a regime the system has no instruction
  for is a gap that names its own idea.
- **A model, asked to combine what is known into something that is not**, which
  is the only source that can produce a genuinely novel combination and the one
  most likely to produce a fluent nonsense.

**Every idea is a trial and is counted as one.** This is the rule that matters
most. A generator that produces a hundred ideas so that the hypothesis block can
find the three that test well has run a hundred trials, and any result from them
must clear a bar a hundred times higher. A system that generated freely and
counted only what survived would convince itself of noise with perfect
statistics -- so the count is emitted with the idea, not reconstructed later.

**An idea is stated as something testable or it is not an idea.** "Momentum works
in crypto" cannot be refuted; "the 4-hour momentum burst detector's calls in a
trending regime resolve within 40 minutes more often than the base rate" can.
Ideas that cannot be stated testably are rejected here rather than passed on.

**Nothing here is ever traded.** An idea's whole life until the hypothesis block
tests it is as a candidate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "idea-generator"

PART_DECLARATION = PartDeclaration(
    part_id="idea-generator",
    consumes=(
        "web-idea", "trade-episode", "bot-scorecard", "validated-llm-output",
        "sentiment-reading", "instruction-history", "knowledge-link", "regime-memory",
    ),
    produces=("novel-idea", "part-health", "llm-request"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "propose-a-testable-idea-this-system-has-not-tried"

INSTRUCTION = (
    "Propose one thing this system has not tried, stated so it could be shown false. "
    "Use only the facts given; every number you write must be one of them. Say what would "
    "have to be observed for the idea to be wrong."
)

FROM_THE_WEB = "read-elsewhere"
FROM_OUR_OWN_TRADES = "this-system's-own-record"
FROM_A_GAP = "a-context-with-no-instruction"
FROM_A_MODEL = "a-model-combining-what-is-known"

PROPOSED = "proposed"
NOT_TESTABLE = "cannot-be-stated-so-it-could-be-shown-false"
ALREADY_TRIED = "this-system-has-tried-this-before"
NOTHING_TO_PROPOSE_FROM = "no-source-had-anything"


@dataclass(frozen=True)
class NovelIdea:
    """One proposal, with what would refute it and the trial count behind it.

    `trials_in_this_family` travels with the idea rather than being looked up
    later: a generator that produced freely and counted only survivors would
    convince itself of noise with perfect statistics.
    """

    idea_id: str
    source: str
    statement: str
    what_would_refute_it: str
    context: dict
    trials_in_this_family: int
    evidence: dict
    state: str
    reason: str
    proposed_at_ns: int

    @property
    def is_testable(self) -> bool:
        return self.state == PROPOSED and bool(self.what_would_refute_it)

    @property
    def may_be_traded(self) -> bool:
        """Never, until the hypothesis block has tested it on this system's data."""
        return False


@dataclass
class GeneratorStanding:
    proposals: int = 0
    proposed: int = 0
    rejected_not_testable: int = 0
    rejected_already_tried: int = 0
    trials_counted: int = 0
    model_sentences_removed: int = 0
    by_source: dict = field(default_factory=dict)


class IdeaGenerator:
    """Proposes testable ideas from four sources, and counts every one as a trial."""

    def __init__(
        self,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._tried: set[str] = set()
        self._trials: dict[str, int] = {}
        self._web_ideas: list = []
        self._gaps: list = []
        self._facts: dict = {}
        self.standing = GeneratorStanding()

    def observe_web_idea(self, idea) -> None:
        self._web_ideas.append(idea)

    def observe_gap(self, venue_id: str, symbol: str, regime: str) -> None:
        """A context the system has no instruction for. A gap names its own idea."""
        self._gaps.append((venue_id, symbol, regime))

    def observe_instruction_history(self, statement: str) -> None:
        """Something already tried, so it is not proposed again as novel."""
        self._tried.add(self._normalise(statement))

    def observe_facts(self, facts: dict) -> None:
        """The measurements a model may phrase into an idea, and nothing else."""
        self._facts = dict(facts)

    def trials_in(self, family: str) -> int:
        return self._trials.get(family, 0)

    def request(self, family: str):
        return make_request(
            purpose=PURPOSE,
            venue_id=self._facts.get("venue_id", ""),
            symbol=self._facts.get("symbol", ""),
            instruction=INSTRUCTION,
            facts=self._facts or {"trials_so_far": self.trials_in(family)},
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def propose(
        self, family: str, source: str, statement: str, what_would_refute_it: str,
        context: dict | None = None, evidence: dict | None = None,
    ) -> NovelIdea:
        """One idea, counted as a trial whether or not anything comes of it."""
        self.standing.proposals += 1
        self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1

        # Counted here, before anything is judged. This is the whole point.
        self._trials[family] = self._trials.get(family, 0) + 1
        self.standing.trials_counted += 1
        trials = self._trials[family]

        if not what_would_refute_it.strip():
            self.standing.rejected_not_testable += 1
            return self._idea(
                family, source, statement, what_would_refute_it, context, trials, evidence,
                NOT_TESTABLE,
                "nothing was named that would show this false. 'Momentum works in crypto' "
                "cannot be refuted; a statement about a named detector's calls in a named "
                "regime resolving within a named window can",
            )

        if self._normalise(statement) in self._tried:
            self.standing.rejected_already_tried += 1
            return self._idea(
                family, source, statement, what_would_refute_it, context, trials, evidence,
                ALREADY_TRIED,
                "this system has tried this before; proposing it again would spend a trial "
                "to relearn something already recorded",
            )

        self._tried.add(self._normalise(statement))
        self.standing.proposed += 1
        return self._idea(
            family, source, statement, what_would_refute_it, context, trials, evidence,
            PROPOSED,
            f"from {source}, the {trials}th trial in {family}. Any result from this family "
            f"must clear a bar {trials} times higher than a single test would need, and that "
            f"count travels with the idea rather than being reconstructed later",
        )

    def propose_from_gaps(self, family: str) -> tuple:
        """A context with no instruction is a gap that names its own idea."""
        ideas = []
        while self._gaps:
            venue_id, symbol, regime = self._gaps.pop(0)
            ideas.append(
                self.propose(
                    family=family,
                    source=FROM_A_GAP,
                    statement=(
                        f"an instruction for {symbol} in the {regime} regime would resolve "
                        f"more often than this system's base rate there"
                    ),
                    what_would_refute_it=(
                        f"instructions compiled for {symbol} in {regime} resolving at or "
                        f"below the base rate over a measured sample"
                    ),
                    context={"venue_id": venue_id, "symbol": symbol, "regime": regime},
                    evidence={"source": "a context with no instruction"},
                )
            )
        return tuple(ideas)

    def propose_from_the_web(self, family: str) -> tuple:
        ideas = []
        while self._web_ideas:
            web = self._web_ideas.pop(0)
            ideas.append(
                self.propose(
                    family=family,
                    source=FROM_THE_WEB,
                    statement=web.title,
                    what_would_refute_it=(
                        "this system's own data failing to reproduce the effect the source "
                        "describes"
                    ),
                    context={"source_url": web.source_url},
                    evidence={"quotation": web.content, "source_url": web.source_url},
                )
            )
        return tuple(ideas)

    def propose_from_a_model(self, family: str, model_output: str | None) -> tuple:
        """A model's proposal, with every number checked against the facts it was given."""
        if model_output is None:
            return ()
        verified = verify_against_facts(
            model_output, self._facts, self._tolerance, require_a_citation=False
        )
        self.standing.model_sentences_removed += len(verified.removed_sentences)
        if verified.is_empty:
            return ()

        sentences = list(verified.kept_sentences)
        statement = sentences[0]
        refutation = sentences[1] if len(sentences) > 1 else ""
        return (
            self.propose(
                family=family,
                source=FROM_A_MODEL,
                statement=statement,
                what_would_refute_it=refutation,
                context=dict(self._facts),
                evidence={"citations": verified.citations},
            ),
        )

    def _normalise(self, statement: str) -> str:
        return " ".join(statement.lower().split())

    def _idea(
        self, family, source, statement, refutation, context, trials, evidence, state, reason
    ) -> NovelIdea:
        return NovelIdea(
            idea_id=f"{family}:{trials}",
            source=source,
            statement=statement,
            what_would_refute_it=refutation,
            context=dict(context or {}),
            trials_in_this_family=trials,
            evidence=dict(evidence or {}),
            state=state,
            reason=reason,
            proposed_at_ns=self._now_ns(),
        )


def describe_idea_generation(generator: IdeaGenerator) -> dict:
    return {
        "part_id": PART_ID,
        "proposals": generator.standing.proposals,
        "proposed": generator.standing.proposed,
        "rejected_as_untestable": generator.standing.rejected_not_testable,
        "rejected_as_already_tried": generator.standing.rejected_already_tried,
        "trials_counted": generator.standing.trials_counted,
        "trials_by_family": dict(sorted(generator._trials.items())),
        "by_source": dict(sorted(generator.standing.by_source.items())),
        "model_sentences_removed": generator.standing.model_sentences_removed,
        "ideas_may_be_traded": False,
    }


def run_idea_generator(
    generator: IdeaGenerator, control_socket, read_sources, publish_ideas, publish_requests,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        family, model_output = read_sources(generator)
        ideas = (
            generator.propose_from_gaps(family)
            + generator.propose_from_the_web(family)
            + generator.propose_from_a_model(family, model_output)
        )
        publish_ideas(ideas)
        if model_output is None:
            publish_requests((generator.request(family),))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
