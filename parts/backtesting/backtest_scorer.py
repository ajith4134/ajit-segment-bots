"""backtest-scorer: what a run scored, and how much of it the sample can support.

Every statistic here is easy to compute and easy to over-read. The scorer's job is to
compute them and attach the thing that decides whether they mean anything: how many
independent bets produced them.

- **Expectancy, not win rate.** A 70% win rate with losses three times the size of
  wins loses money. Both are reported, and expectancy is the one that decides.
- **Effective bets, not trade count.** Trades opened close together in correlated
  instruments are one bet with several fee payments; a Sharpe-like statistic over
  them is overconfident by roughly the square root of the ratio.
- **The required sample size is computed, not assumed.** For an observed edge and
  spread, the number of trades needed to distinguish it from zero is arithmetic, and
  reporting it beside the result is what stops "sixty trades, looks great" being
  treated as evidence.
- **Drawdown is peak-to-trough on the equity path**, not the worst single trade. The
  worst trade is survivable; the sequence of ordinary losses that arrived together is
  what ends an account.

**Cost share is reported prominently** because it is the number that predicts live
disappointment: a strategy whose gross edge is barely larger than its costs is a
strategy whose live result will be a rounding error away from the backtest in the
wrong direction.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from runtime.backtest_types import BacktestResult
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "backtest-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="backtest-scorer",
    consumes=("backtest-run",),
    produces=("backtest-result", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SCORED = "scored"
NO_TRADES = "the-run-produced-no-trade-to-score"
TOO_FEW_TRADES = "too-few-trades-to-distinguish-the-edge-from-zero"


@dataclass(frozen=True)
class ScoreOutcome:
    run_id: str
    state: str
    result: BacktestResult | None
    equity_path: tuple
    reason: str
    scored_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.result is not None


@dataclass
class ScorerStanding:
    runs_scored: int = 0
    runs_without_trades: int = 0
    runs_below_required_sample: int = 0
    runs_where_costs_exceeded_the_gross_edge: int = 0
    largest_required_sample: int = 0


class BacktestScorer:
    """Scores a run and reports the sample size its numbers would need."""

    def __init__(self, confidence_multiple: float, now_ns=time.time_ns) -> None:
        if confidence_multiple <= 0:
            raise ValueError(
                "the confidence multiple is how many standard errors the edge must "
                "exceed; at zero every edge is significant"
            )
        self._confidence_multiple = confidence_multiple
        self._now_ns = now_ns
        self._effective_bets: dict[str, float] = {}
        self.standing = ScorerStanding()

    def observe_effective_bets(self, run_id: str, effective_bets: float) -> None:
        """From the cluster detector: how many independent bets a run really made."""
        self._effective_bets[run_id] = effective_bets

    @staticmethod
    def worst_drawdown(equity_path) -> float:
        """Peak to trough on the path. The worst single trade is survivable."""
        peak = 0.0
        worst = 0.0
        for equity in equity_path:
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    def required_trades(self, edge: float, spread: float) -> int:
        """How many trades it would take to tell this edge apart from zero."""
        if edge == 0 or spread <= 0:
            return 0
        return int(math.ceil((self._confidence_multiple * spread / abs(edge)) ** 2))

    def score(self, run) -> ScoreOutcome:
        if not run.trades:
            self.standing.runs_without_trades += 1
            return self._outcome(
                run.run_id, NO_TRADES, None, (),
                "the run produced no trade. A rule that never fires has no edge to "
                "measure, which is a result rather than a failure",
            )

        nets = [trade.net for trade in run.trades]
        equity_path = []
        running = 0.0
        for net in nets:
            running += net
            equity_path.append(running)

        wins = [net for net in nets if net > 0]
        losses = [net for net in nets if net < 0]
        win_rate = len(wins) / len(nets)
        average_win = statistics.mean(wins) if wins else 0.0
        average_loss = statistics.mean(losses) if losses else 0.0
        # A 70% win rate with losses three times the size of wins loses money.
        expectancy = statistics.mean(nets)
        spread = statistics.pstdev(nets) if len(nets) > 1 else 0.0

        effective = self._effective_bets.get(run.run_id, float(len(nets)))
        required = self.required_trades(expectancy, spread)
        self.standing.largest_required_sample = max(
            self.standing.largest_required_sample, required
        )
        is_significant = required > 0 and effective >= required

        if run.cost_share >= 1.0:
            self.standing.runs_where_costs_exceeded_the_gross_edge += 1
        if not is_significant:
            self.standing.runs_below_required_sample += 1

        result = BacktestResult(
            run_id=run.run_id,
            instruction_id=run.instruction_id,
            trades=len(nets),
            net_return=run.net_return,
            gross_return=run.gross_return,
            cost_share=run.cost_share,
            win_rate=win_rate,
            average_win=average_win,
            average_loss=average_loss,
            expectancy=expectancy,
            worst_drawdown=self.worst_drawdown(equity_path),
            effective_bets=effective,
            is_significant=is_significant,
            required_trades=required,
            scored_at_ns=self._now_ns(),
        )
        self.standing.runs_scored += 1

        return self._outcome(
            run.run_id, SCORED if is_significant else TOO_FEW_TRADES, result,
            tuple(equity_path),
            f"{run.net_return:+.4f} net from {len(nets)} trade(s) "
            f"({effective:.1f} effective bet(s)), {win_rate:.0%} win rate, expectancy "
            f"{expectancy:+.4f}, worst drawdown {result.worst_drawdown:.4f}, "
            f"{run.cost_share:.0%} of the gross move spent on costs"
            + (
                f". {required} effective bet(s) would be needed to tell this edge apart "
                f"from zero, and there are {effective:.1f}"
                if not is_significant
                else ""
            )
            + (
                ". Costs exceed the gross edge, which is the number that predicts live "
                "disappointment"
                if run.cost_share >= 1.0
                else ""
            ),
        )

    def _outcome(self, run_id, state, result, equity_path, reason) -> ScoreOutcome:
        return ScoreOutcome(
            run_id=run_id, state=state, result=result, equity_path=equity_path,
            reason=reason, scored_at_ns=self._now_ns(),
        )


def describe_scoring(scorer: BacktestScorer) -> dict:
    return {
        "part_id": PART_ID,
        "runs_scored": scorer.standing.runs_scored,
        "runs_without_trades": scorer.standing.runs_without_trades,
        "runs_below_the_required_sample": scorer.standing.runs_below_required_sample,
        "runs_where_costs_exceeded_the_gross_edge": (
            scorer.standing.runs_where_costs_exceeded_the_gross_edge
        ),
        "largest_required_sample": scorer.standing.largest_required_sample,
        "confidence_multiple": scorer._confidence_multiple,
        "reports_win_rate_as_the_headline": False,
        "counts_trades_instead_of_effective_bets": False,
    }


def run_backtest_scorer(
    scorer: BacktestScorer, control_socket, read_runs, publish_results,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for run in read_runs():
            outcome = scorer.score(run)
            if outcome.is_usable:
                publish_results(outcome.result)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_scoring(scorer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    runs = Batch(read=context.bus.reader("backtest-run"))
    publish_results = context.bus.publisher_for("backtest-result")
    scorer = BacktestScorer(confidence_multiple=context.number("backtest_confidence_multiple"))

    return run_backtest_scorer(
        scorer=scorer,
        control_socket=context.control_socket,
        read_runs=lambda: tuple(runs.payloads()),
        publish_results=lambda result: publish_results((result,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
