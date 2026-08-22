"""exit-timing-learner: what the exits actually cost, which entries never reveal.

A system that measures only entries improves its entries and loses the same money
in a different place. Exit timing is where a large fraction of a strategy's
expectancy quietly goes, and it is invisible from the trade record alone -- a
trade that made 2% looks the same whether the peak was 2.1% or 9%.

This part measures the difference, per detector and per regime:

- **Capture: what fraction of the peak was realised** (RL-042). That single
  number separates a strategy with a weak edge from one with a good edge and a
  bad exit, and the fix for each is opposite.
- **What a different exit would have made**, from the exit counterfactual. Capture
  alone says the exit was early; the counterfactual says whether waiting would
  actually have paid, and in a mean-reverting symbol it often would not.
- **Whether exits are early or late, separately.** They are different failures.
  Early exits leave money; late exits give back money already earned, and the
  second is the one that turns a winning strategy into a losing one.

**It writes to the instruction scorecard**, because an instruction's expectancy
is not separable from the exit rule it was traded with -- an instruction retired
for poor expectancy when the exit was the problem retires the wrong thing.

**A trade whose peak was never above its entry is excluded from capture.** There
was nothing to capture, and including it as 0% capture would make every losing
strategy look like an exit problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator, RateEstimator
from runtime.learning_types import InstructionScorecard
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "exit-timing-learner"

PART_DECLARATION = PartDeclaration(
    part_id="exit-timing-learner",
    consumes=("exit-quality", "trade-episode", "exit-counterfactual"),
    produces=("instruction-scorecard", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
NOT_MEASURED = "too-few-trades-with-something-to-capture"

EXITS_ARE_EARLY = "exits-are-leaving-money"
EXITS_ARE_LATE = "exits-are-giving-back-money-already-earned"
EXITS_ARE_TIMED = "exits-are-timed"


@dataclass(frozen=True)
class ExitTimingReport:
    """How well one instruction's exits have been timed, and which way they err."""

    instruction_id: str
    regime: str
    state: str
    verdict: str
    median_capture: float | None
    counterfactual_lift: float | None
    early_rate: Estimate
    trades_with_something_to_capture: int
    trades_excluded: int
    reason: str
    reported_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == MEASURED

    @property
    def the_exit_is_the_problem(self) -> bool:
        """A good edge with a bad exit, which retiring the instruction would not fix."""
        return self.verdict in (EXITS_ARE_EARLY, EXITS_ARE_LATE)


@dataclass
class LearnerStanding:
    trades_seen: int = 0
    trades_with_something_to_capture: int = 0
    trades_excluded: int = 0
    reports: int = 0
    instructions_where_the_exit_is_the_problem: int = 0
    by_verdict: dict = field(default_factory=dict)
    worst_capture_seen: float | None = None


class ExitTimingLearner:
    """Measures capture against the peak, and whether a different exit would have paid."""

    def __init__(
        self,
        good_capture: float,
        window: int,
        minimum_trades: int,
        prior_capture: float,
        prior_early_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < good_capture <= 1.0:
            raise ValueError("capture is a fraction of the peak and its bar must be in (0, 1]")
        self._good_capture = good_capture
        self._window = window
        self._minimum = minimum_trades
        self._prior_capture = prior_capture
        self._prior_early_rate = prior_early_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._capture: dict[tuple[str, str], QuantileEstimator] = {}
        self._early: dict[tuple[str, str], RateEstimator] = {}
        self._counterfactual: dict[tuple[str, str], list] = {}
        self._excluded: dict[tuple[str, str], int] = {}
        self.standing = LearnerStanding()

    def observe_exit(
        self, instruction_id: str, regime: str, realised: float, peak_favourable: float,
        exited_before_peak: bool,
    ) -> None:
        """One closed trade's exit against the peak it reached (RL-042)."""
        self.standing.trades_seen += 1
        key = (instruction_id, regime)

        if peak_favourable <= 0:
            # Nothing to capture. Counting it as 0% capture would make every
            # losing strategy look like an exit problem.
            self._excluded[key] = self._excluded.get(key, 0) + 1
            self.standing.trades_excluded += 1
            return

        self.standing.trades_with_something_to_capture += 1
        capture = max(0.0, min(1.0, realised / peak_favourable))
        self._capture_for(key).observe(capture)
        self._early_for(key).observe(exited_before_peak)

    def observe_counterfactual(
        self, instruction_id: str, regime: str, realised: float, would_have_made: float
    ) -> None:
        """What a different exit rule would have produced on the same trade.

        Capture alone says the exit was early; only this says whether waiting
        would actually have paid -- and in a mean-reverting symbol it often
        would not.
        """
        self._counterfactual.setdefault((instruction_id, regime), []).append(
            would_have_made - realised
        )

    def counterfactual_lift(self, instruction_id: str, regime: str) -> float | None:
        lifts = self._counterfactual.get((instruction_id, regime))
        if not lifts:
            return None
        return sum(lifts) / len(lifts)

    def report(self, instruction_id: str, regime: str) -> ExitTimingReport:
        self.standing.reports += 1
        key = (instruction_id, regime)
        capture_estimator = self._capture_for(key)
        capture = capture_estimator.estimate(0.5, self._minimum)
        early = self._early_for(key).estimate(self._minimum)
        lift = self.counterfactual_lift(instruction_id, regime)
        trades = capture_estimator.observations
        excluded = self._excluded.get(key, 0)

        if not capture.is_fitted:
            return self._report(
                instruction_id, regime, NOT_MEASURED, EXITS_ARE_TIMED, None, lift, early,
                trades, excluded,
                f"{trades} trade(s) with something to capture, of the {self._minimum} needed"
                + (
                    f"; {excluded} more had no favourable excursion at all and are excluded, "
                    f"because counting them as zero capture makes every losing strategy look "
                    f"like an exit problem"
                    if excluded
                    else ""
                ),
            )

        if self.standing.worst_capture_seen is None or capture.value < self.standing.worst_capture_seen:
            self.standing.worst_capture_seen = capture.value

        if capture.value >= self._good_capture:
            verdict = EXITS_ARE_TIMED
        elif lift is not None and lift <= 0:
            # Capture is low but waiting would not have paid: the peak was
            # unreachable, which is not an exit failure.
            verdict = EXITS_ARE_TIMED
        elif early.value > 0.5:
            verdict = EXITS_ARE_EARLY
        else:
            verdict = EXITS_ARE_LATE

        self.standing.by_verdict[verdict] = self.standing.by_verdict.get(verdict, 0) + 1
        if verdict != EXITS_ARE_TIMED:
            self.standing.instructions_where_the_exit_is_the_problem += 1

        return self._report(
            instruction_id, regime, MEASURED, verdict, capture.value, lift, early,
            trades, excluded,
            f"{instruction_id} captures {capture.value:.0%} of its peak in {regime} over "
            f"{trades} trade(s), against a {self._good_capture:.0%} bar"
            + (
                f"; a different exit would have made {lift:+.4%} more per trade"
                if lift is not None
                else "; no exit counterfactual has been recorded, so whether waiting would "
                "have paid is unknown"
            )
            + (
                ". Early and late are different failures: early leaves money, late gives back "
                "money already earned, and the second turns a winning strategy into a losing one"
                if verdict != EXITS_ARE_TIMED
                else ""
            ),
        )

    def scorecard(self, instruction_id: str, regime: str) -> InstructionScorecard:
        """The exit's contribution, written where the instruction's record lives.

        An instruction's expectancy is not separable from the exit rule it was
        traded with; retiring it for poor expectancy when the exit was the
        problem retires the wrong thing.
        """
        report = self.report(instruction_id, regime)
        capture_estimator = self._capture_for((instruction_id, regime))
        return InstructionScorecard(
            instruction_id=instruction_id,
            trades=capture_estimator.observations,
            wins=0,
            realised=0.0,
            hit_rate=capture_estimator.estimate(0.5, self._minimum),
            expectancy=None,
            live_versus_replay_gap=report.counterfactual_lift,
            is_measured=report.is_measured,
            reason=report.reason,
            scored_at_ns=self._now_ns(),
        )

    def _capture_for(self, key) -> QuantileEstimator:
        estimator = self._capture.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_capture)
            self._capture[key] = estimator
        return estimator

    def _early_for(self, key) -> RateEstimator:
        estimator = self._early.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_early_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._early[key] = estimator
        return estimator

    def _report(
        self, instruction_id, regime, state, verdict, capture, lift, early, trades, excluded, reason
    ) -> ExitTimingReport:
        return ExitTimingReport(
            instruction_id=instruction_id,
            regime=regime,
            state=state,
            verdict=verdict,
            median_capture=capture,
            counterfactual_lift=lift,
            early_rate=early,
            trades_with_something_to_capture=trades,
            trades_excluded=excluded,
            reason=reason,
            reported_at_ns=self._now_ns(),
        )


def describe_exit_timing(learner: ExitTimingLearner) -> dict:
    return {
        "part_id": PART_ID,
        "trades_seen": learner.standing.trades_seen,
        "trades_with_something_to_capture": learner.standing.trades_with_something_to_capture,
        "trades_excluded_with_no_favourable_excursion": learner.standing.trades_excluded,
        "reports": learner.standing.reports,
        "instructions_where_the_exit_is_the_problem": (
            learner.standing.instructions_where_the_exit_is_the_problem
        ),
        "by_verdict": dict(sorted(learner.standing.by_verdict.items())),
        "worst_capture_seen": learner.standing.worst_capture_seen,
    }


def run_exit_timing_learner(
    learner: ExitTimingLearner, control_socket, read_exits, publish_scorecards,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        contexts = read_exits(learner)
        publish_scorecards(
            tuple(learner.scorecard(instruction_id, regime) for instruction_id, regime in contexts)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
