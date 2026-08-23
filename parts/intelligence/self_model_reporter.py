"""self-model-reporter: what this system is actually competent at.

Every part reports how it is doing. Nobody assembles those into an answer to the
question that decides whether a trade should be taken at all: **does this system
understand this symbol, in this regime, well enough to have a view?**

The competence map is that answer, and the discipline in it is that competence
must be *earned per context*, never inherited:

- **Per symbol and per regime.** A system that is excellent on BTCUSDT in a trend
  is not thereby competent on a new listing in a chop. Competence measured
  overall and applied everywhere is how a system's best result licenses its worst
  trade.
- **From closed trades and scored instructions, not from activity.** A part that
  has run for a month has produced no competence; one whose calls have resolved
  has. Activity is the metric that flatters most and means least.
- **Coverage bounds it.** A symbol the system has abstained on almost entirely
  has no competence in it, however good the few trades were -- the sample is the
  ones it happened to like, and that is the definition of selection bias.
- **Forgetting reduces it.** A model that has stopped recalling what it learned
  is less competent than its trade record says, and only the forgetting report
  can see that.

**Unmeasured is its own state, not zero and not average.** A symbol with no
record is one the arbiter must refuse rather than trade small (Rule 8).

**Competence decays.** A record from a regime that ended describes a system that
no longer exists, and a map without decay would keep authorising trades on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "self-model-reporter"

PART_DECLARATION = PartDeclaration(
    part_id="self-model-reporter",
    consumes=("bot-scorecard", "instruction-scorecard", "coverage-report", "forgetting-report"),
    produces=("competence-map", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

COMPETENT = "competent"
NOT_COMPETENT = "measured-and-not-competent"
NOT_MEASURED = "no-record-in-this-context"
COVERAGE_TOO_THIN = "the-sample-is-the-trades-it-happened-to-like"


@dataclass(frozen=True)
class Competence:
    """What this system knows about one symbol in one regime, and how sure that is."""

    venue_id: str
    symbol: str
    regime: str
    state: str
    competence: float | None
    hit_rate: Estimate
    trades: int
    coverage: float | None
    forgetting: float | None
    reason: str
    reported_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state in (COMPETENT, NOT_COMPETENT)

    @property
    def is_competent(self) -> bool:
        return self.state == COMPETENT


@dataclass(frozen=True)
class CompetenceMap:
    """Every context this system has a view on, and every one it does not."""

    entries: tuple
    contexts_measured: int
    contexts_competent: int
    contexts_unmeasured: int
    best: Competence | None
    worst: Competence | None
    reason: str
    mapped_at_ns: int

    def competence_in(self, venue_id: str, symbol: str, regime: str) -> Competence | None:
        for entry in self.entries:
            if (entry.venue_id, entry.symbol, entry.regime) == (venue_id, symbol, regime):
                return entry
        return None


@dataclass
class ReporterStanding:
    maps_produced: int = 0
    trades_learned_from: int = 0
    contexts_tracked: int = 0
    contexts_competent: int = 0
    contexts_thin: int = 0
    highest_competence_seen: float | None = None


class SelfModelReporter:
    """Assembles what this system is competent at, per symbol and per regime."""

    def __init__(
        self,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_trades: int,
        minimum_coverage: float,
        competence_threshold: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < competence_threshold < 1.0:
            raise ValueError("competence is a fraction and its threshold must be inside (0, 1)")
        if not 0.0 <= minimum_coverage <= 1.0:
            raise ValueError("coverage is a fraction of the opportunities seen")
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum_trades = minimum_trades
        self._minimum_coverage = minimum_coverage
        self._threshold = competence_threshold
        self._now_ns = now_ns
        self._records: dict[tuple[str, str, str], RateEstimator] = {}
        self._trades: dict[tuple[str, str, str], int] = {}
        self._coverage: dict[tuple[str, str], float] = {}
        self._forgetting: dict[tuple[str, str], float] = {}
        self.standing = ReporterStanding()

    def observe_closed_trade(self, venue_id: str, symbol: str, regime: str, was_win: bool) -> None:
        """Competence comes from resolved calls, never from activity."""
        key = (venue_id, symbol, regime)
        self._estimator_for(key).observe(was_win)
        self._trades[key] = self._trades.get(key, 0) + 1
        self.standing.trades_learned_from += 1
        self.standing.contexts_tracked = len(self._records)

    def observe_coverage(self, venue_id: str, symbol: str, coverage: float) -> None:
        """What fraction of this symbol's opportunities the system actually acted on."""
        self._coverage[(venue_id, symbol)] = coverage

    def observe_forgetting(self, venue_id: str, symbol: str, recall: float) -> None:
        self._forgetting[(venue_id, symbol)] = recall

    def competence_in(self, venue_id: str, symbol: str, regime: str) -> Competence:
        key = (venue_id, symbol, regime)
        trades = self._trades.get(key, 0)
        hit_rate = self._estimator_for(key).estimate(self._minimum_trades)
        coverage = self._coverage.get((venue_id, symbol))
        forgetting = self._forgetting.get((venue_id, symbol))

        if trades < self._minimum_trades:
            # Unmeasured, not zero and not average: a symbol with no record is
            # one to refuse rather than trade small.
            return self._competence(
                venue_id, symbol, regime, NOT_MEASURED, None, hit_rate, trades,
                coverage, forgetting,
                f"{trades} closed trade(s) of the {self._minimum_trades} needed in {regime}; "
                f"no record is a reason to refuse, not a reason to trade small",
            )

        if coverage is not None and coverage < self._minimum_coverage:
            self.standing.contexts_thin += 1
            return self._competence(
                venue_id, symbol, regime, COVERAGE_TOO_THIN, None, hit_rate, trades,
                coverage, forgetting,
                f"the system acted on {coverage:.0%} of this symbol's opportunities, below "
                f"the {self._minimum_coverage:.0%} needed for a record to mean anything -- "
                f"the sample is the trades it happened to like, which is selection bias",
            )

        competence = hit_rate.value
        if forgetting is not None:
            # A model that has stopped recalling what it learned is less
            # competent than its trade record says, and only the forgetting
            # report can see that.
            competence *= forgetting

        if (
            self.standing.highest_competence_seen is None
            or competence > self.standing.highest_competence_seen
        ):
            self.standing.highest_competence_seen = competence

        state = COMPETENT if competence >= self._threshold else NOT_COMPETENT
        if state == COMPETENT:
            self.standing.contexts_competent += 1

        return self._competence(
            venue_id, symbol, regime, state, competence, hit_rate, trades,
            coverage, forgetting,
            f"{hit_rate.value:.0%} over {trades} closed trade(s) in {regime}"
            + (
                f", reduced to {competence:.0%} because this model recalls {forgetting:.0%} "
                f"of what it learned"
                if forgetting is not None and forgetting < 1.0
                else ""
            )
            + f", against the {self._threshold:.0%} this system treats as competent. Measured "
            f"in this context and not inherited: excellence on one symbol in a trend does not "
            f"license a new listing in a chop",
        )

    def map(self) -> CompetenceMap:
        self.standing.maps_produced += 1
        entries = tuple(
            self.competence_in(venue_id, symbol, regime)
            for venue_id, symbol, regime in sorted(self._records)
        )
        measured = [entry for entry in entries if entry.is_measured]
        competent = [entry for entry in entries if entry.is_competent]
        return CompetenceMap(
            entries=entries,
            contexts_measured=len(measured),
            contexts_competent=len(competent),
            contexts_unmeasured=len(entries) - len(measured),
            best=max(measured, key=lambda entry: entry.competence, default=None),
            worst=min(measured, key=lambda entry: entry.competence, default=None),
            reason=(
                f"{len(entries)} context(s): {len(measured)} measured, {len(competent)} "
                f"competent, {len(entries) - len(measured)} with no record. A context with no "
                f"record renders as its own state rather than as average"
            ),
            mapped_at_ns=self._now_ns(),
        )

    def _estimator_for(self, key) -> RateEstimator:
        estimator = self._records.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._records[key] = estimator
        return estimator

    def _competence(
        self, venue_id, symbol, regime, state, competence, hit_rate, trades,
        coverage, forgetting, reason,
    ) -> Competence:
        return Competence(
            venue_id=venue_id,
            symbol=symbol,
            regime=regime,
            state=state,
            competence=competence,
            hit_rate=hit_rate,
            trades=trades,
            coverage=coverage,
            forgetting=forgetting,
            reason=reason,
            reported_at_ns=self._now_ns(),
        )


def describe_competence(reporter: SelfModelReporter) -> dict:
    competence_map = reporter.map()
    return {
        "part_id": PART_ID,
        "maps_produced": reporter.standing.maps_produced,
        "trades_learned_from": reporter.standing.trades_learned_from,
        "contexts_tracked": reporter.standing.contexts_tracked,
        "contexts_measured": competence_map.contexts_measured,
        "contexts_competent": competence_map.contexts_competent,
        "contexts_unmeasured": competence_map.contexts_unmeasured,
        "contexts_with_too_thin_coverage": reporter.standing.contexts_thin,
        "highest_competence_seen": reporter.standing.highest_competence_seen,
        "best_context": None if competence_map.best is None else
            f"{competence_map.best.symbol}/{competence_map.best.regime}",
    }


def run_self_model_reporter(
    reporter: SelfModelReporter, control_socket, read_records, publish_map,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_records(reporter)
        publish_map(reporter.map())

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

    Coverage arrives per venue, symbol and regime and is observed as such.
    A closed trade with its venue, symbol and regime is what a competence
    is built from, and neither scorecard on this part's inputs carries all
    three: a bot scorecard aggregates across symbols, an instruction
    scorecard names no context. They are read and drained, and every
    context the coverage reports name is mapped as unmeasured -- the true
    state until a record with a context reaches this part. A forgetting
    report names a model, not a context, and is drained too.
    """
    from runtime.input_assembly import Batch

    coverage = Batch(read=context.bus.reader("coverage-report"))
    drained = tuple(
        Batch(read=context.bus.reader(name))
        for name in ("bot-scorecard", "instruction-scorecard", "forgetting-report")
    )
    publish_map = context.bus.publisher_for("competence-map")
    reporter = SelfModelReporter(
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_trades=int(context.number("decoding_minimum_trades")),
        minimum_coverage=context.number("competence_minimum_coverage"),
        competence_threshold=context.number("hypothesis_working_threshold"),
    )

    def read_records(_reporter) -> None:
        for source in drained:
            source.payloads()
        for report in coverage.payloads():
            if report.coverage is not None:
                reporter.observe_coverage(report.venue_id, report.symbol, float(report.coverage))

    def publish(competence_map) -> None:
        if competence_map is not None:
            publish_map((competence_map,))

    return run_self_model_reporter(
        reporter=reporter,
        control_socket=context.control_socket,
        read_records=read_records,
        publish_map=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
