"""expectancy-decomposer: where a strategy's money actually came from, and went.

Expectancy is a single number that hides four different businesses. A strategy
with an excellent setup and terrible exits, and one with a mediocre setup and
perfect exits, can have identical expectancy -- and the fix for each is the
opposite of the fix for the other.

So the decomposition is the deliverable:

- **From the setup**: what the trade would have made at the peak the setup
  reached, which is the setup's own contribution and nothing else's.
- **From entry timing**: what entering better or worse than the average entry
  cost or saved.
- **From exit timing**: the gap between the peak and what was realised. This is
  where most strategies quietly lose, and expectancy alone cannot see it.
- **From costs**: fees, funding and slippage, which are a real business decision
  and not an accounting footnote.

**Every component can be positive while the total is negative.** That is the case
this part exists for: a strategy losing money with a working setup needs a new
exit, and expectancy alone says only that it loses.

**Decomposed per detector and per regime.** A strategy's exits can be right in a
trend and wrong in a chop, and the aggregate describes neither.

**Costs are never netted into another component.** Netting them into the setup
makes an expensive strategy look like a bad one, and the two need different
answers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.learning_types import (
    ExpectancyBreakdown, FROM_COSTS, FROM_ENTRY_TIMING, FROM_EXIT_TIMING, FROM_SIZING,
    FROM_THE_MARKET, FROM_THE_SETUP,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "expectancy-decomposer"

PART_DECLARATION = PartDeclaration(
    part_id="expectancy-decomposer",
    consumes=("trade-episode", "forecast-accuracy", "pnl-attribution", "horizon-profile"),
    produces=("expectancy-breakdown", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class TradeContribution:
    """One trade, split into where its result came from."""

    detector: str
    regime: str
    realised: float
    peak_favourable: float
    entry_slippage: float
    costs: float
    market_drift: float

    @property
    def from_the_setup(self) -> float:
        """What the setup itself reached, before anything gave it back."""
        return self.peak_favourable - self.market_drift

    @property
    def from_exit_timing(self) -> float:
        """The gap between the peak and what was kept. Usually the largest drag."""
        return self.realised + self.costs - self.peak_favourable

    @property
    def from_entry_timing(self) -> float:
        return -self.entry_slippage


@dataclass
class DecomposerStanding:
    trades_decomposed: int = 0
    breakdowns_produced: int = 0
    strategies_saveable_by_fixing_one_component: int = 0
    by_largest_drag: dict = field(default_factory=dict)
    worst_exit_drag: float | None = None


class ExpectancyDecomposer:
    """Splits expectancy into the four businesses it hides."""

    def __init__(
        self,
        minimum_trades: int,
        prior_win_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        self._minimum = minimum_trades
        self._prior_win_rate = prior_win_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._contributions: dict[tuple[str, str], list] = {}
        self._win_rates: dict[tuple[str, str], RateEstimator] = {}
        self.standing = DecomposerStanding()

    def observe_trade(self, contribution: TradeContribution) -> None:
        key = (contribution.detector, contribution.regime)
        self._contributions.setdefault(key, []).append(contribution)
        self._win_rate_for(key).observe(contribution.realised > 0)
        self.standing.trades_decomposed += 1

    def decompose(self, detector: str, regime: str) -> ExpectancyBreakdown:
        self.standing.breakdowns_produced += 1
        key = (detector, regime)
        trades = self._contributions.get(key, [])
        win_rate = self._win_rate_for(key).estimate(self._minimum)

        if not trades:
            return ExpectancyBreakdown(
                detector=detector, regime=regime, total_expectancy=0.0, by_component={},
                trades=0, win_rate=win_rate, average_win=0.0, average_loss=0.0,
                is_measured=False,
                reason=f"no trade has closed for {detector} in {regime}",
                decomposed_at_ns=self._now_ns(),
            )

        count = len(trades)
        components = {
            FROM_THE_SETUP: sum(trade.from_the_setup for trade in trades) / count,
            FROM_ENTRY_TIMING: sum(trade.from_entry_timing for trade in trades) / count,
            FROM_EXIT_TIMING: sum(trade.from_exit_timing for trade in trades) / count,
            # Never netted into another component: netting costs into the setup
            # makes an expensive strategy look like a bad one, and the two need
            # different answers.
            FROM_COSTS: -sum(trade.costs for trade in trades) / count,
            FROM_THE_MARKET: sum(trade.market_drift for trade in trades) / count,
        }
        total = sum(trade.realised for trade in trades) / count

        wins = [trade.realised for trade in trades if trade.realised > 0]
        losses = [trade.realised for trade in trades if trade.realised <= 0]

        breakdown = ExpectancyBreakdown(
            detector=detector,
            regime=regime,
            total_expectancy=total,
            by_component=components,
            trades=count,
            win_rate=win_rate,
            average_win=sum(wins) / len(wins) if wins else 0.0,
            average_loss=sum(losses) / len(losses) if losses else 0.0,
            is_measured=win_rate.is_fitted,
            reason=self._explain(detector, regime, total, components, count),
            decomposed_at_ns=self._now_ns(),
        )

        drag = breakdown.largest_drag
        if drag is not None:
            self.standing.by_largest_drag[drag[0]] = (
                self.standing.by_largest_drag.get(drag[0], 0) + 1
            )
            if drag[0] == FROM_EXIT_TIMING and (
                self.standing.worst_exit_drag is None
                or drag[1] < self.standing.worst_exit_drag
            ):
                self.standing.worst_exit_drag = drag[1]
        if breakdown.would_be_profitable_without_its_worst_component and total <= 0:
            self.standing.strategies_saveable_by_fixing_one_component += 1

        return breakdown

    def decompose_all(self) -> tuple:
        return tuple(
            self.decompose(detector, regime) for detector, regime in sorted(self._contributions)
        )

    def _explain(self, detector, regime, total, components, count) -> str:
        negatives = {name: value for name, value in components.items() if value < 0}
        worst = min(negatives, key=lambda name: negatives[name]) if negatives else None
        return (
            f"{detector} in {regime} over {count} trade(s): expectancy {total:+.4%} from "
            + ", ".join(f"{name} {value:+.4%}" for name, value in sorted(components.items()))
            + (
                f". The largest drag is {worst} at {negatives[worst]:+.4%}; without it this "
                f"would be {total - negatives[worst]:+.4%}, which is what expectancy alone "
                f"cannot see -- a strategy losing money with a working setup needs a new exit, "
                f"not a new setup"
                if worst is not None
                else ". Nothing is dragging"
            )
        )

    def _win_rate_for(self, key) -> RateEstimator:
        estimator = self._win_rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_win_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._win_rates[key] = estimator
        return estimator


def describe_expectancy(decomposer: ExpectancyDecomposer) -> dict:
    return {
        "part_id": PART_ID,
        "trades_decomposed": decomposer.standing.trades_decomposed,
        "breakdowns_produced": decomposer.standing.breakdowns_produced,
        "strategies_saveable_by_fixing_one_component": (
            decomposer.standing.strategies_saveable_by_fixing_one_component
        ),
        "by_largest_drag": dict(sorted(decomposer.standing.by_largest_drag.items())),
        "worst_exit_drag": decomposer.standing.worst_exit_drag,
        "components": [
            FROM_THE_SETUP, FROM_ENTRY_TIMING, FROM_EXIT_TIMING, FROM_COSTS, FROM_THE_MARKET
        ],
    }


def run_expectancy_decomposer(
    decomposer: ExpectancyDecomposer, control_socket, read_episodes, publish_breakdowns,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_episodes(decomposer)
        publish_breakdowns(decomposer.decompose_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
