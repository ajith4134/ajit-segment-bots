"""strategy-review-reasoner: which of this system's own bots or detectors is
working, and why -- the first part built from docs/proposals/llm-reasoning-gets-a-vote.md
(RL-010/013/026: real intelligence, not a hardcoded threshold).

**Advisory only, no vote.** `opinion-arbiter` folds this in as a per-bot trust
discount, the same shape as `competence-map` (a gate) and `conflict-ruling` (a
filter) -- never a new opinion. This part is not making a per-symbol call, so
it has nothing to vote on; the proposal reserves a real vote for
`setup-second-opinion-reasoner` and `market-thesis-reasoner`, built after this
one has run and been measured.

**A bot's record, not a symbol's.** `bot-scorecard` is the whole record; this
part learns a recency-weighted hit rate off it the same way `devils-advocate`
learns an objection's hit rate, and asks a model only to phrase why, checked
against the same numbers (`runtime/claim_verification.py`) -- a model may
phrase a claim, never establish one.

**Reviewed when there is something new to say, not on a fixed clock.** A bot
is not re-reviewed until `strategy_review_minimum_new_trades` more of its
trades have closed; reviewing on every scorecard heartbeat would ask a model
the same question with the same answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import UNDERPERFORMING, UNMEASURED, WORKING, StrategyReview

PART_ID = "strategy-review-reasoner"

PART_DECLARATION = PartDeclaration(
    part_id="strategy-review-reasoner",
    consumes=("competence-map", "bot-scorecard", "closed-trade", "validated-llm-output"),
    produces=("strategy-review", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PURPOSE = "review-a-bots-own-record"

INSTRUCTION = (
    "This is one bot's measured trading record and the system's current competence "
    "context. Judge whether this bot is working and say why, using only the numbers "
    "given -- every number you write must be one of them."
)

# A subject of a bot review is the whole system, not one venue -- the fixed
# LlmRequest/CounterArgument-family shape needs a venue_id and a symbol to key
# a request against its later answer, so this repurposes symbol to hold the bot
# id and venue_id to a fixed sentinel naming what it stands for.
SYSTEM_VENUE = "system"


@dataclass
class ReviewStanding:
    reviews_written: int = 0
    working: int = 0
    underperforming: int = 0
    unmeasured: int = 0
    model_sentences_removed: int = 0


class StrategyReviewReasoner:
    """Learns a per-bot hit rate off its scorecard, judges it, asks a model why."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        working_threshold: float,
        minimum_new_trades: int,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < working_threshold < 1.0:
            raise ValueError("working_threshold is a fraction and must be inside (0, 1)")
        if minimum_new_trades < 1:
            raise ValueError("a review needs at least one new closed trade to say anything new")
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._working_threshold = working_threshold
        self._minimum_new_trades = minimum_new_trades
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._records: dict[str, RateEstimator] = {}
        self._last_seen: dict[str, tuple[int, int]] = {}
        self._closed_trades_since_review = 0
        self._best_competence: tuple[str, float] | None = None
        self._worst_competence: tuple[str, float] | None = None
        self.standing = ReviewStanding()

    def observe_closed_trade(self) -> None:
        self._closed_trades_since_review += 1

    def observe_competence_map(self, mapped) -> None:
        self._best_competence = (
            (mapped.best.symbol, mapped.best.competence)
            if mapped.best is not None and mapped.best.competence is not None
            else None
        )
        self._worst_competence = (
            (mapped.worst.symbol, mapped.worst.competence)
            if mapped.worst is not None and mapped.worst.competence is not None
            else None
        )

    def _record_for(self, bot: str) -> RateEstimator:
        record = self._records.get(bot)
        if record is None:
            record = RateEstimator(
                prior=self._prior_hit_rate,
                prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._records[bot] = record
        return record

    def due_for_review(self, bot: str, trades: int) -> bool:
        last_trades, _ = self._last_seen.get(bot, (0, 0))
        return (trades - last_trades) >= self._minimum_new_trades

    def facts_for(self, bot: str, trades: int, wins: int, realised: float, confidence: Estimate) -> dict:
        facts = {
            "trades": trades,
            "wins": wins,
            "win_rate": round(wins / trades, 4) if trades else 0.0,
            "realised": round(realised, 6),
            "learned_hit_rate": round(confidence.value, 4),
            "hit_rate_observations": confidence.observations,
            "closed_trades_system_wide_since_last_review": self._closed_trades_since_review,
        }
        if self._best_competence is not None:
            facts["system_best_competence"] = round(self._best_competence[1], 4)
        if self._worst_competence is not None:
            facts["system_worst_competence"] = round(self._worst_competence[1], 4)
        return facts

    def assessment_for(self, confidence: Estimate) -> str:
        if not confidence.is_fitted:
            return UNMEASURED
        return WORKING if confidence.value >= self._working_threshold else UNDERPERFORMING

    def prepare_review(self, bot: str, trades: int, wins: int, realised: float) -> tuple[dict, str, Estimate]:
        """Learns from the new trades, judges the bot, and freezes the facts the
        request and its later verification must both agree on."""
        last_trades, last_wins = self._last_seen.get(bot, (0, 0))
        new_trades = max(0, trades - last_trades)
        new_wins = max(0, wins - last_wins)
        record = self._record_for(bot)
        for _ in range(new_wins):
            record.observe(True)
        for _ in range(new_trades - new_wins):
            record.observe(False)
        self._last_seen[bot] = (trades, wins)

        confidence = record.estimate(self._minimum)
        assessment = self.assessment_for(confidence)
        facts = self.facts_for(bot, trades, wins, realised, confidence)
        self._closed_trades_since_review = 0
        return facts, assessment, confidence

    def request(self, bot: str, facts: dict):
        return make_request(
            purpose=PURPOSE,
            venue_id=SYSTEM_VENUE,
            symbol=bot,
            instruction=INSTRUCTION,
            facts=facts,
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def review(
        self, bot: str, facts: dict, assessment: str, confidence: Estimate, model_output: str | None = None,
    ) -> StrategyReview:
        self.standing.reviews_written += 1
        if model_output is None:
            verified = written_without_a_model(
                f"{bot} is judged {assessment} at a learned hit rate of {confidence.value:.1%}", facts,
            )
        else:
            # A review is entirely about a bot's numbers, unlike a premortem's failure
            # modes -- an unfalsifiable sentence here is the one a reader would trust
            # hardest, so it is removed rather than kept.
            verified = verify_against_facts(model_output, facts, self._tolerance, require_a_citation=True)
            self.standing.model_sentences_removed += len(verified.removed_sentences)

        if assessment == WORKING:
            self.standing.working += 1
        elif assessment == UNDERPERFORMING:
            self.standing.underperforming += 1
        else:
            self.standing.unmeasured += 1

        return StrategyReview(
            bot=bot,
            assessment=assessment,
            confidence=confidence,
            reason=verified.text or assessment,
            formed_at_ns=self._now_ns(),
        )


def describe_review(reasoner: StrategyReviewReasoner) -> dict:
    return {
        "part_id": PART_ID,
        "reviews_written": reasoner.standing.reviews_written,
        "working": reasoner.standing.working,
        "underperforming": reasoner.standing.underperforming,
        "unmeasured": reasoner.standing.unmeasured,
        "model_sentences_removed": reasoner.standing.model_sentences_removed,
        "bots_with_a_record": len(reasoner._records),
    }


def run_strategy_review_reasoner(
    reasoner: StrategyReviewReasoner, control_socket, read_reviews_and_output,
    publish_reviews, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        reviews = []
        requests = []
        for (bot, facts, assessment, confidence), model_output in read_reviews_and_output(reasoner):
            if model_output is None:
                requests.append(reasoner.request(bot, facts))
            reviews.append(reasoner.review(bot, facts, assessment, confidence, model_output))
        publish_reviews(tuple(reviews))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_review(reasoner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A bot's scorecard is cumulative, so `prepare_review` learns off the delta
    since the bot's last review rather than replaying its whole history each
    time. `closed-trade` and `competence-map` are drained into side context
    used only to enrich the facts a review carries, never to key one -- a bot's
    own scorecard is the only thing that decides whether it is due.
    """
    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    closed_trades = Batch(read=context.bus.reader("closed-trade"))
    competences = Batch(read=context.bus.reader("competence-map"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_reviews = context.bus.publisher_for("strategy-review")
    publish_requests = context.bus.publisher_for("llm-request")

    reasoner = StrategyReviewReasoner(
        prior_hit_rate=context.number("brain_prior_hit_rate"),
        prior_weight=context.number("brain_prior_weight"),
        half_life_observations=context.number("brain_half_life_observations"),
        minimum_observations=int(context.number("brain_minimum_observations")),
        working_threshold=context.number("strategy_review_working_threshold"),
        minimum_new_trades=int(context.number("strategy_review_minimum_new_trades")),
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    pending: dict[tuple[str, str], tuple] = {}

    def totals_from(card) -> tuple[int, int, float]:
        described = card.describe()
        trades = wins = 0
        realised = 0.0
        for entry in described["by_detector"].values():
            trades += entry["trades"]
            wins += entry["wins"]
            realised += entry["realised"]
        return trades, wins, realised

    def read_reviews_and_output(active_reasoner):
        for mapped in competences.payloads():
            active_reasoner.observe_competence_map(mapped)
        for _ in closed_trades.payloads():
            active_reasoner.observe_closed_trade()

        judgements = []
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            key = (str(value.get("venue_id", "")), str(value.get("symbol", "")))
            if key in pending:
                judgements.append((pending.pop(key), output.text))

        for card in scorecards.payloads():
            trades, wins, realised = totals_from(card)
            if not active_reasoner.due_for_review(card.bot, trades):
                continue
            facts, assessment, confidence = active_reasoner.prepare_review(
                card.bot, trades, wins, realised
            )
            key = (SYSTEM_VENUE, card.bot)
            pending[key] = (card.bot, facts, assessment, confidence)
            judgements.append((pending[key], None))
        return judgements

    return run_strategy_review_reasoner(
        reasoner=reasoner,
        control_socket=context.control_socket,
        read_reviews_and_output=read_reviews_and_output,
        publish_reviews=publish_reviews,
        publish_requests=publish_requests,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
