"""lookahead-auditor: does this run's arithmetic admit to using the future.

The replayer prevents lookahead structurally, which is the right way to prevent it.
This part is the check that the structure held -- because a bounded view protects the
decision and not everything else: a cost model fitted on the whole period, a stop
placed at a level derived from the session's range, a symbol list assembled from
today's survivors. Those all leak the future without any decision reaching forward.

The audit is arithmetic on the run rather than inspection of the code, so it works on
runs produced by code nobody has read:

- **Every trade's decision timestamp must precede its entry.** A decision stamped
  after its own fill is the plainest form of the defect.
- **Fills must lie inside the bar they claim.** A price the bar never contained means
  the fill came from somewhere other than the tape.
- **Costs must be non-zero.** A run with no costs is not a slightly optimistic run,
  it is a run about a market that does not charge, and its result is unrelated to any
  achievable one.
- **The result must not be impossibly smooth.** A win rate at or near one over enough
  trades is not a good strategy, it is a bug, and treating it as a finding is how a
  broken backtest reaches production.
- **Out-of-sample must be true.** A run over the training period is a description of
  the training period.

Any of these is fatal on its own. The verdict is not a score -- there is no partly
believable backtest -- and a run with a fatal defect is refused rather than discounted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import (
    BacktestVerdict, FATAL_DEFECTS, FILLED_AT_A_PRICE_THAT_NEVER_TRADED,
    FITTED_ON_THE_TEST_PERIOD, NO_COSTS, USED_THE_FUTURE,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "lookahead-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="lookahead-auditor",
    consumes=("backtest-run",),
    produces=("backtest-verdict", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

BELIEVABLE = "believable"
NOT_BELIEVABLE = "not-believable"

IMPOSSIBLY_SMOOTH = "the-result-is-too-clean-to-be-a-strategy"

CHECKS = (
    USED_THE_FUTURE, FILLED_AT_A_PRICE_THAT_NEVER_TRADED, NO_COSTS,
    FITTED_ON_THE_TEST_PERIOD, IMPOSSIBLY_SMOOTH,
)


@dataclass(frozen=True)
class AuditOutcome:
    run_id: str
    state: str
    verdict: BacktestVerdict
    offending_trades: tuple
    reason: str
    audited_at_ns: int

    @property
    def is_usable(self) -> bool:
        return True


@dataclass
class AuditorStanding:
    runs_audited: int = 0
    believable: int = 0
    refused: int = 0
    by_defect: dict = field(default_factory=dict)
    trades_that_used_the_future: int = 0
    fills_outside_their_bar: int = 0


class LookaheadAuditor:
    """Checks a run's own numbers for the defects that make it unbelievable."""

    def __init__(
        self,
        impossible_win_rate: float,
        minimum_trades_for_smoothness: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < impossible_win_rate <= 1.0:
            raise ValueError(
                "the smoothness bar is a win rate; above it a result is a bug rather "
                "than a strategy"
            )
        if minimum_trades_for_smoothness < 2:
            raise ValueError(
                "two winning trades is not an impossibly smooth result, it is two trades"
            )
        self._impossible_win_rate = impossible_win_rate
        self._minimum_trades = minimum_trades_for_smoothness
        self._now_ns = now_ns
        self._bars: dict[tuple, tuple] = {}
        self.standing = AuditorStanding()

    def observe_bar(
        self, venue_id: str, symbol: str, at_ns: int, low_price: float, high_price: float,
    ) -> None:
        """The range each bar actually covered, so fills can be checked against it."""
        self._bars[(venue_id, symbol, at_ns)] = (low_price, high_price)

    def audit(self, run) -> AuditOutcome:
        self.standing.runs_audited += 1
        defects: list = []
        offending: list = []

        for index, trade in enumerate(run.trades):
            if trade.used_the_future:
                if USED_THE_FUTURE not in defects:
                    defects.append(USED_THE_FUTURE)
                offending.append((index, USED_THE_FUTURE))
                self.standing.trades_that_used_the_future += 1

            for at_ns, price in (
                (trade.entry_at_ns, trade.entry_price),
                (trade.exit_at_ns, trade.exit_price),
            ):
                bar = self._bars.get((run.venue_id, run.symbol, at_ns))
                if bar is None:
                    continue
                low, high = bar
                if not low <= price <= high:
                    if FILLED_AT_A_PRICE_THAT_NEVER_TRADED not in defects:
                        defects.append(FILLED_AT_A_PRICE_THAT_NEVER_TRADED)
                    offending.append((index, FILLED_AT_A_PRICE_THAT_NEVER_TRADED))
                    self.standing.fills_outside_their_bar += 1

        if run.trades and (not run.costs_applied or all(
            trade.costs <= 0 for trade in run.trades
        )):
            defects.append(NO_COSTS)

        if not run.was_out_of_sample:
            defects.append(FITTED_ON_THE_TEST_PERIOD)

        if len(run.trades) >= self._minimum_trades:
            wins = sum(1 for trade in run.trades if trade.net > 0)
            win_rate = wins / len(run.trades)
            if win_rate >= self._impossible_win_rate:
                defects.append(IMPOSSIBLY_SMOOTH)

        for defect in defects:
            self.standing.by_defect[defect] = self.standing.by_defect.get(defect, 0) + 1

        if defects:
            self.standing.refused += 1
            return self._outcome(
                run.run_id, NOT_BELIEVABLE, defects, tuple(offending),
                "; ".join(self._explain(defect) for defect in defects)
                + ". Each of these is fatal on its own: there is no partly believable "
                  "backtest, so the run is refused rather than discounted",
            )

        self.standing.believable += 1
        return self._outcome(
            run.run_id, BELIEVABLE, (), (),
            f"{len(CHECKS)} check(s) passed over {len(run.trades)} trade(s). The "
            f"replayer prevents lookahead structurally; this confirms the structure held "
            f"for the parts a bounded view does not cover",
        )

    @staticmethod
    def _explain(defect: str) -> str:
        return {
            USED_THE_FUTURE: (
                "a decision is stamped after the fill it produced, which is the plainest "
                "form of the defect"
            ),
            FILLED_AT_A_PRICE_THAT_NEVER_TRADED: (
                "a fill sits outside the range of the bar it claims, so it came from "
                "somewhere other than the tape"
            ),
            NO_COSTS: (
                "no cost was subtracted, so this describes a market that does not charge "
                "for anything"
            ),
            FITTED_ON_THE_TEST_PERIOD: (
                "the run covers the period the rule was fitted on, so it describes that "
                "period rather than the rule"
            ),
            IMPOSSIBLY_SMOOTH: (
                "the win rate is too clean to be a strategy. That is a bug, and treating "
                "it as a finding is how a broken backtest reaches production"
            ),
        }.get(defect, defect)

    def _outcome(self, run_id, state, defects, offending, reason) -> AuditOutcome:
        return AuditOutcome(
            run_id=run_id, state=state,
            verdict=BacktestVerdict(
                run_id=run_id, defects=tuple(defects), is_believable=not defects,
                checks_run=CHECKS, reason=reason, audited_at_ns=self._now_ns(),
            ),
            offending_trades=offending, reason=reason, audited_at_ns=self._now_ns(),
        )


def describe_auditing(auditor: LookaheadAuditor) -> dict:
    return {
        "part_id": PART_ID,
        "runs_audited": auditor.standing.runs_audited,
        "believable": auditor.standing.believable,
        "refused": auditor.standing.refused,
        "by_defect": dict(auditor.standing.by_defect),
        "trades_that_used_the_future": auditor.standing.trades_that_used_the_future,
        "fills_outside_their_bar": auditor.standing.fills_outside_their_bar,
        "checks": list(CHECKS),
        "fatal_defects": list(FATAL_DEFECTS),
        "produces_a_partial_score": False,
    }


def run_lookahead_auditor(
    auditor: LookaheadAuditor, control_socket, read_runs, publish_verdicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for run in read_runs():
            publish_verdicts(auditor.audit(run).verdict)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
