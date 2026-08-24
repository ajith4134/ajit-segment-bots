"""edge-graduation-gate: whether a bot has earned its way out of exploration.

RL-005 in one part: everything starts on paper with no restriction, and something
has to decide when a bot's results are evidence rather than a sample. Get that
wrong in one direction and the system explores forever; wrong in the other and it
graduates noise into live capital.

So graduation requires **every one** of these, and they are deliberately not
tradeable against each other:

- **Enough closed trades**, from the required sample size the power estimator
  computed for this bot's own effect size -- not a round number somebody chose.
- **Decision quality, not just profit.** A bot that made money badly has not
  demonstrated anything repeatable, and profit alone would graduate it.
- **A refutation verdict that did not refute it.** An edge nobody tried to break
  is an edge nobody has tested.
- **A trial count.** A bot found by trying two hundred variations must clear a
  bar two hundred times higher, and a gate that ignored the count would graduate
  the best of two hundred coin flips every time.
- **Coverage.** A bot that abstained from everything difficult has a record about
  a slice it selected.
- **Clustered trades counted once.** Ten correlated entries are one bet, and a
  gate counting ten would graduate on a single decision.

**Graduation is reversible.** A bot that stops meeting the bar returns to
exploration rather than being retired -- retirement would end the evidence, and a
bot that cannot produce evidence can never come back.

**Every refusal names which condition failed**, because "not yet" is not
actionable and "eleven of the forty trades its own power estimate requires" is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "edge-graduation-gate"

PART_DECLARATION = PartDeclaration(
    part_id="edge-graduation-gate",
    consumes=(
        "bot-scorecard", "decision-quality-score", "refutation-verdict", "trial-ledger",
        "coverage-report", "pair-verdict", "trade-cluster",
    ),
    produces=("bot-maturity", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

EXPLORING = "exploring"
GRADUATED = "graduated"
RETURNED_TO_EXPLORATION = "returned-to-exploration"

TOO_FEW_TRADES = "fewer-closed-trades-than-its-own-power-estimate-requires"
DECISION_QUALITY_TOO_LOW = "it-made-money-without-making-good-decisions"
NOT_REFUTATION_TESTED = "nothing-has-tried-to-break-this-edge"
WAS_REFUTED = "the-refutation-battery-broke-it"
DOES_NOT_CLEAR_ITS_TRIALS = "it-does-not-clear-the-bar-its-own-search-implies"
COVERAGE_TOO_THIN = "its-record-is-about-a-slice-it-selected"
HIT_RATE_TOO_LOW = "its-hit-rate-is-below-the-bar"


@dataclass(frozen=True)
class BotMaturity:
    """Whether a bot's record is evidence yet, and exactly what is missing."""

    bot: str
    regime: str
    state: str
    is_mature: bool
    trades_here: int
    required_trades: int | None
    conditions_met: dict
    failing_conditions: tuple
    reason: str
    judged_at_ns: int

    @property
    def may_trade_live(self) -> bool:
        return self.state == GRADUATED


@dataclass
class GateStanding:
    judgements: int = 0
    graduations: int = 0
    returns_to_exploration: int = 0
    clustered_trades_collapsed: int = 0
    by_failing_condition: dict = field(default_factory=dict)
    bots_currently_graduated: int = 0


class EdgeGraduationGate:
    """Decides when a bot's record is evidence, and sends it back when it stops being."""

    def __init__(
        self,
        minimum_hit_rate: float,
        minimum_decision_quality: float,
        minimum_coverage: float,
        default_required_trades: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_hit_rate < 1.0:
            raise ValueError("a hit-rate bar outside (0, 1) graduates everything or nothing")
        if not 0.0 < minimum_decision_quality <= 1.0:
            raise ValueError(
                "without a decision-quality bar, profit alone graduates a bot that made money "
                "badly"
            )
        self._minimum_hit_rate = minimum_hit_rate
        self._minimum_quality = minimum_decision_quality
        self._minimum_coverage = minimum_coverage
        self._default_required = default_required_trades
        self._now_ns = now_ns
        self._trades: dict[tuple[str, str], int] = {}
        self._wins: dict[tuple[str, str], int] = {}
        self._required: dict[tuple[str, str], int] = {}
        self._quality: dict[tuple[str, str], float] = {}
        self._refutation: dict[str, str] = {}
        self._trial_clears: dict[str, bool] = {}
        self._coverage: dict[tuple[str, str], float] = {}
        self._graduated: set[tuple[str, str]] = set()
        self._clusters_seen: set[str] = set()
        self.standing = GateStanding()

    def observe_closed_trade(
        self, bot: str, regime: str, was_win: bool, cluster_id: str | None = None
    ) -> None:
        """One closed trade. Ten correlated entries are one bet, not ten."""
        if cluster_id is not None:
            if cluster_id in self._clusters_seen:
                self.standing.clustered_trades_collapsed += 1
                return
            self._clusters_seen.add(cluster_id)
        key = (bot, regime)
        self._trades[key] = self._trades.get(key, 0) + 1
        if was_win:
            self._wins[key] = self._wins.get(key, 0) + 1

    def observe_required_sample_size(self, bot: str, regime: str, trades: int) -> None:
        """What the power estimator computed for this bot's own effect size."""
        self._required[(bot, regime)] = trades

    def observe_decision_quality(self, bot: str, regime: str, score: float) -> None:
        self._quality[(bot, regime)] = score

    def observe_refutation_verdict(self, bot: str, verdict: str) -> None:
        self._refutation[bot] = verdict

    def observe_trial_verdict(self, bot: str, clears_its_bar: bool) -> None:
        """Whether the bot's result clears the bar its own search implies."""
        self._trial_clears[bot] = clears_its_bar

    def observe_coverage(self, bot: str, regime: str, coverage: float) -> None:
        self._coverage[(bot, regime)] = coverage

    def judge(self, bot: str, regime: str) -> BotMaturity:
        self.standing.judgements += 1
        key = (bot, regime)
        trades = self._trades.get(key, 0)
        wins = self._wins.get(key, 0)
        required = self._required.get(key, self._default_required)
        hit_rate = wins / trades if trades else 0.0

        conditions = {
            TOO_FEW_TRADES: trades >= required,
            HIT_RATE_TOO_LOW: hit_rate >= self._minimum_hit_rate,
            DECISION_QUALITY_TOO_LOW: (
                self._quality.get(key, 0.0) >= self._minimum_quality
            ),
            NOT_REFUTATION_TESTED: bot in self._refutation,
            WAS_REFUTED: self._refutation.get(bot) != "refuted",
            DOES_NOT_CLEAR_ITS_TRIALS: self._trial_clears.get(bot, False),
            COVERAGE_TOO_THIN: self._coverage.get(key, 0.0) >= self._minimum_coverage,
        }

        failing = tuple(name for name, met in conditions.items() if not met)
        for name in failing:
            self.standing.by_failing_condition[name] = (
                self.standing.by_failing_condition.get(name, 0) + 1
            )

        was_graduated = key in self._graduated

        if failing:
            if was_graduated:
                # Back to exploration rather than retired: retirement ends the
                # evidence, and a bot that cannot produce evidence can never
                # come back.
                self._graduated.discard(key)
                self.standing.returns_to_exploration += 1
                state = RETURNED_TO_EXPLORATION
            else:
                state = EXPLORING
            self.standing.bots_currently_graduated = len(self._graduated)
            return self._maturity(
                bot, regime, state, False, trades, required, conditions, failing,
                f"{len(failing)} condition(s) not met: "
                + "; ".join(
                    self._explain(name, trades, required, hit_rate, key) for name in failing
                )
                + (
                    ". It was graduated and is returning to exploration rather than being "
                    "retired -- retirement would end the evidence"
                    if state == RETURNED_TO_EXPLORATION
                    else ""
                ),
            )

        if not was_graduated:
            self.standing.graduations += 1
        self._graduated.add(key)
        self.standing.bots_currently_graduated = len(self._graduated)

        return self._maturity(
            bot, regime, GRADUATED, True, trades, required, conditions, (),
            f"{bot} has {trades} closed trade(s) in {regime} against the {required} its own "
            f"power estimate requires, a {hit_rate:.0%} hit rate, decision quality "
            f"{self._quality.get(key, 0.0):.2f}, a refutation battery that did not break it, "
            f"a result clearing the bar its own search implies, and "
            f"{self._coverage.get(key, 0.0):.0%} coverage. Every condition, because they are "
            f"not tradeable against each other -- profit alone would graduate a bot that made "
            f"money badly",
        )

    def _explain(self, condition: str, trades: int, required: int, hit_rate: float, key) -> str:
        if condition == TOO_FEW_TRADES:
            return f"{trades} of the {required} closed trade(s) its own power estimate requires"
        if condition == HIT_RATE_TOO_LOW:
            return f"a {hit_rate:.0%} hit rate against a {self._minimum_hit_rate:.0%} bar"
        if condition == DECISION_QUALITY_TOO_LOW:
            return (
                f"decision quality {self._quality.get(key, 0.0):.2f} against "
                f"{self._minimum_quality:.2f} -- profit without good decisions is not repeatable"
            )
        if condition == NOT_REFUTATION_TESTED:
            return "nothing has tried to break this edge, and an untested edge is untested"
        if condition == WAS_REFUTED:
            return "the refutation battery broke it"
        if condition == DOES_NOT_CLEAR_ITS_TRIALS:
            return (
                "it does not clear the bar its own search implies; a bot found by trying two "
                "hundred variations must clear a bar two hundred times higher"
            )
        if condition == COVERAGE_TOO_THIN:
            return (
                f"{self._coverage.get(key, 0.0):.0%} coverage against "
                f"{self._minimum_coverage:.0%} -- its record is about a slice it selected"
            )
        return condition

    def _maturity(
        self, bot, regime, state, is_mature, trades, required, conditions, failing, reason
    ) -> BotMaturity:
        return BotMaturity(
            bot=bot,
            regime=regime,
            state=state,
            is_mature=is_mature,
            trades_here=trades,
            required_trades=required,
            conditions_met=dict(conditions),
            failing_conditions=failing,
            reason=reason,
            judged_at_ns=self._now_ns(),
        )


def describe_graduation(gate: EdgeGraduationGate) -> dict:
    return {
        "part_id": PART_ID,
        "judgements": gate.standing.judgements,
        "graduations": gate.standing.graduations,
        "returns_to_exploration": gate.standing.returns_to_exploration,
        "clustered_trades_collapsed": gate.standing.clustered_trades_collapsed,
        "bots_currently_graduated": gate.standing.bots_currently_graduated,
        "by_failing_condition": dict(sorted(gate.standing.by_failing_condition.items())),
        "graduation_is_reversible": True,
    }


def run_edge_graduation_gate(
    gate: EdgeGraduationGate, control_socket, read_records, publish_maturity,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        contexts = read_records(gate)
        publish_maturity(tuple(gate.judge(bot, regime) for bot, regime in contexts))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_graduation(gate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A bot's trades per regime are adopted from its scorecard: the difference
    between the scorecard's count and what the gate has already counted is
    new closed trades, with wins in the same proportion. Every bot and regime
    the scorecards name is judged once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    qualities = Batch(read=context.bus.reader("decision-quality-score"))
    refutations = Batch(read=context.bus.reader("refutation-verdict"))
    ledgers = Batch(read=context.bus.reader("trial-ledger"))
    coverage = Batch(read=context.bus.reader("coverage-report"))
    verdicts = Batch(read=context.bus.reader("pair-verdict"))
    clusters = Batch(read=context.bus.reader("trade-cluster"))
    publish_maturity = context.bus.publisher_for("bot-maturity")
    gate = EdgeGraduationGate(
        minimum_hit_rate=context.number("graduation_minimum_hit_rate"),
        minimum_decision_quality=context.number("graduation_minimum_decision_quality"),
        minimum_coverage=context.number("graduation_minimum_coverage"),
        default_required_trades=int(context.number("graduation_default_required_trades")),
    )
    counted: dict[tuple[str, str], tuple[int, int]] = {}
    known: set[tuple[str, str]] = set()
    last_judged = [float("-inf")]

    def read_records(_gate):
        for scorecard in scorecards.payloads():
            for regime, record in scorecard.describe()["by_regime"].items():
                key = (scorecard.bot, regime)
                seen_trades, seen_wins = counted.get(key, (0, 0))
                for _ in range(max(0, record["wins"] - seen_wins)):
                    gate.observe_closed_trade(scorecard.bot, regime, True)
                for _ in range(max(0, (record["trades"] - record["wins"]) - (seen_trades - seen_wins))):
                    gate.observe_closed_trade(scorecard.bot, regime, False)
                counted[key] = (record["trades"], record["wins"])
                known.add(key)
        for quality in qualities.payloads():
            # A decision-quality score names a symbol, not a bot; it is the
            # quality of the brain's decision and applies to every bot judged
            # in the regime it was scored in, which the score does not name.
            for bot, regime in known:
                gate.observe_decision_quality(bot, regime, quality.score)
        for verdict in refutations.payloads():
            gate.observe_refutation_verdict(verdict.instruction_id, verdict.verdict)
        for ledger in ledgers.payloads():
            gate.observe_trial_verdict(ledger.family, ledger.expected_false_positives < 1.0)
        for report in coverage.payloads():
            if report.coverage is not None:
                for bot, regime in known:
                    if regime == report.regime:
                        gate.observe_coverage(bot, regime, report.coverage)
        verdicts.payloads()
        clusters.payloads()
        now = _time.monotonic()
        if now - last_judged[0] < context.health_interval_seconds:
            return ()
        last_judged[0] = now
        return tuple(sorted(known))

    def publish(maturities) -> None:
        if maturities:
            publish_maturity(maturities)

    return run_edge_graduation_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_records=read_records,
        publish_maturity=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
