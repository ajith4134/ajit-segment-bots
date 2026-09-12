"""setup-second-opinion-reasoner: does the case for this specific setup hold up?

Step 2 of docs/proposals/llm-reasoning-gets-a-vote.md (RL-010/013/026). **A real
vote**, weighed by `opinion-arbiter` exactly like bull-bot, bear-bot and the
tailgater -- but it never proposes its own setup. It only reviews a symbol
another bot already has a live, acting `directional-opinion` on this tick, so
its own opinion is bounded by construction to what has already been found.

**`timing=None, exit_plan=None`, always.** An LLM should not be inventing a
stop price by reading a book, so it can confirm or refuse a candidate; it
cannot manufacture a trade with a plan behind it. `opinion-arbiter` already
prefers a planned acting opinion when it sources a trade-intent's
`stop_price`/`horizon_seconds` (`_planned_or_acting` there), so a confirmation
adds weight without ever being what a position is sized against.

**The verdict is measured, never asserted by the model** (the same invariant
`devils-advocate` and `brain-self-reflector` hold): whether a candidate is
confirmed or refused turns on whether real, verified-snapshot evidence exists
for the symbol -- structural, model-independent -- and, once the model answers,
on whether anything it wrote survived being checked against that evidence.
`runtime/claim_verification.py` calls that a model may phrase a claim, never
establish one; a verdict resting on whether the model could find *any* real
number worth citing is that principle applied to a yes/no rather than to
prose. The model's text never changes the reasoner's own learned conviction --
only whether there was a real number behind it, and so a case to confirm at
all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.bot_opinion import ENTER_NOW, DirectionalOpinion, stand_down
from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.learned_estimator import RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "setup-second-opinion-reasoner"
BOT = "setup-second-opinion-reasoner"

PART_DECLARATION = PartDeclaration(
    part_id="setup-second-opinion-reasoner",
    consumes=("directional-opinion", "verified-snapshot", "validated-llm-output"),
    produces=("directional-opinion", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "review-the-case-for-a-setup"

INSTRUCTION = (
    "Another bot wants to trade this. Judge whether the case for this specific setup "
    "actually holds up, using only the numbers given -- every number you write must be "
    "one of them. Cite the numbers that support confirming it, or write nothing if none do."
)

NOT_ENOUGH_EVIDENCE = "no-usable-verified-snapshot-for-this-symbol"
MODEL_FOUND_NOTHING_TO_CONFIRM = "the-model-cited-nothing-that-survived-verification"


@dataclass
class SecondOpinionStanding:
    reviews_written: int = 0
    confirmed: int = 0
    refused_for_insufficient_evidence: int = 0
    refused_after_the_model_found_nothing: int = 0
    model_sentences_removed: int = 0


class SetupSecondOpinionReasoner:
    """Confirms or refuses a candidate another bot already found -- never its own."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        self._minimum = minimum_observations
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._record = RateEstimator(
            prior=prior_hit_rate, prior_weight=prior_weight,
            half_life_observations=half_life_observations,
        )
        self._snapshots: dict[tuple[str, str], object] = {}
        self.standing = SecondOpinionStanding()

    def observe_verified_snapshot(self, snapshot) -> None:
        self._snapshots[(snapshot.venue_id, snapshot.symbol)] = snapshot

    def observe_confirmation_outcome(self, was_right: bool) -> None:
        """Whether a confirmed setup went on to be the trade worth taking.

        Exposed for whatever future part decodes a closed trade's attribution
        back to this bot's vote -- unwired today, the same honest gap
        devils-advocate's own `observe_objection_outcome` carries: a rate with
        no observations reports its prior and says so, never a guess.
        """
        self._record.observe(was_right)

    def facts_for(self, candidate: DirectionalOpinion) -> dict:
        snapshot = self._snapshots.get((candidate.venue_id, candidate.symbol))
        facts = {
            "candidate_conviction": round(candidate.conviction.value, 4),
            "candidate_conviction_is_measured": candidate.conviction.is_fitted,
        }
        if snapshot is not None:
            facts.update(snapshot.facts)
        return facts

    def request(self, candidate: DirectionalOpinion):
        return make_request(
            purpose=PURPOSE,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for(candidate),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
            asked_by=PART_ID,
        )

    def review(self, candidate: DirectionalOpinion, model_output: str | None = None) -> DirectionalOpinion:
        self.standing.reviews_written += 1
        snapshot = self._snapshots.get((candidate.venue_id, candidate.symbol))

        if snapshot is None or not snapshot.can_be_used_as_ground_truth:
            self.standing.refused_for_insufficient_evidence += 1
            return stand_down(
                BOT, candidate.side, candidate.venue_id, candidate.symbol,
                refusal=NOT_ENOUGH_EVIDENCE,
                reason=f"no usable verified snapshot for {candidate.symbol}",
                now_ns=self._now_ns,
            )

        facts = self.facts_for(candidate)
        if model_output is None:
            confidence = self._record.estimate(self._minimum)
            verified = written_without_a_model(
                f"confirming {candidate.symbol} at a learned confirmation rate of "
                f"{confidence.value:.1%}",
                facts,
            )
        else:
            # A verdict resting on whether the model could cite a real number, never
            # on what it asserted: nothing kept means nothing was found worth
            # confirming, and that is a refusal rather than a guess in its absence.
            verified = verify_against_facts(model_output, facts, self._tolerance, require_a_citation=True)
            self.standing.model_sentences_removed += len(verified.removed_sentences)
            if verified.is_empty:
                self.standing.refused_after_the_model_found_nothing += 1
                return stand_down(
                    BOT, candidate.side, candidate.venue_id, candidate.symbol,
                    refusal=MODEL_FOUND_NOTHING_TO_CONFIRM,
                    reason=f"nothing the model wrote about {candidate.symbol} survived verification",
                    now_ns=self._now_ns,
                )
            confidence = self._record.estimate(self._minimum)

        self.standing.confirmed += 1
        return DirectionalOpinion(
            bot=BOT,
            side=candidate.side,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            action=ENTER_NOW,
            conviction=confidence,
            timing=None,
            exit_plan=None,
            features_summary=dict(candidate.features_summary),
            refusal=None,
            reason=verified.text or f"confirms {candidate.bot}'s case for {candidate.symbol}",
            formed_at_ns=self._now_ns(),
        )


def describe_second_opinion(reasoner: SetupSecondOpinionReasoner) -> dict:
    return {
        "part_id": PART_ID,
        "reviews_written": reasoner.standing.reviews_written,
        "confirmed": reasoner.standing.confirmed,
        "refused_for_insufficient_evidence": reasoner.standing.refused_for_insufficient_evidence,
        "refused_after_the_model_found_nothing": reasoner.standing.refused_after_the_model_found_nothing,
        "model_sentences_removed": reasoner.standing.model_sentences_removed,
    }


def run_setup_second_opinion_reasoner(
    reasoner: SetupSecondOpinionReasoner, control_socket, read_candidates_and_output,
    publish_opinions, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        opinions = []
        requests = []
        for candidate, model_output in read_candidates_and_output(reasoner):
            if model_output is None:
                requests.append(reasoner.request(candidate))
            opinions.append(reasoner.review(candidate, model_output))
        publish_opinions(tuple(opinions))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_second_opinion(reasoner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Only a symbol with a live, acting candidate this tick is reviewed -- this
    part never goes looking for its own. When more than one bot is acting on
    the same symbol this tick, the strongest candidate is reviewed: the
    request/answer correlation is keyed by (venue_id, symbol) alone, the same
    fixed shape every brain reasoner shares, so at most one request per symbol
    can be answered back to.
    """
    from runtime.input_assembly import Batch

    candidates = Batch(read=context.bus.reader("directional-opinion"))
    snapshots = Batch(read=context.bus.reader("verified-snapshot"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_opinions = context.bus.publisher_for("directional-opinion")
    publish_requests = context.bus.publisher_for("llm-request")

    reasoner = SetupSecondOpinionReasoner(
        prior_hit_rate=context.number("brain_prior_hit_rate"),
        prior_weight=context.number("brain_prior_weight"),
        half_life_observations=context.number("brain_half_life_observations"),
        minimum_observations=int(context.number("brain_minimum_observations")),
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    pending: dict[tuple[str, str], object] = {}

    def read_candidates_and_output(active_reasoner):
        for snapshot in snapshots.payloads():
            active_reasoner.observe_verified_snapshot(snapshot)

        judgements = []
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            key = (str(value.get("venue_id", "")), str(value.get("symbol", "")))
            if key in pending:
                judgements.append((pending.pop(key), output.text))

        strongest: dict[tuple[str, str], object] = {}
        for opinion in candidates.payloads():
            if opinion.bot == BOT or not opinion.is_a_call_to_act:
                continue
            key = (opinion.venue_id, opinion.symbol)
            current = strongest.get(key)
            if current is None or opinion.conviction.value > current.conviction.value:
                strongest[key] = opinion

        for key, candidate in strongest.items():
            pending[key] = candidate
            judgements.append((candidate, None))
        return judgements

    return run_setup_second_opinion_reasoner(
        reasoner=reasoner,
        control_socket=context.control_socket,
        read_candidates_and_output=read_candidates_and_output,
        publish_opinions=publish_opinions,
        publish_requests=publish_requests,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
