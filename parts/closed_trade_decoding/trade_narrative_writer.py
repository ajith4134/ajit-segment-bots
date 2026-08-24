"""trade-narrative-writer: the trade in sentences, every one traced to a measurement.

A narrative is the most dangerous artefact this system produces, because it is the
one a person reads and believes. A fluent paragraph about why a trade worked is
persuasive whether or not any of it is true, and once written it gets quoted
downstream as though it were evidence.

So the narrative is assembled from measured facts and then checked back against them.
The model is allowed to phrase; it is not allowed to conclude, and it is never asked
to recall a number.

Four rules:

- **Every sentence with a number must trace to a measurement**, or it is removed and
  the removal is recorded. A shorter narrative with a list of what was cut is far
  more useful than a complete one with an invented clause.
- **Causal claims are refused unless the causal analysis produced them.** "The trade
  lost because the regime turned" is admissible only when the classifier said so;
  otherwise it is a story fitted to an outcome.
- **The narrative can be written without a model at all**, from the facts alone. That
  path is the fallback when the model is unavailable and the reference when checking
  whether the model added anything.
- **It is written once, when the trade closes.** Rewritten later with more knowledge,
  it becomes a description of what is believed now rather than what was understood
  then -- and the episode's whole value is being the latter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.trade_decoding_types import TradeNarrative
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-narrative-writer"
PURPOSE = "describe-a-closed-trade"

PART_DECLARATION = PartDeclaration(
    part_id="trade-narrative-writer",
    consumes=("trade-episode", "journal-entry", "decision-rationale", "validated-llm-output"),
    produces=("trade-narrative", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WRITTEN = "written"
AWAITING_PHRASING = "the-facts-are-assembled-and-await-phrasing"
WRITTEN_WITHOUT_A_MODEL = "written-from-the-facts-alone"
ALREADY_WRITTEN = "a-narrative-already-exists-for-this-trade"
NOTHING_TO_SAY = "no-measured-fact-to-write-from"

# Words that assert causation. Admissible only when the causal analysis produced them.
CAUSAL_WORDS = ("because", "caused by", "due to", "as a result of", "led to", "owing to")


@dataclass(frozen=True)
class NarrativeOutcome:
    trade_id: str
    state: str
    narrative: TradeNarrative | None
    request: object | None
    reason: str
    written_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.narrative is not None


@dataclass
class WriterStanding:
    narratives_written: int = 0
    written_without_a_model: int = 0
    requests_made: int = 0
    sentences_removed: int = 0
    causal_claims_removed: int = 0
    rewrites_refused: int = 0
    nothing_to_say: int = 0


class TradeNarrativeWriter:
    """Assembles facts, lets a model phrase them, and removes what does not trace back."""

    def __init__(
        self, relative_tolerance: float, maximum_sentences: int, now_ns=time.time_ns,
    ) -> None:
        if maximum_sentences < 1:
            raise ValueError("a narrative of zero sentences is not a narrative")
        self._relative_tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._written: dict[str, TradeNarrative] = {}
        self.standing = WriterStanding()

    def facts_from(self, episode, loss_cause=None) -> dict:
        facts = {
            name: value
            for name, value in episode.conditions.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        facts["realised"] = episode.realised
        return facts

    def sentences_from_facts(self, episode, loss_cause=None) -> tuple:
        """The no-model path: every sentence is a measurement stated plainly."""
        sentences = [
            f"The trade realised {episode.realised:+.4f} over "
            f"{episode.conditions.get('holding_seconds', 0.0) / 60.0:.1f} minutes."
        ]
        percentile = episode.conditions.get("entry_percentile")
        if percentile is not None:
            sentences.append(
                f"The entry was better than {percentile:.2f} of the prices reachable "
                f"when the signal appeared."
            )
        captured = episode.conditions.get("captured_fraction")
        if captured is not None:
            sentences.append(
                f"The exit captured {captured:.2f} of the favourable move."
            )
        cost_share = episode.conditions.get("cost_share")
        if cost_share is not None:
            sentences.append(f"Costs took {cost_share:.2f} of the gross move.")
        if loss_cause is not None:
            sentences.append(
                f"The classifier attributed this loss to {loss_cause.cause}."
            )
        return tuple(sentences[: self._maximum_sentences])

    def write(
        self, trade_id: str, episode, loss_cause=None, phrased_text: str | None = None,
    ) -> NarrativeOutcome:
        if trade_id in self._written:
            self.standing.rewrites_refused += 1
            return self._outcome(
                trade_id, ALREADY_WRITTEN, self._written[trade_id], None,
                "a narrative already exists. Rewritten later with more knowledge it "
                "becomes a description of what is believed now rather than what was "
                "understood then, and the episode's value is being the latter",
            )

        facts = self.facts_from(episode, loss_cause)
        if not facts:
            self.standing.nothing_to_say += 1
            return self._outcome(
                trade_id, NOTHING_TO_SAY, None, None,
                "no measured fact to write from. An unsupported narrative is the one a "
                "reader believes hardest",
            )

        if phrased_text is None:
            sentences = self.sentences_from_facts(episode, loss_cause)
            request = make_request(
                purpose=PURPOSE,
                venue_id=episode.venue_id,
                symbol=episode.symbol,
                instruction=(
                    "Rewrite these measured statements as plain prose. Do not add any "
                    "number, do not assert a cause that is not stated, and do not "
                    "speculate about what should have been done: "
                    + " ".join(sentences)
                ),
                facts=facts,
                maximum_sentences=self._maximum_sentences,
                now_ns=self._now_ns,
            )
            self.standing.requests_made += 1

            # The no-model narrative stands on its own, and is the reference for
            # judging whether the model added anything.
            narrative = TradeNarrative(
                trade_id=trade_id,
                text=" ".join(sentences),
                sentences_kept=sentences,
                sentences_removed=(),
                facts_used=dict(facts),
                was_written_by_a_model=False,
                written_at_ns=self._now_ns(),
            )
            self.standing.written_without_a_model += 1
            return self._outcome(
                trade_id, AWAITING_PHRASING, narrative, request,
                f"{len(sentences)} sentence(s) written from the facts alone, and a "
                f"request issued to phrase them. The unphrased version stands on its own",
            )

        verified = verify_against_facts(
            phrased_text, facts, relative_tolerance=self._relative_tolerance,
            require_a_citation=True,
        )
        kept = []
        removed = list(verified.removed_sentences)
        for sentence in verified.kept_sentences:
            lowered = sentence.lower()
            # A causal claim is admissible only if the classifier produced one.
            if any(word in lowered for word in CAUSAL_WORDS) and loss_cause is None:
                removed.append(sentence)
                self.standing.causal_claims_removed += 1
                continue
            kept.append(sentence)

        self.standing.sentences_removed += len(removed)

        narrative = TradeNarrative(
            trade_id=trade_id,
            text=" ".join(kept),
            sentences_kept=tuple(kept),
            sentences_removed=tuple(removed),
            facts_used=dict(facts),
            was_written_by_a_model=True,
            written_at_ns=self._now_ns(),
        )
        self._written[trade_id] = narrative
        self.standing.narratives_written += 1

        return self._outcome(
            trade_id, WRITTEN, narrative, None,
            f"{len(kept)} sentence(s) kept, {len(removed)} removed"
            + (
                " -- a shorter narrative with the cuts listed is more useful than a "
                "complete one with an invented clause"
                if removed
                else ", every one tracing to a measurement"
            ),
        )

    def _outcome(self, trade_id, state, narrative, request, reason) -> NarrativeOutcome:
        return NarrativeOutcome(
            trade_id=trade_id, state=state, narrative=narrative, request=request,
            reason=reason, written_at_ns=self._now_ns(),
        )


def describe_narrative_writing(writer: TradeNarrativeWriter) -> dict:
    return {
        "part_id": PART_ID,
        "narratives_written": writer.standing.narratives_written,
        "written_without_a_model": writer.standing.written_without_a_model,
        "requests_made": writer.standing.requests_made,
        "sentences_removed": writer.standing.sentences_removed,
        "causal_claims_removed": writer.standing.causal_claims_removed,
        "rewrites_refused": writer.standing.rewrites_refused,
        "nothing_to_say": writer.standing.nothing_to_say,
        "lets_a_model_assert_a_cause": False,
        "rewrites_a_narrative_later": False,
    }


def run_trade_narrative_writer(
    writer: TradeNarrativeWriter, control_socket, read_episodes, publish_narratives,
    publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, episode, loss_cause, phrased in read_episodes():
            outcome = writer.write(trade_id, episode, loss_cause, phrased)
            if outcome.request is not None:
                publish_requests(outcome.request)
            if outcome.is_usable:
                publish_narratives(outcome.narrative)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_narrative_writing(writer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every episode is narrated from its facts the moment it arrives and the
    request for a phrased version is published; a validated output for this
    purpose naming the same trade is written as the phrased narrative. No
    model is configured in phase 1, so every narrative is the factual one.
    """
    from runtime.input_assembly import Batch

    episodes = Batch(read=context.bus.reader("trade-episode"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    rationales = Batch(read=context.bus.reader("decision-rationale"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_narratives = context.bus.publisher_for("trade-narrative")
    publish_requests = context.bus.publisher_for("llm-request")
    writer = TradeNarrativeWriter(
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    pending: dict[str, object] = {}

    def read_episodes():
        entries.payloads()
        rationales.payloads()
        jobs = []
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            trade_id = str(value.get("trade_id", ""))
            episode = pending.pop(trade_id, None)
            if episode is not None:
                jobs.append((trade_id, episode, None, output.text))
        for episode in episodes.payloads():
            trade_id = episode.episode_id.split("-")[1] if episode.episode_id.count("-") >= 2 else episode.episode_id
            pending[trade_id] = episode
            jobs.append((trade_id, episode, None, None))
        return tuple(jobs)

    return run_trade_narrative_writer(
        writer=writer,
        control_socket=context.control_socket,
        read_episodes=read_episodes,
        publish_requests=lambda request: publish_requests((request,)),
        publish_narratives=lambda narrative: publish_narratives((narrative,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
