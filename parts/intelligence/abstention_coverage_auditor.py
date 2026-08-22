"""abstention-coverage-auditor: what the system chose not to trade, and whether that was right.

A trading system's record is a sample it selected. Every measure of how good it
is -- hit rate, expectancy, competence -- is computed over the trades it took, and
says nothing about the ones it passed on. A system that abstains from everything
difficult looks excellent and is useless, and nothing inside it can tell.

This part measures the other half:

- **Coverage.** What fraction of the opportunities it saw did it act on? Very low
  coverage means every other measurement is about a small, self-selected slice.
- **What abstention cost.** A near miss that would have worked is evidence that
  the refusals are too tight; one that would have lost is evidence they are
  right. Without both, abstention looks free -- and free is the one thing it is
  not, because the trades not taken are where a system's edge quietly goes.
- **Why it abstained, by reason.** "Conviction below floor" ten thousand times
  and "no exit plan" twice are different problems with different fixes, and a
  single abstention count would hide which one is happening.

**Abstaining is not automatically good or bad**, and this part refuses to say
which. It reports the rate, the cost and the reasons; whether the floor should
move is a decision belonging to whoever owns the floor.

**Coverage is per symbol and per regime**, because a system that covers the
majors and abstains from everything else has a coverage number that describes
neither.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "abstention-coverage-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="abstention-coverage-auditor",
    consumes=("directional-opinion", "trade-episode", "near-miss-episode"),
    produces=("coverage-report", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
TOO_FEW_OPPORTUNITIES = "too-few-opportunities-seen-to-report-coverage"


@dataclass(frozen=True)
class CoverageReport:
    """How much of what the system saw it acted on, and what the rest would have done."""

    venue_id: str
    symbol: str
    regime: str
    state: str
    coverage: float | None
    opportunities_seen: int
    acted_on: int
    abstained: int
    abstention_would_have_won: Estimate
    median_missed_return: float | None
    by_refusal: dict
    reason: str
    reported_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == MEASURED

    @property
    def abstention_is_costing_money(self) -> bool:
        """Measured, not assumed: near misses that would have worked, more often than not."""
        return (
            self.abstention_would_have_won.is_fitted
            and self.abstention_would_have_won.value > 0.5
        )


@dataclass
class AuditorStanding:
    opportunities_seen: int = 0
    acted_on: int = 0
    abstained: int = 0
    near_misses_scored: int = 0
    reports: int = 0
    contexts_tracked: int = 0
    by_refusal: dict = field(default_factory=dict)
    lowest_coverage_seen: float | None = None


class AbstentionCoverageAuditor:
    """Measures what was passed on, and what passing on it cost."""

    def __init__(
        self,
        minimum_opportunities: int,
        prior_near_miss_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_near_misses: int,
        missed_return_window: int,
        prior_missed_return: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_opportunities < 1:
            raise ValueError("coverage over no opportunities is not a fraction")
        self._minimum_opportunities = minimum_opportunities
        self._minimum_near_misses = minimum_near_misses
        self._now_ns = now_ns
        self._seen: dict[tuple[str, str, str], int] = {}
        self._acted: dict[tuple[str, str, str], int] = {}
        self._refusals: dict[tuple[str, str, str], dict] = {}
        self._near_miss_outcome: dict[tuple[str, str, str], RateEstimator] = {}
        self._missed_returns: dict[tuple[str, str, str], QuantileEstimator] = {}
        self._prior_hit_rate = prior_near_miss_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._missed_window = missed_return_window
        self._prior_missed_return = prior_missed_return
        self.standing = AuditorStanding()

    def observe_opportunity(self, venue_id: str, symbol: str, regime: str, was_acted_on: bool, refusal: str | None = None) -> None:
        """One thing the system saw, and whether it did anything about it."""
        key = (venue_id, symbol, regime)
        self._seen[key] = self._seen.get(key, 0) + 1
        self.standing.opportunities_seen += 1
        if was_acted_on:
            self._acted[key] = self._acted.get(key, 0) + 1
            self.standing.acted_on += 1
        else:
            self.standing.abstained += 1
            reason = refusal or "unstated"
            self._refusals.setdefault(key, {})
            self._refusals[key][reason] = self._refusals[key].get(reason, 0) + 1
            self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        self.standing.contexts_tracked = len(self._seen)

    def observe_near_miss(
        self, venue_id: str, symbol: str, regime: str, would_have_won: bool, would_have_returned: float
    ) -> None:
        """What a trade the system passed on would have produced.

        Both outcomes matter. Without the losses, every refusal looks like a
        missed profit; without the wins, abstention looks free -- and free is the
        one thing it is not.
        """
        key = (venue_id, symbol, regime)
        self._near_miss_for(key).observe(would_have_won)
        self._missed_return_for(key).observe(would_have_returned)
        self.standing.near_misses_scored += 1

    def report(self, venue_id: str, symbol: str, regime: str) -> CoverageReport:
        self.standing.reports += 1
        key = (venue_id, symbol, regime)
        seen = self._seen.get(key, 0)
        acted = self._acted.get(key, 0)
        refusals = dict(self._refusals.get(key, {}))
        near_miss = self._near_miss_for(key).estimate(self._minimum_near_misses)
        missed = self._missed_return_for(key).estimate(0.5, self._minimum_near_misses)

        if seen < self._minimum_opportunities:
            return self._report(
                venue_id, symbol, regime, TOO_FEW_OPPORTUNITIES, None, seen, acted,
                near_miss, None, refusals,
                f"{seen} opportunity/opportunities of the {self._minimum_opportunities} "
                f"needed before a coverage fraction means anything",
            )

        coverage = acted / seen
        if (
            self.standing.lowest_coverage_seen is None
            or coverage < self.standing.lowest_coverage_seen
        ):
            self.standing.lowest_coverage_seen = coverage

        return self._report(
            venue_id, symbol, regime, MEASURED, coverage, seen, acted, near_miss,
            missed.value if missed.is_fitted else None, refusals,
            f"acted on {acted} of {seen} opportunities ({coverage:.0%}) in {regime}"
            + (
                f"; the ones passed on would have worked {near_miss.value:.0%} of the time "
                f"over {near_miss.observations} scored near miss(es), with a median "
                + (f"{missed.value:+.2%}" if missed.is_fitted else "unmeasured")
                + " return"
                if near_miss.observations
                else "; none of the ones passed on has been scored, so abstention still "
                "looks free -- and free is the one thing it is not"
            )
            + (
                f". Refusals: {', '.join(f'{reason} {count}' for reason, count in sorted(refusals.items()))}"
                if refusals
                else ""
            )
            + ". Whether the floor should move is not this part's decision",
        )

    def report_all(self) -> tuple:
        return tuple(
            self.report(venue_id, symbol, regime)
            for venue_id, symbol, regime in sorted(self._seen)
        )

    def _near_miss_for(self, key) -> RateEstimator:
        estimator = self._near_miss_outcome.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._near_miss_outcome[key] = estimator
        return estimator

    def _missed_return_for(self, key) -> QuantileEstimator:
        estimator = self._missed_returns.get(key)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._missed_window, prior=self._prior_missed_return
            )
            self._missed_returns[key] = estimator
        return estimator

    def _report(
        self, venue_id, symbol, regime, state, coverage, seen, acted,
        near_miss, missed, refusals, reason,
    ) -> CoverageReport:
        return CoverageReport(
            venue_id=venue_id,
            symbol=symbol,
            regime=regime,
            state=state,
            coverage=coverage,
            opportunities_seen=seen,
            acted_on=acted,
            abstained=seen - acted,
            abstention_would_have_won=near_miss,
            median_missed_return=missed,
            by_refusal=refusals,
            reason=reason,
            reported_at_ns=self._now_ns(),
        )


def describe_coverage(auditor: AbstentionCoverageAuditor) -> dict:
    seen = max(1, auditor.standing.opportunities_seen)
    return {
        "part_id": PART_ID,
        "opportunities_seen": auditor.standing.opportunities_seen,
        "acted_on": auditor.standing.acted_on,
        "abstained": auditor.standing.abstained,
        "overall_coverage": auditor.standing.acted_on / seen,
        "near_misses_scored": auditor.standing.near_misses_scored,
        "contexts_tracked": auditor.standing.contexts_tracked,
        "lowest_coverage_seen": auditor.standing.lowest_coverage_seen,
        "by_refusal": dict(sorted(auditor.standing.by_refusal.items())),
        "says_whether_abstaining_was_right": False,
    }


def run_abstention_coverage_auditor(
    auditor: AbstentionCoverageAuditor, control_socket, read_opportunities, publish_reports,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_opportunities(auditor)
        publish_reports(auditor.report_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
