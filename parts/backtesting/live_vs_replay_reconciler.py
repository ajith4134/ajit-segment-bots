"""live-vs-replay-reconciler: does the backtest predict anything at all.

Every other part in this block is machinery for producing a believable number. This
one asks whether the machinery works, by comparing what a backtest promised against
what the same instruction actually did live. It is the only feedback the whole
backtesting apparatus receives, and without it the apparatus is a very careful way of
being wrong.

The shape of the gap says which model is broken, and the diagnosis is the deliverable:

- **A consistent overstatement by roughly the same fraction on every instruction** is
  a cost model that is too cheap. Costs are subtracted proportionally, so their error
  shows up proportionally.
- **An erratic gap, large on some instructions and small on others**, is a fill model
  that is wrong: fills depend on liquidity, which varies by instrument far more than
  fees do.
- **A gap that grows with position size** is impact, which the cost model treats as
  sublinear and may be understating.
- **A gap concentrated in fast markets** is latency: the backtest fills at a price the
  live system could not reach in time.
- **Live beating the backtest** is not good news. It usually means the backtest is
  missing trades the live system took, which is a replay that does not match what runs.

Two properties keep the comparison meaningful. **Only the same instruction over the
same period is compared** -- comparing a backtest of one period against live results
of another compares markets. And **too few live trades produces no verdict**: the
live sample is always the small one, and reading a diagnosis out of four trades is
how a working cost model gets rewritten.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.backtest_types import LiveVsReplayGap
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "live-vs-replay-reconciler"

PART_DECLARATION = PartDeclaration(
    part_id="live-vs-replay-reconciler",
    consumes=("backtest-result", "trade-episode"),
    produces=("live-vs-replay-gap", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

RECONCILED = "reconciled"
TOO_FEW_LIVE_TRADES = "too-few-live-trades-to-diagnose-anything"
NO_BACKTEST = "no-backtest-result-for-this-instruction"
PERIODS_DO_NOT_MATCH = "the-two-cover-different-periods"

# What a gap of each shape most likely means. Naming it is the whole output.
COSTS_ARE_UNDERSTATED = "the-cost-model-is-too-cheap"
FILLS_ARE_OPTIMISTIC = "the-fill-model-is-wrong"
IMPACT_IS_UNDERSTATED = "impact-grows-faster-with-size-than-modelled"
LATENCY_IS_UNMODELLED = "the-backtest-fills-at-prices-the-live-system-cannot-reach-in-time"
THE_REPLAY_MISSES_TRADES = "the-replay-does-not-match-what-actually-runs"
NOTHING_OBVIOUS = "no-single-cause-stands-out"


@dataclass(frozen=True)
class ReconciliationOutcome:
    instruction_id: str
    state: str
    gap: LiveVsReplayGap | None
    per_trade_gaps: tuple
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.gap is not None


@dataclass
class ReconcilerStanding:
    reconciliations: int = 0
    refused_thin_live_sample: int = 0
    refused_no_backtest: int = 0
    refused_period_mismatch: int = 0
    by_cause: dict = field(default_factory=dict)
    times_live_beat_the_backtest: int = 0
    largest_gap: float = 0.0


class LiveVsReplayReconciler:
    """Compares promised against achieved and names which model is wrong."""

    def __init__(
        self,
        minimum_live_trades: int,
        consistency_tolerance: float,
        size_correlation_threshold: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_live_trades < 2:
            raise ValueError(
                "reading a diagnosis out of a handful of live trades is how a working "
                "cost model gets rewritten"
            )
        if consistency_tolerance <= 0:
            raise ValueError(
                "consistency is how tightly the per-trade gaps cluster, and it needs a "
                "tolerance"
            )
        self._minimum_live_trades = minimum_live_trades
        self._consistency_tolerance = consistency_tolerance
        self._size_correlation_threshold = size_correlation_threshold
        self._now_ns = now_ns
        self._backtests: dict[str, object] = {}
        self._backtest_periods: dict[str, tuple] = {}
        self._live: dict[str, list] = {}
        self.standing = ReconcilerStanding()

    def observe_backtest(self, result, period_from_ns: int, period_to_ns: int) -> None:
        self._backtests[result.instruction_id] = result
        self._backtest_periods[result.instruction_id] = (period_from_ns, period_to_ns)

    def observe_live_trade(
        self, instruction_id: str, realised: float, expected: float, notional: float,
        was_fast_market: bool, at_ns: int,
    ) -> None:
        self._live.setdefault(instruction_id, []).append(
            {
                "realised": realised, "expected": expected, "notional": notional,
                "fast": was_fast_market, "at_ns": at_ns,
            }
        )

    def reconcile(self, instruction_id: str) -> ReconciliationOutcome:
        self.standing.reconciliations += 1
        backtest = self._backtests.get(instruction_id)
        if backtest is None:
            self.standing.refused_no_backtest += 1
            return self._outcome(
                instruction_id, NO_BACKTEST, None, (),
                "no backtest result exists for this instruction, so there is nothing to "
                "compare live against",
            )

        live = self._live.get(instruction_id, [])
        if len(live) < self._minimum_live_trades:
            self.standing.refused_thin_live_sample += 1
            return self._outcome(
                instruction_id, TOO_FEW_LIVE_TRADES, None, (),
                f"{len(live)} live trade(s), below the {self._minimum_live_trades} bar. "
                f"The live sample is always the small one",
            )

        period = self._backtest_periods.get(instruction_id)
        if period is not None:
            outside = [
                trade for trade in live
                if not period[0] <= trade["at_ns"] <= period[1]
            ]
            if len(outside) == len(live):
                self.standing.refused_period_mismatch += 1
                return self._outcome(
                    instruction_id, PERIODS_DO_NOT_MATCH, None, (),
                    "every live trade falls outside the backtested period. Comparing "
                    "them compares markets rather than models",
                )

        per_trade = tuple(trade["expected"] - trade["realised"] for trade in live)
        live_total = sum(trade["realised"] for trade in live)
        gap = backtest.net_return - live_total
        self.standing.largest_gap = max(self.standing.largest_gap, abs(gap))

        mean_gap = statistics.mean(per_trade)
        spread = statistics.pstdev(per_trade) if len(per_trade) > 1 else 0.0
        is_consistent = (
            abs(mean_gap) > 0 and spread / abs(mean_gap) <= self._consistency_tolerance
        )

        cause = self._diagnose(live, per_trade, mean_gap, is_consistent)
        self.standing.by_cause[cause] = self.standing.by_cause.get(cause, 0) + 1
        if gap < 0:
            self.standing.times_live_beat_the_backtest += 1

        return self._outcome(
            instruction_id, RECONCILED,
            LiveVsReplayGap(
                instruction_id=instruction_id,
                backtested_return=backtest.net_return,
                live_return=live_total,
                gap=gap,
                live_trades=len(live),
                backtested_trades=backtest.trades,
                is_consistent=is_consistent,
                likely_cause=cause,
                reason=(
                    f"the backtest promised {backtest.net_return:+.4f} and live produced "
                    f"{live_total:+.4f}, a gap of {gap:+.4f} across {len(live)} live "
                    f"trade(s). The per-trade gap is "
                    + ("consistent" if is_consistent else "erratic")
                    + f", which points at: {cause}"
                ),
                measured_at_ns=self._now_ns(),
            ),
            per_trade,
            f"gap {gap:+.4f}, cause {cause}",
        )

    def _diagnose(self, live, per_trade, mean_gap, is_consistent) -> str:
        if mean_gap < 0:
            # Live ahead of the backtest usually means the replay missed trades.
            return THE_REPLAY_MISSES_TRADES

        fast = [
            gap for gap, trade in zip(per_trade, live) if trade["fast"]
        ]
        calm = [
            gap for gap, trade in zip(per_trade, live) if not trade["fast"]
        ]
        if fast and calm and statistics.mean(fast) > 2.0 * max(statistics.mean(calm), 1e-12):
            return LATENCY_IS_UNMODELLED

        if len(live) > 2:
            large = [
                gap for gap, trade in zip(per_trade, live)
                if trade["notional"] >= statistics.median(
                    item["notional"] for item in live
                )
            ]
            small = [
                gap for gap, trade in zip(per_trade, live)
                if trade["notional"] < statistics.median(
                    item["notional"] for item in live
                )
            ]
            if (
                large and small
                and statistics.mean(large)
                > statistics.mean(small) * (1.0 + self._size_correlation_threshold)
            ):
                return IMPACT_IS_UNDERSTATED

        if is_consistent:
            # Costs are subtracted proportionally, so their error shows up
            # proportionally rather than erratically.
            return COSTS_ARE_UNDERSTATED
        if per_trade:
            return FILLS_ARE_OPTIMISTIC
        return NOTHING_OBVIOUS

    def _outcome(self, instruction_id, state, gap, per_trade, reason) -> ReconciliationOutcome:
        return ReconciliationOutcome(
            instruction_id=instruction_id, state=state, gap=gap,
            per_trade_gaps=per_trade, reason=reason, measured_at_ns=self._now_ns(),
        )


def describe_reconciliation(reconciler: LiveVsReplayReconciler) -> dict:
    return {
        "part_id": PART_ID,
        "reconciliations": reconciler.standing.reconciliations,
        "refused_thin_live_sample": reconciler.standing.refused_thin_live_sample,
        "refused_no_backtest": reconciler.standing.refused_no_backtest,
        "refused_period_mismatch": reconciler.standing.refused_period_mismatch,
        "by_cause": dict(reconciler.standing.by_cause),
        "times_live_beat_the_backtest": (
            reconciler.standing.times_live_beat_the_backtest
        ),
        "largest_gap": reconciler.standing.largest_gap,
        "treats_live_beating_the_backtest_as_good_news": False,
        "compares_different_periods": False,
    }


def run_live_vs_replay_reconciler(
    reconciler: LiveVsReplayReconciler, control_socket, read_instructions, publish_gaps,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for instruction_id in read_instructions(reconciler):
            outcome = reconciler.reconcile(instruction_id)
            if outcome.is_usable:
                publish_gaps(outcome.gap)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_reconciliation(reconciler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A backtest result is observed per instruction; a closed trade's episode
    names the detector that raised it, which for a learned instruction is
    its id, and is observed as a live trade against the expectancy the
    episode's conditions record.
    """
    from runtime.input_assembly import Batch

    results = Batch(read=context.bus.reader("backtest-result"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    publish_gaps = context.bus.publisher_for("live-vs-replay-gap")
    reconciler = LiveVsReplayReconciler(
        minimum_live_trades=int(context.number("reconcile_minimum_live_trades")),
        consistency_tolerance=context.number("reconcile_consistency_tolerance"),
        size_correlation_threshold=context.number("reconcile_size_correlation_threshold"),
    )
    known: set[str] = set()

    def read_instructions(_reconciler):
        touched = set()
        for result in results.payloads():
            reconciler.observe_backtest(result, getattr(result, "period_from_ns", 0), getattr(result, "period_to_ns", 0))
            known.add(result.instruction_id)
            touched.add(result.instruction_id)
        for episode in episodes.payloads():
            if episode.detector not in known:
                continue
            conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
            reconciler.observe_live_trade(
                episode.detector, episode.realised, float(conditions.get("expected", 0.0) or 0.0),
                float(conditions.get("notional", 0.0) or 0.0), bool(conditions.get("was_fast_market", False)),
                episode.closed_at_ns,
            )
            touched.add(episode.detector)
        return tuple(sorted(touched))

    def publish(item) -> None:
        if item is not None:
            publish_gaps((item,))

    return run_live_vs_replay_reconciler(
        reconciler=reconciler,
        control_socket=context.control_socket,
        read_instructions=read_instructions,
        publish_gaps=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
