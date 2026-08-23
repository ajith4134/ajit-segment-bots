"""allocation-rebalance-proposer: propose moving allocation toward what earns it.

**Proposed only.** This part cannot move a single unit of capital; the operator
edits the settings file. That is not timidity -- it is the boundary between a
system that optimises within its allowance and one that decides its own
allowance, and this project keeps the operator on the right side of it.

What it proposes from, and why each is needed:

- **Realised USDT result**, not unrealised. An open position's paper gain is a
  claim, and allocating against claims is how a system doubles down on a trade
  that has not finished going wrong.
- **Return on the capital actually used**, not absolute profit. A segment earning
  100 on 1,000 is doing better work than one earning 150 on 10,000, and absolute
  profit would move capital toward the second.
- **Utilisation.** A segment earning well on capital it barely touches does not
  need more; a segment at its ceiling with a good return is the one starved.

Two guards against reacting to noise: nothing is proposed for a segment with too
few closed trades to distinguish skill from luck, and no proposal moves more than
a bounded fraction of an allocation at once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "allocation-rebalance-proposer"

PART_DECLARATION = PartDeclaration(
    part_id="allocation-rebalance-proposer",
    consumes=("usdt-pnl-statement", "capital-utilisation", "bot-scorecard"),
    produces=("allocation-proposal", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROPOSED = "proposed"
NO_CHANGE = "no-change-worth-proposing"
TOO_FEW_TRADES = "too-few-closed-trades-to-judge"


@dataclass(frozen=True)
class SegmentPerformance:
    """What one segment has actually earned, and on what."""

    segment: str
    realised_usdt: float
    capital_used: float
    closed_trades: int
    utilisation: float

    @property
    def return_on_capital(self) -> float | None:
        return self.realised_usdt / self.capital_used if self.capital_used > 0 else None


@dataclass(frozen=True)
class AllocationProposal:
    """A suggested allocation for one segment, for the operator to accept or ignore."""

    segment: str
    current_allocation: float
    proposed_allocation: float
    change: float
    outcome: str
    return_on_capital: float | None
    utilisation: float
    closed_trades: int
    reason: str
    proposed_at_ns: int

    @property
    def is_actionable(self) -> bool:
        return self.outcome == PROPOSED and self.change != 0.0


@dataclass
class ProposerStanding:
    rounds: int = 0
    proposals: int = 0
    held_for_too_few_trades: int = 0
    no_change: int = 0
    largest_proposed_move: float = 0.0
    segments_seen: int = 0


class AllocationRebalanceProposer:
    """Suggests where capital would work harder, and never moves any of it."""

    def __init__(
        self,
        minimum_closed_trades: int,
        maximum_move_fraction: float,
        utilisation_floor: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_move_fraction <= 1.0:
            raise ValueError("a move must be a fraction of the allocation in (0, 1]")
        self._minimum_trades = minimum_closed_trades
        self._maximum_move = maximum_move_fraction
        self._utilisation_floor = utilisation_floor
        self._now_ns = now_ns
        self._performance: dict[str, SegmentPerformance] = {}
        self._allocations: dict[str, float] = {}
        self.standing = ProposerStanding()

    def observe_performance(self, performance: SegmentPerformance) -> None:
        self._performance[performance.segment] = performance
        self.standing.segments_seen = len(self._performance)

    def set_allocation(self, segment: str, allocation: float) -> None:
        self._allocations[segment] = allocation

    def propose(self) -> tuple[AllocationProposal, ...]:
        """One proposal per segment, summing to the same total that exists now.

        Summing to the same total matters: this part reallocates what the
        operator has already committed and never proposes that more money be
        added, which is a decision it has no standing to make.
        """
        self.standing.rounds += 1
        judgeable = {
            segment: performance
            for segment, performance in self._performance.items()
            if performance.closed_trades >= self._minimum_trades
            and performance.return_on_capital is not None
        }

        proposals: list[AllocationProposal] = []
        for segment, performance in sorted(self._performance.items()):
            current = self._allocations.get(segment, 0.0)
            if segment not in judgeable:
                self.standing.held_for_too_few_trades += 1
                proposals.append(
                    self._proposal(
                        segment, current, current, TOO_FEW_TRADES, performance,
                        f"{performance.closed_trades} closed trade(s), fewer than the "
                        f"{self._minimum_trades} needed to tell skill from luck",
                    )
                )
        if not judgeable:
            return tuple(proposals)

        # Weight by return on capital, floored at zero: a losing segment is not
        # given a negative allocation, it is given less.
        weights = {
            segment: max(0.0, performance.return_on_capital)
            for segment, performance in judgeable.items()
        }
        for segment, performance in judgeable.items():
            # A segment that barely uses what it has does not need more, however
            # well it is doing with the fraction it touches.
            if performance.utilisation < self._utilisation_floor:
                weights[segment] = min(weights[segment], 0.0)

        total_weight = sum(weights.values())
        pool = sum(self._allocations.get(segment, 0.0) for segment in judgeable)

        for segment, performance in sorted(judgeable.items()):
            current = self._allocations.get(segment, 0.0)
            if total_weight <= 0:
                proposals.append(
                    self._proposal(
                        segment, current, current, NO_CHANGE, performance,
                        "no segment has a positive return on the capital it used",
                    )
                )
                self.standing.no_change += 1
                continue

            target = pool * (weights[segment] / total_weight)
            # Bounded so one good month cannot move everything at once.
            largest = current * self._maximum_move
            proposed = current + max(-largest, min(largest, target - current))
            change = proposed - current

            if abs(change) < current * 0.01:
                self.standing.no_change += 1
                proposals.append(
                    self._proposal(
                        segment, current, current, NO_CHANGE, performance,
                        f"return on capital {performance.return_on_capital:.2%}; the implied move "
                        f"is under a percent of the allocation",
                    )
                )
                continue

            self.standing.proposals += 1
            self.standing.largest_proposed_move = max(
                self.standing.largest_proposed_move, abs(change)
            )
            proposals.append(
                self._proposal(
                    segment, current, proposed, PROPOSED, performance,
                    f"{'more' if change > 0 else 'less'}: return on capital "
                    f"{performance.return_on_capital:.2%} over {performance.closed_trades} trades "
                    f"at {performance.utilisation:.0%} utilisation. The operator edits the file; "
                    f"this part moves nothing.",
                )
            )
        return tuple(proposals)

    def _proposal(self, segment, current, proposed, outcome, performance, reason) -> AllocationProposal:
        return AllocationProposal(
            segment=segment,
            current_allocation=current,
            proposed_allocation=proposed,
            change=proposed - current,
            outcome=outcome,
            return_on_capital=performance.return_on_capital,
            utilisation=performance.utilisation,
            closed_trades=performance.closed_trades,
            reason=reason,
            proposed_at_ns=self._now_ns(),
        )


def describe_proposals(proposer: AllocationRebalanceProposer) -> dict:
    return {
        "part_id": PART_ID,
        "rounds": proposer.standing.rounds,
        "proposals": proposer.standing.proposals,
        "held_for_too_few_trades": proposer.standing.held_for_too_few_trades,
        "no_change": proposer.standing.no_change,
        "largest_proposed_move": proposer.standing.largest_proposed_move,
        "segments_seen": proposer.standing.segments_seen,
        "moves_capital": False,
    }


def run_allocation_rebalance_proposer(
    proposer: AllocationRebalanceProposer, control_socket, read_performance, publish_proposals,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_performance(proposer)
        publish_proposals(proposer.propose())

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

    A segment's performance is the sum of its USDT statements, its closed
    trade count from its bots' scorecards, and its utilisation from the
    meter. Proposals go out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey

    statements = Batch(read=context.bus.reader("usdt-pnl-statement"))
    utilisations = LatestByKey(read=context.bus.reader("capital-utilisation"), key_of=lambda u: u.segment)
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    publish_proposals = context.bus.publisher_for("allocation-proposal")
    proposer = AllocationRebalanceProposer(
        minimum_closed_trades=int(context.number("rebalance_minimum_closed_trades")),
        maximum_move_fraction=context.number("rebalance_maximum_move_fraction"),
        utilisation_floor=context.number("rebalance_utilisation_floor"),
    )
    segment = str(context.setting("segment_id").value)
    realised = [0.0]
    capital = [0.0]
    trades = [0]
    last_publish = [float("-inf")]

    def read_performance(_proposer) -> None:
        for statement in statements.payloads():
            realised[0] += statement.net_pnl_usdt
            capital[0] = max(capital[0], statement.capital_used_usdt or 0.0)
        for scorecard in scorecards.payloads():
            trades[0] = max(trades[0], scorecard.describe().get("trades", 0))
        utilisation = utilisations.mapping().get(segment)
        if utilisation is not None:
            proposer.set_allocation(segment, utilisation.allotted)
            proposer.observe_performance(
                SegmentPerformance(
                    segment=segment, realised_usdt=realised[0], capital_used=capital[0],
                    closed_trades=trades[0], utilisation=utilisation.utilisation,
                )
            )

    def tick() -> None:
        read_performance(proposer)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        proposals = proposer.propose()
        if proposals:
            publish_proposals(tuple(proposals))
        last_publish[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
