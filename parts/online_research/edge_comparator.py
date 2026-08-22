"""edge-comparator: where somebody else's results beat this system's, and why.

Comparing edges across systems is mostly a way to generate false conclusions,
because the two sides are measured differently by default. A public trader's return
is gross, self-selected, in whatever currency flatters it, over a window they chose.
This system's edge is net of fees and slippage, over every trade it took, in USDT
(RL-028). Subtracting one from the other produces a number that means nothing.

So the comparator normalises before it compares, and refuses when it cannot:

- **Same period or no comparison.** An overlapping window is required, because a
  bull month against a chop month compares the market, not the traders.
- **Same symbols or a named difference.** If they traded three symbols this system
  never looks at, that is not a worse edge -- it is a different universe, and the
  universe is the finding.
- **Net against net.** Their return has costs subtracted using this system's own
  measured fee and slippage model, since their gross number is the one they publish.
- **Enough trades on both sides.** A difference measured over four of their trades
  and nine hundred of this system's is a difference in sample size.

A gap is only worth raising if it is reachable. "They trade a venue this system has
no account on" is real and useless; "they take a setup this scanner never looks
for" is real and actionable. The comparator says which, and names the blocker when
the answer is the first one -- because an unreachable gap raised repeatedly is how a
backlog fills with work nobody can do.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.external_research_types import StrategyGap
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "edge-comparator"

PART_DECLARATION = PartDeclaration(
    part_id="edge-comparator",
    consumes=("research-finding", "trade-episode"),
    produces=("strategy-gap", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

GAP_FOUND = "they-do-something-this-system-does-not"
NO_GAP = "no-difference-worth-raising"
WE_ARE_AHEAD = "this-system-does-better"
NO_OVERLAP = "the-periods-do-not-overlap"
NOT_COMPARABLE = "the-two-sides-are-not-measured-the-same-way"
TOO_FEW_TRADES = "too-few-trades-on-one-side"

# Why a real gap cannot be acted on here. Each is a fact about this system, not
# about the finding, and naming it prevents the same gap being raised forever.
NO_VENUE_ACCESS = "this-system-has-no-account-on-that-venue"
NO_INSTRUMENT = "that-instrument-type-is-not-traded-here"
NO_CAPITAL_SCALE = "the-edge-only-exists-at-a-size-this-system-does-not-run"
NO_LATENCY = "the-edge-lives-inside-a-delay-this-system-cannot-reach"


@dataclass(frozen=True)
class EdgeComparison:
    """One side-by-side, with everything that had to be equalised first."""

    subject: str
    state: str
    their_edge: float | None
    our_edge: float | None
    difference: float | None
    overlapping_days: float
    their_trades: int
    our_trades: int
    shared_symbols: tuple
    their_only_symbols: tuple
    gap: StrategyGap | None
    reason: str
    compared_at_ns: int


@dataclass
class ComparatorStanding:
    comparisons: int = 0
    gaps_found: int = 0
    reachable_gaps: int = 0
    unreachable_gaps: int = 0
    refused_no_overlap: int = 0
    refused_thin_sample: int = 0
    times_this_system_was_ahead: int = 0
    costs_subtracted_from_their_side: int = 0


class EdgeComparator:
    """Normalises two records onto the same basis, then reports what differs."""

    def __init__(
        self,
        minimum_trades_each_side: int,
        minimum_overlapping_days: float,
        material_difference: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_trades_each_side < 2:
            raise ValueError("a difference over one trade on a side is that trade")
        if minimum_overlapping_days <= 0:
            raise ValueError(
                "without an overlapping window the comparison is between two markets"
            )
        if material_difference <= 0:
            raise ValueError(
                "a difference threshold of zero makes every rounding error a gap"
            )
        self._minimum_trades = minimum_trades_each_side
        self._minimum_overlap = minimum_overlapping_days
        self._material_difference = material_difference
        self._now_ns = now_ns
        self._their_trades: dict[str, list] = {}
        self._our_trades: list = []
        self._cost_model = None
        self._blockers: dict[str, str] = {}
        self._raised: set = set()
        self.standing = ComparatorStanding()

    def install_cost_model(self, cost_of) -> None:
        """`cost_of(venue_id, symbol, notional) -> fraction`, this system's own.

        Their published return is gross. Netting it uses the costs measured here,
        because those are the costs this system would actually have paid.
        """
        self._cost_model = cost_of

    def declare_blocker(self, description: str, blocker: str) -> None:
        """Something this system cannot reach, named once rather than rediscovered."""
        self._blockers[description] = blocker

    def observe_their_trade(
        self, subject: str, venue_id: str, symbol: str, gross_return: float,
        notional: float | None, opened_at_ns: int, closed_at_ns: int,
    ) -> None:
        self._their_trades.setdefault(subject, []).append(
            {
                "venue_id": venue_id, "symbol": symbol, "gross_return": gross_return,
                "notional": notional, "opened_at_ns": opened_at_ns,
                "closed_at_ns": closed_at_ns,
            }
        )

    def observe_our_episode(self, episode) -> None:
        """A trade this system actually took, already net (RL-028, in USDT)."""
        self._our_trades.append(episode)

    def compare(self, subject: str, description: str | None = None) -> EdgeComparison:
        self.standing.comparisons += 1
        theirs = self._their_trades.get(subject, [])
        ours = self._our_trades

        if len(theirs) < self._minimum_trades or len(ours) < self._minimum_trades:
            self.standing.refused_thin_sample += 1
            return self._comparison(
                subject, TOO_FEW_TRADES, None, None, 0.0, len(theirs), len(ours), (), (),
                None,
                f"{len(theirs)} of their trade(s) against {len(ours)} of this system's, "
                f"below the {self._minimum_trades} bar on at least one side. A difference "
                f"measured across unequal samples is a difference in sample size",
            )

        window = self._overlap(theirs, ours)
        if window < self._minimum_overlap:
            self.standing.refused_no_overlap += 1
            return self._comparison(
                subject, NO_OVERLAP, None, None, window, len(theirs), len(ours), (), (),
                None,
                f"the records overlap by {window:.1f} day(s). Comparing a bull month with "
                f"a chop month compares the market, not the traders",
            )

        their_symbols = {trade["symbol"] for trade in theirs}
        our_symbols = {episode.symbol for episode in ours}
        shared = tuple(sorted(their_symbols & our_symbols))
        their_only = tuple(sorted(their_symbols - our_symbols))

        if not shared:
            gap = self._gap(
                subject,
                description or f"they trade {', '.join(their_only)} and this system does not",
                None, None, len(theirs),
            )
            return self._comparison(
                subject, GAP_FOUND, None, None, window, len(theirs), len(ours),
                shared, their_only, gap,
                f"no shared symbol. That is not a worse edge, it is a different universe, "
                f"and the universe is the finding",
            )

        their_net = self._their_net_edge(theirs, shared)
        our_net = self._our_net_edge(ours, shared)
        if their_net is None or our_net is None:
            return self._comparison(
                subject, NOT_COMPARABLE, their_net, our_net, window, len(theirs),
                len(ours), shared, their_only, None,
                "one side could not be put on a net basis. Their published number is "
                "gross and this system's is net, so subtracting them directly is arithmetic "
                "on two different quantities",
            )

        difference = their_net - our_net
        if difference <= -self._material_difference:
            self.standing.times_this_system_was_ahead += 1
            return self._comparison(
                subject, WE_ARE_AHEAD, their_net, our_net, window, len(theirs), len(ours),
                shared, their_only, None,
                f"{our_net:+.2%} here against {their_net:+.2%} net for them over the same "
                f"{window:.1f} day(s) and {len(shared)} shared symbol(s)",
            )

        if difference < self._material_difference:
            return self._comparison(
                subject, NO_GAP, their_net, our_net, window, len(theirs), len(ours),
                shared, their_only, None,
                f"{difference:+.2%} apart, inside the {self._material_difference:.2%} "
                f"threshold. A difference this size is noise wearing a conclusion",
            )

        gap = self._gap(
            subject,
            description or f"they earn more on {', '.join(shared)} than this system does",
            their_net, our_net, len(theirs),
        )
        return self._comparison(
            subject, GAP_FOUND, their_net, our_net, window, len(theirs), len(ours),
            shared, their_only, gap,
            f"{their_net:+.2%} net for them against {our_net:+.2%} here, "
            f"{difference:+.2%} apart on {len(shared)} shared symbol(s)"
            + (
                f". Unreachable: {gap.blocked_by}"
                if not gap.is_reachable_here
                else ". Reachable here, so it is worth a hypothesis"
            ),
        )

    def _gap(self, subject, description, their_edge, our_edge, evidence) -> StrategyGap:
        blocker = self._blockers.get(description)
        gap = StrategyGap(
            gap_id=f"gap:{subject}:{description}",
            description=description,
            their_edge=their_edge,
            our_edge=our_edge,
            difference=(
                their_edge - our_edge
                if their_edge is not None and our_edge is not None
                else None
            ),
            is_reachable_here=blocker is None,
            blocked_by=blocker,
            evidence_count=evidence,
            found_at_ns=self._now_ns(),
        )
        self.standing.gaps_found += 1
        if gap.is_reachable_here:
            self.standing.reachable_gaps += 1
        else:
            self.standing.unreachable_gaps += 1
        self._raised.add(gap.gap_id)
        return gap

    def _their_net_edge(self, trades, shared) -> float | None:
        relevant = [trade for trade in trades if trade["symbol"] in shared]
        if not relevant:
            return None
        net = []
        for trade in relevant:
            cost = 0.0
            if self._cost_model is not None:
                cost = self._cost_model(
                    trade["venue_id"], trade["symbol"], trade["notional"] or 0.0
                )
                self.standing.costs_subtracted_from_their_side += 1
            net.append(trade["gross_return"] - cost)
        return statistics.mean(net)

    def _our_net_edge(self, episodes, shared) -> float | None:
        relevant = [episode for episode in episodes if episode.symbol in shared]
        if not relevant:
            return None
        return statistics.mean(episode.realised for episode in relevant)

    def _overlap(self, theirs, ours) -> float:
        their_start = min(trade["opened_at_ns"] for trade in theirs)
        their_end = max(trade["closed_at_ns"] for trade in theirs)
        our_start = min(episode.opened_at_ns for episode in ours)
        our_end = max(episode.closed_at_ns for episode in ours)
        overlap = min(their_end, our_end) - max(their_start, our_start)
        return max(overlap, 0) / 86_400e9

    def _comparison(
        self, subject, state, their_edge, our_edge, window, their_trades, our_trades,
        shared, their_only, gap, reason,
    ) -> EdgeComparison:
        return EdgeComparison(
            subject=subject, state=state, their_edge=their_edge, our_edge=our_edge,
            difference=(
                their_edge - our_edge
                if their_edge is not None and our_edge is not None
                else None
            ),
            overlapping_days=window, their_trades=their_trades, our_trades=our_trades,
            shared_symbols=shared, their_only_symbols=their_only, gap=gap, reason=reason,
            compared_at_ns=self._now_ns(),
        )


def describe_edge_comparison(comparator: EdgeComparator) -> dict:
    return {
        "part_id": PART_ID,
        "comparisons": comparator.standing.comparisons,
        "gaps_found": comparator.standing.gaps_found,
        "reachable_gaps": comparator.standing.reachable_gaps,
        "unreachable_gaps": comparator.standing.unreachable_gaps,
        "refused_no_overlapping_period": comparator.standing.refused_no_overlap,
        "refused_thin_sample": comparator.standing.refused_thin_sample,
        "times_this_system_was_ahead": comparator.standing.times_this_system_was_ahead,
        "costs_subtracted_from_their_side": (
            comparator.standing.costs_subtracted_from_their_side
        ),
        "compares_gross_against_net": False,
    }


def run_edge_comparator(
    comparator: EdgeComparator, control_socket, read_subjects, publish_gaps,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for subject, description in read_subjects(comparator):
            comparison = comparator.compare(subject, description)
            if comparison.gap is not None:
                publish_gaps(comparison.gap)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
