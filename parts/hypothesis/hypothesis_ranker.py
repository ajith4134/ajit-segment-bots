"""hypothesis-ranker: which hypothesis to spend the next trades on.

There are always more hypotheses than trades to test them with, and the order
decides what this system learns. Ranking by expected edge alone is the obvious
choice and the wrong one -- it always picks the largest claim, and the largest
claim is usually the one with the least evidence behind it.

So the ranking weighs four things that pull in different directions:

- **Expected edge**, from the expectancy breakdown. What it would be worth if
  true.
- **What it costs to find out**, from the required sample size. A hypothesis
  claiming a huge edge that needs forty thousand trades is worth less than a
  modest one that needs four hundred, because the second gets answered.
- **Novelty.** A near-duplicate of something already tested buys almost no
  information whatever its claimed edge.
- **How fast its edge would decay.** An edge with a forty-trade half-life is
  barely worth confirming; by the time it is confirmed it has gone.

**The ranking is by information per trade**, which is what all four combine into:
how much this system would learn per unit of the scarce thing.

**A hypothesis whose sample size is unreachable is ranked last, not excluded.**
Excluding it hides it; ranking it last means it appears the moment the system can
produce more trades, which it might.

**Ties are broken toward the older hypothesis.** Otherwise a stream of new ideas
starves everything already queued, and the queue becomes a stack.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "hypothesis-ranker"

PART_DECLARATION = PartDeclaration(
    part_id="hypothesis-ranker",
    consumes=(
        "expectancy-breakdown", "instruction-scorecard", "exit-quality", "edge-half-life",
        "novelty-score", "required-sample-size",
    ),
    produces=("hypothesis-priority", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RANKED = "ranked"
UNREACHABLE = "its-sample-size-is-more-than-this-system-can-produce"
NOT_ENOUGH_INPUTS = "too-little-is-known-about-it-to-rank-it"


@dataclass(frozen=True)
class HypothesisPriority:
    """Where a hypothesis sits in the queue, and what put it there."""

    hypothesis_id: str
    state: str
    rank: int | None
    information_per_trade: float | None
    expected_edge: float | None
    trades_required: int | None
    novelty: float | None
    half_life_trades: float | None
    proposed_at_ns: int
    reason: str
    ranked_at_ns: int

    @property
    def is_ranked(self) -> bool:
        return self.state == RANKED


@dataclass
class RankerStanding:
    rankings: int = 0
    hypotheses_ranked: int = 0
    unreachable: int = 0
    not_enough_inputs: int = 0
    starved_by_ties: int = 0
    highest_information_per_trade: float | None = None


class HypothesisRanker:
    """Ranks by information per trade, which is what the four inputs combine into."""

    def __init__(
        self,
        maximum_reachable_trades: int,
        minimum_half_life_trades: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_reachable_trades < 1:
            raise ValueError("a system that can produce no trades ranks nothing")
        if minimum_half_life_trades <= 0:
            raise ValueError(
                "an edge with no half-life bound is one this ranker would confirm after it "
                "had already gone"
            )
        self._maximum = maximum_reachable_trades
        self._minimum_half_life = minimum_half_life_trades
        self._now_ns = now_ns
        self._edges: dict[str, float] = {}
        self._samples: dict[str, int] = {}
        self._novelty: dict[str, float] = {}
        self._half_lives: dict[str, float] = {}
        self._proposed_at: dict[str, int] = {}
        self.standing = RankerStanding()

    def observe_expected_edge(self, hypothesis_id: str, edge: float, proposed_at_ns: int = 0) -> None:
        self._edges[hypothesis_id] = edge
        self._proposed_at.setdefault(hypothesis_id, proposed_at_ns)

    def observe_required_sample(self, hypothesis_id: str, trades: int) -> None:
        self._samples[hypothesis_id] = trades

    def observe_novelty(self, hypothesis_id: str, novelty: float) -> None:
        self._novelty[hypothesis_id] = novelty

    def observe_edge_half_life(self, hypothesis_id: str, half_life_trades: float) -> None:
        self._half_lives[hypothesis_id] = half_life_trades

    def information_per_trade(self, hypothesis_id: str) -> float | None:
        """What this system would learn per unit of the scarce thing.

        Edge times novelty, divided by what it costs to find out, discounted by
        how much of the edge survives that long.
        """
        edge = self._edges.get(hypothesis_id)
        trades = self._samples.get(hypothesis_id)
        if edge is None or trades is None or trades <= 0:
            return None

        novelty = self._novelty.get(hypothesis_id, 1.0)
        half_life = self._half_lives.get(hypothesis_id)

        # How much of the edge is still there once it has been confirmed. An
        # edge with a forty-trade half-life is barely worth confirming.
        survival = 1.0 if half_life is None else 0.5 ** (trades / max(1e-9, half_life))

        return abs(edge) * novelty * survival / trades

    def rank(self, hypothesis_ids) -> tuple:
        """The whole queue, in the order the next trades should be spent."""
        self.standing.rankings += 1
        scored = []
        unreachable = []
        unrankable = []

        for hypothesis_id in hypothesis_ids:
            trades = self._samples.get(hypothesis_id)
            information = self.information_per_trade(hypothesis_id)

            if information is None:
                self.standing.not_enough_inputs += 1
                unrankable.append(hypothesis_id)
                continue

            if trades is not None and trades > self._maximum:
                # Ranked last rather than excluded: excluding hides it, and it
                # becomes rankable the moment this system can produce more.
                self.standing.unreachable += 1
                unreachable.append((hypothesis_id, information))
                continue

            scored.append((hypothesis_id, information))

        # Ties toward the older hypothesis: otherwise a stream of new ideas
        # starves everything already queued and the queue becomes a stack.
        scored.sort(
            key=lambda entry: (-entry[1], self._proposed_at.get(entry[0], 0))
        )
        unreachable.sort(key=lambda entry: -entry[1])

        priorities = []
        rank = 1
        for hypothesis_id, information in scored:
            if (
                self.standing.highest_information_per_trade is None
                or information > self.standing.highest_information_per_trade
            ):
                self.standing.highest_information_per_trade = information
            priorities.append(self._priority(hypothesis_id, RANKED, rank, information))
            rank += 1

        for hypothesis_id, information in unreachable:
            priorities.append(self._priority(hypothesis_id, UNREACHABLE, rank, information))
            rank += 1

        for hypothesis_id in unrankable:
            priorities.append(self._priority(hypothesis_id, NOT_ENOUGH_INPUTS, None, None))

        self.standing.hypotheses_ranked += len(scored)
        return tuple(priorities)

    def _priority(self, hypothesis_id, state, rank, information) -> HypothesisPriority:
        edge = self._edges.get(hypothesis_id)
        trades = self._samples.get(hypothesis_id)
        novelty = self._novelty.get(hypothesis_id)
        half_life = self._half_lives.get(hypothesis_id)

        if state == RANKED:
            reason = (
                f"rank {rank}: a {edge:+.1%} claimed edge needing {trades:,} trade(s), "
                f"novelty {novelty if novelty is not None else 1.0:.2f}"
                + (
                    f", half-life {half_life:.0f} trade(s)"
                    if half_life is not None
                    else ", with no measured decay"
                )
                + f" -- {information:.3e} of information per trade. Ranked by that rather than "
                f"by edge, because ranking on edge alone always picks the largest claim, and "
                f"the largest claim usually has the least evidence"
            )
        elif state == UNREACHABLE:
            reason = (
                f"{trades:,} trade(s) is past the {self._maximum:,} this system can produce. "
                f"Ranked last rather than excluded, so it reappears if this system can ever "
                f"produce more"
            )
        else:
            reason = (
                "its expected edge or required sample size is not known, so nothing can be "
                "said about what testing it would buy"
            )

        return HypothesisPriority(
            hypothesis_id=hypothesis_id,
            state=state,
            rank=rank,
            information_per_trade=information,
            expected_edge=edge,
            trades_required=trades,
            novelty=novelty,
            half_life_trades=half_life,
            proposed_at_ns=self._proposed_at.get(hypothesis_id, 0),
            reason=reason,
            ranked_at_ns=self._now_ns(),
        )


def describe_ranking(ranker: HypothesisRanker) -> dict:
    return {
        "part_id": PART_ID,
        "rankings": ranker.standing.rankings,
        "hypotheses_ranked": ranker.standing.hypotheses_ranked,
        "unreachable": ranker.standing.unreachable,
        "not_enough_inputs": ranker.standing.not_enough_inputs,
        "highest_information_per_trade": ranker.standing.highest_information_per_trade,
        "maximum_reachable_trades": ranker._maximum,
        "ranks_by": "information-per-trade",
    }


def run_hypothesis_ranker(
    ranker: HypothesisRanker, control_socket, read_inputs, publish_priorities,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        hypothesis_ids = read_inputs(ranker)
        publish_priorities(ranker.rank(hypothesis_ids))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
