"""copy-worthiness-scorer: would following this trader have paid, after the delay.

The distinction this part enforces is the one that makes copy trading mostly lose
money: **a trader's return and a copier's return are different numbers**, and they
differ by exactly the latency this system measured. Somebody up 200% whose edge
lives in the first ninety seconds after entry is not copyable at all, and scoring
them on their own return says the opposite.

So the score is computed on the copyable return -- their return minus what the
delay took -- and the pieces are kept separately so the reason is visible: what
they made, what was left after the delay, and what the delay ate.

Four refusals, each for a specific failure:

- **An unverified record is not scored.** A claimed return can be anything; scoring
  it produces a confident number about a fiction.
- **A partial book is not scored.** Half a hedge copied is worse than nothing, and
  a visible long leg looks identical to an outright long.
- **A record too short is not scored.** With four trades the copyable return is
  noise, and a scorer that ranks noise ranks whoever was luckiest.
- **An unmeasured latency is not silently treated as zero.** That is the specific
  assumption that makes every trader look copyable, so it is refused instead.

The scorer produces a number, not a decision. Whether to actually follow anybody is
the tailgating bot's judgement, made with position sizing and risk in view.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import CopyScore
from runtime.learned_estimator import RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "copy-worthiness-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="copy-worthiness-scorer",
    consumes=(
        "external-position", "tracked-trader", "trade-episode", "verified-record",
        "copy-latency",
    ),
    produces=("copy-score", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

WORTH_COPYING = "worth-copying"
NOT_WORTH_COPYING = "the-delay-eats-the-edge"
NOT_VERIFIED = "the-record-was-never-verified"
BOOK_NOT_VISIBLE = "only-part-of-the-book-can-be-seen"
LATENCY_NOT_MEASURED = "the-copying-delay-has-never-been-measured"
TOO_LITTLE_EVIDENCE = "too-few-positions-to-say-anything"


@dataclass
class ScorerStanding:
    traders_scored: int = 0
    worth_copying: int = 0
    edge_eaten_by_delay: int = 0
    refused_unverified: int = 0
    refused_partial_book: int = 0
    refused_unmeasured_latency: int = 0
    refused_thin_evidence: int = 0
    total_lost_to_latency: float = 0.0


class CopyWorthinessScorer:
    """Scores a trader on what copying them would have returned, not on their return."""

    def __init__(
        self,
        minimum_positions: int,
        worth_copying_threshold: float,
        prior_follow_success: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_follow_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_positions < 1:
            raise ValueError("scoring zero positions scores nothing")
        if minimum_follow_observations < 1:
            raise ValueError(
                "a follow-success rate fitted on zero outcomes is the prior wearing the "
                "appearance of evidence"
            )
        if worth_copying_threshold <= 0:
            raise ValueError(
                "a copyable return that is not positive after costs is not an edge"
            )
        self._minimum_positions = minimum_positions
        self._threshold = worth_copying_threshold
        self._now_ns = now_ns
        self._prior_follow_success = prior_follow_success
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum_follow_observations = minimum_follow_observations
        self._records: dict[str, object] = {}
        self._latency: dict[tuple, object] = {}
        self._positions: dict[str, list] = {}
        # Learned per trader: how often following them actually worked here (RL-060).
        self._follow_success: dict[str, RateEstimator] = {}
        self.standing = ScorerStanding()

    def observe_verified_record(self, record) -> None:
        self._records[record.trader_id] = record

    def observe_latency(self, latency) -> None:
        self._latency[(latency.venue_id, latency.symbol)] = latency

    def observe_position(self, position, realised_return: float | None = None) -> None:
        self._positions.setdefault(position.trader_id, []).append(
            (position, realised_return)
        )

    def observe_follow_outcome(self, trader_id: str, made_money: bool) -> None:
        """What happened when this system actually followed them."""
        self._follow_success.setdefault(
            trader_id,
            RateEstimator(
                prior=self._prior_follow_success,
                prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            ),
        ).observe(made_money)

    def score(self, trader_id: str, symbol: str | None = None) -> CopyScore:
        self.standing.traders_scored += 1
        record = self._records.get(trader_id)
        if record is None or record.verification_state in (
            "nothing-can-be-reconstructed", "the-positions-do-not-support-the-claim",
        ):
            self.standing.refused_unverified += 1
            return self._score(
                trader_id, symbol, 0.0, None, None, None, NOT_VERIFIED,
                "the record was never reconstructed from positions. Scoring a claimed "
                "return produces a confident number about a fiction",
            )

        entries = [
            (position, realised)
            for position, realised in self._positions.get(trader_id, [])
            if symbol is None or position.symbol == symbol
        ]
        if len(entries) < self._minimum_positions:
            self.standing.refused_thin_evidence += 1
            return self._score(
                trader_id, symbol, 0.0, record.verifiable_return, None, None,
                TOO_LITTLE_EVIDENCE,
                f"{len(entries)} position(s), below the {self._minimum_positions} bar. "
                f"A copyable return over this many trades is noise, and ranking noise "
                f"ranks whoever was luckiest",
            )

        if any(not position.can_be_reasoned_about_alone for position, _ in entries):
            self.standing.refused_partial_book += 1
            return self._score(
                trader_id, symbol, 0.0, record.verifiable_return, None, None,
                BOOK_NOT_VISIBLE,
                "only part of the book is visible. A visible long leg looks identical to "
                "an outright long, and copying half a hedge is worse than copying neither",
            )

        latencies = [
            self._latency.get((position.venue_id, position.symbol))
            for position, _ in entries
        ]
        if any(latency is None or not latency.is_measured for latency in latencies):
            self.standing.refused_unmeasured_latency += 1
            return self._score(
                trader_id, symbol, 0.0, record.verifiable_return, None, None,
                LATENCY_NOT_MEASURED,
                "the copying delay has never been measured for at least one of these "
                "symbols. Treating an unmeasured delay as zero is the assumption that "
                "makes every trader look copyable",
            )

        their_return = 0.0
        copyable_return = 0.0
        for (position, realised), latency in zip(entries, latencies):
            if realised is None:
                continue
            their_return += realised
            copyable_return += realised - latency.adverse_move_fraction

        lost = their_return - copyable_return
        self.standing.total_lost_to_latency += lost

        success = self._follow_success.get(trader_id)
        # The learned term is a multiplier on the arithmetic, never a substitute for
        # it: a trader followed successfully three times is still not copyable if the
        # delay eats the move.
        follow_estimate = (
            success.estimate(self._minimum_follow_observations) if success else None
        )
        confidence = (
            follow_estimate.value if follow_estimate else self._prior_follow_success
        )
        score = copyable_return * confidence

        if copyable_return < self._threshold:
            self.standing.edge_eaten_by_delay += 1
            return self._score(
                trader_id, symbol, score, their_return, copyable_return, lost,
                NOT_WORTH_COPYING,
                f"they made {their_return:+.2%}; {lost:.2%} of it is gone by the time this "
                f"system can act, leaving {copyable_return:+.2%}. Their edge lives in the "
                f"window this system cannot reach",
            )

        self.standing.worth_copying += 1
        return self._score(
            trader_id, symbol, score, their_return, copyable_return, lost, WORTH_COPYING,
            f"{copyable_return:+.2%} survives the delay out of {their_return:+.2%}"
            + (
                f", and following them has worked {confidence:.0%} of "
                f"{success.observations} time(s) here"
                if follow_estimate and follow_estimate.is_fitted
                else ", with no follow history here yet"
            ),
        )

    def _score(
        self, trader_id, symbol, score, their_return, copyable, lost, state, reason,
    ) -> CopyScore:
        return CopyScore(
            trader_id=trader_id, symbol=symbol, score=score, their_return=their_return,
            copyable_return=copyable, lost_to_latency=lost, state=state, reason=reason,
            scored_at_ns=self._now_ns(),
        )


def describe_copy_scoring(scorer: CopyWorthinessScorer) -> dict:
    return {
        "part_id": PART_ID,
        "traders_scored": scorer.standing.traders_scored,
        "worth_copying": scorer.standing.worth_copying,
        "edge_eaten_by_delay": scorer.standing.edge_eaten_by_delay,
        "refused_unverified": scorer.standing.refused_unverified,
        "refused_partial_book": scorer.standing.refused_partial_book,
        "refused_unmeasured_latency": scorer.standing.refused_unmeasured_latency,
        "refused_thin_evidence": scorer.standing.refused_thin_evidence,
        "total_return_lost_to_latency": scorer.standing.total_lost_to_latency,
        "scores_on_their_return": False,
        "decides_whether_to_follow": False,
    }


def run_copy_worthiness_scorer(
    scorer: CopyWorthinessScorer, control_socket, read_candidates, publish_scores,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trader_id, symbol in read_candidates(scorer):
            publish_scores(scorer.score(trader_id, symbol))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
