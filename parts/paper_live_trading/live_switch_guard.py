"""live-switch-guard: refuse live money until the bots have graduated on paper (RL-005).

RL-005 is explicit: paper trading first, with full experimentation and no
restrictions, and what earns profit there goes live. This part is what makes that
a mechanism rather than an intention.

The operator can set `money_mode = live` at any time. This part does not stop
them -- it issues a zero risk limit while the segment's bots have not met the
graduation bar, so a premature switch results in a live segment that places no
orders rather than in a live segment that places bad ones. The distinction
matters: the operator sees a clear reason, and no capital moves while they read it.

Graduation is measured on **paper results only**, and on four things at once,
because each alone is gameable:

- **Enough closed trades** to distinguish skill from luck.
- **A positive result after costs**, since a strategy profitable before fees is
  not profitable.
- **A drawdown inside what the operator will tolerate**, because a strategy that
  earns well and draws down 60% will be turned off by a human at the worst moment.
- **Enough elapsed time**, so a bot cannot graduate on a single favourable
  afternoon.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "live-switch-guard"

PART_DECLARATION = PartDeclaration(
    part_id="live-switch-guard",
    consumes=("money-mode", "bot-maturity", "bot-scorecard", "closed-trade", "drawdown-episode"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PAPER = "paper"
LIVE = "live"

GRADUATED = "graduated"
NOT_GRADUATED = "not-graduated"
NO_RECORD = "no-paper-record"

# Nanoseconds in a day, for turning a span of trades into the elapsed time the
# fourth test is about. A unit conversion, not a decision (RL-061).
NANOSECONDS_PER_DAY = 86_400 * 1_000_000_000


@dataclass(frozen=True)
class BotMaturity:
    """What one bot has actually done on paper."""

    bot_id: str
    closed_trades: int
    net_result_after_costs: float
    worst_drawdown_fraction: float
    days_traded: float
    # The regimes edge-graduation-gate says this bot's edge has graduated in.
    # Empty is the honest state before that gate has judged anything, and it is
    # not the same as a bot judged and found immature -- both refuse, and the
    # verdict says which.
    mature_in_regimes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraduationVerdict:
    """Whether a bot has earned real money, and every test it did or did not pass."""

    bot_id: str
    verdict: str
    failures: tuple[str, ...]
    maturity: BotMaturity | None
    reason: str
    judged_at_ns: int

    @property
    def has_graduated(self) -> bool:
        return self.verdict == GRADUATED


@dataclass
class GuardStanding:
    judgements: int = 0
    graduated: int = 0
    refused: int = 0
    live_blocked: int = 0
    bots_known: int = 0
    closed_trades_seen: int = 0
    worst_drawdown_fraction: float = 0.0
    failures_by_test: dict = field(default_factory=dict)


class LiveSwitchGuard:
    """Zeroes the risk limit while a live segment's bots have not graduated on paper."""

    def __init__(
        self,
        minimum_closed_trades: int,
        minimum_days_traded: float,
        maximum_drawdown_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_closed_trades < 1 or minimum_days_traded <= 0:
            raise ValueError("graduation needs both trades and time; neither alone is evidence")
        self._minimum_trades = minimum_closed_trades
        self._minimum_days = minimum_days_traded
        self._maximum_drawdown = maximum_drawdown_fraction
        self._now_ns = now_ns
        self._trades_by_bot: dict[str, int] = {}
        self._mature_regimes: dict[str, set[str]] = {}
        # The segment's own record. Net result, drawdown and elapsed time are not
        # per bot: a closed trade carries no attribution to the bot whose opinion
        # opened it, and inventing one here would be a worse answer than saying so.
        self._net_after_costs = 0.0
        self._worst_drawdown_fraction = 0.0
        self._earliest_open_ns: int | None = None
        self._latest_close_ns: int | None = None
        self.standing = GuardStanding()

    def observe_paper_maturity(self, maturity: BotMaturity) -> None:
        """One bot's whole record, already assembled. Used by tests and by callers
        that have the four numbers in hand; the live part builds it from the four
        wires that measure them."""
        self._trades_by_bot[maturity.bot_id] = maturity.closed_trades
        self._net_after_costs = maturity.net_result_after_costs
        self._worst_drawdown_fraction = maturity.worst_drawdown_fraction
        if maturity.days_traded > 0:
            span = int(maturity.days_traded * NANOSECONDS_PER_DAY)
            self._latest_close_ns = self._latest_close_ns or span
            self._earliest_open_ns = self._latest_close_ns - span
        self._mature_regimes[maturity.bot_id] = set(maturity.mature_in_regimes)
        self.standing.bots_known = len(self._trades_by_bot)

    def observe_bot_trades(self, bot_id: str, closed_trades: int) -> None:
        """How many closed trades this bot's own opinions are credited with.

        From `bot-scorecard`, which is the part that attributes an outcome to the
        bot that gave the opinion -- and which collapses ten correlated entries in
        one setup into one bet, so this count cannot be inflated by trading the
        same idea ten ways.
        """
        self._trades_by_bot[bot_id] = closed_trades
        self.standing.bots_known = len(self._trades_by_bot)

    def observe_edge_maturity(self, bot_id: str, regime: str, is_mature: bool) -> None:
        """edge-graduation-gate's verdict on this bot's edge in one regime."""
        regimes = self._mature_regimes.setdefault(bot_id, set())
        if is_mature:
            regimes.add(regime)
        else:
            regimes.discard(regime)

    def observe_closed_trade(self, realised_pnl: float, fees_paid: float,
                             opened_at_ns: int, closed_at_ns: int) -> None:
        """One round trip, net of what it cost. Gross would answer a different question."""
        self._net_after_costs += realised_pnl - fees_paid
        self.standing.closed_trades_seen += 1
        if self._earliest_open_ns is None or opened_at_ns < self._earliest_open_ns:
            self._earliest_open_ns = opened_at_ns
        if self._latest_close_ns is None or closed_at_ns > self._latest_close_ns:
            self._latest_close_ns = closed_at_ns

    def observe_drawdown(self, depth_fraction: float) -> None:
        """The deepest fall from an equity peak seen so far."""
        self._worst_drawdown_fraction = max(self._worst_drawdown_fraction, depth_fraction)
        self.standing.worst_drawdown_fraction = self._worst_drawdown_fraction

    @property
    def days_traded(self) -> float:
        """The span of the trading record this part has seen, in days.

        Measured from the earliest trade **it has seen**, so a restart starts the
        clock again. That delays graduation and never hastens it, which is the
        direction a guard should fail in.
        """
        if self._earliest_open_ns is None or self._latest_close_ns is None:
            return 0.0
        return max(0.0, (self._latest_close_ns - self._earliest_open_ns) / NANOSECONDS_PER_DAY)

    def maturity_of(self, bot_id: str) -> BotMaturity | None:
        """The record judged for one bot: its own trade count, the segment's result."""
        if bot_id not in self._trades_by_bot and bot_id not in self._mature_regimes:
            return None
        return BotMaturity(
            bot_id=bot_id,
            closed_trades=self._trades_by_bot.get(bot_id, 0),
            net_result_after_costs=self._net_after_costs,
            worst_drawdown_fraction=self._worst_drawdown_fraction,
            days_traded=self.days_traded,
            mature_in_regimes=tuple(sorted(self._mature_regimes.get(bot_id, ()))),
        )

    def judge(self, bot_id: str) -> GraduationVerdict:
        self.standing.judgements += 1
        maturity = self.maturity_of(bot_id)

        if maturity is None:
            self.standing.refused += 1
            return GraduationVerdict(
                bot_id=bot_id, verdict=NO_RECORD, failures=("no paper record",),
                maturity=None,
                reason=f"{bot_id} has no paper trading record; there is nothing to graduate on",
                judged_at_ns=self._now_ns(),
            )

        failures: list[str] = []
        if maturity.closed_trades < self._minimum_trades:
            failures.append(
                f"{maturity.closed_trades} closed trades of the {self._minimum_trades} needed "
                f"to tell skill from luck"
            )
        if maturity.days_traded < self._minimum_days:
            failures.append(
                f"{maturity.days_traded:.1f} days traded of the {self._minimum_days:.1f} needed; "
                f"a bot must not graduate on one favourable afternoon"
            )
        if maturity.net_result_after_costs <= 0:
            failures.append(
                f"net result after costs is {maturity.net_result_after_costs:,.2f}; a strategy "
                f"profitable before fees is not profitable"
            )
        if maturity.worst_drawdown_fraction > self._maximum_drawdown:
            failures.append(
                f"worst drawdown {maturity.worst_drawdown_fraction:.1%} exceeds the "
                f"{self._maximum_drawdown:.1%} tolerated; a human turns that off at the worst moment"
            )
        if not maturity.mature_in_regimes:
            failures.append(
                "its edge has graduated in no regime; a bot whose edge is mature nowhere "
                "has an account record and no reason for it"
            )

        for failure in failures:
            key = failure.split(" of the ")[0].split(" is ")[0][:40]
            self.standing.failures_by_test[key] = self.standing.failures_by_test.get(key, 0) + 1

        if failures:
            self.standing.refused += 1
            return GraduationVerdict(
                bot_id=bot_id, verdict=NOT_GRADUATED, failures=tuple(failures), maturity=maturity,
                reason="; ".join(failures), judged_at_ns=self._now_ns(),
            )

        self.standing.graduated += 1
        return GraduationVerdict(
            bot_id=bot_id, verdict=GRADUATED, failures=(), maturity=maturity,
            reason=(
                f"{maturity.closed_trades} paper trades over {maturity.days_traded:.1f} days, "
                f"{maturity.net_result_after_costs:,.2f} after costs, worst drawdown "
                f"{maturity.worst_drawdown_fraction:.1%}, edge mature in "
                f"{', '.join(maturity.mature_in_regimes)}"
            ),
            judged_at_ns=self._now_ns(),
        )

    def read_limit(self, money_mode: str, bot_ids: tuple[str, ...]) -> RiskLimit:
        """Zero while the segment is live and any of its bots has not graduated.

        Any, not all: one ungraduated bot in a live segment can lose real money,
        and the segment shares one allocation.
        """
        if money_mode != LIVE:
            return RiskLimit(
                limiter=PART_ID,
                fraction_of_allotment=1.0,
                reason="this segment is on paper money; graduation is not required to trade it",
                is_binding=False,
                decided_at_ns=self._now_ns(),
            )

        verdicts = [self.judge(bot_id) for bot_id in bot_ids]
        ungraduated = [verdict for verdict in verdicts if not verdict.has_graduated]

        if not bot_ids:
            self.standing.live_blocked += 1
            return RiskLimit(
                limiter=PART_ID,
                fraction_of_allotment=NO_RISK_ALLOWED,
                reason="the segment is live with no bots; nothing has graduated because "
                "nothing has traded",
                is_binding=True,
                decided_at_ns=self._now_ns(),
            )

        if ungraduated:
            self.standing.live_blocked += 1
            return RiskLimit(
                limiter=PART_ID,
                fraction_of_allotment=NO_RISK_ALLOWED,
                reason=(
                    f"live money is set but {len(ungraduated)} of {len(bot_ids)} bot(s) have not "
                    f"graduated on paper (RL-005): "
                    + "; ".join(f"{v.bot_id}: {v.reason}" for v in ungraduated[:2])
                ),
                is_binding=True,
                decided_at_ns=self._now_ns(),
            )

        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=1.0,
            reason=f"all {len(bot_ids)} bot(s) graduated on paper",
            is_binding=False,
            decided_at_ns=self._now_ns(),
        )


def describe_guard(guard: LiveSwitchGuard) -> dict:
    return {
        "part_id": PART_ID,
        "judgements": guard.standing.judgements,
        "graduated": guard.standing.graduated,
        "refused": guard.standing.refused,
        "live_blocked": guard.standing.live_blocked,
        "bots_known": guard.standing.bots_known,
        "closed_trades_seen": guard.standing.closed_trades_seen,
        "worst_drawdown_fraction": guard.standing.worst_drawdown_fraction,
        "days_traded": guard.days_traded,
        "failures_by_test": dict(guard.standing.failures_by_test),
    }


def run_live_switch_guard(
    guard: LiveSwitchGuard, control_socket, read_mode_and_bots, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        money_mode, bot_ids = read_mode_and_bots(guard)
        publish_limit(guard.read_limit(money_mode, bot_ids))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_guard(guard),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    On paper the guard publishes the full limit and judges nothing, which is
    the case today. Live, each of the five tests is read from the part that
    measures it: the bot's own trade count from its scorecard, the net result
    and the elapsed span from the closed trades themselves, the worst drawdown
    from the equity peaks, and the edge verdict from the graduation gate.

    Until 2026-08-25 all four numbers were read off `bot-maturity`, which carries
    one of them, so three tests were judged against their getattr defaults --
    a net of 0.0, a drawdown of 1.0 and 0.0 days. It refused everything, which is
    the safe direction and the reason nobody noticed.
    """
    from runtime.input_assembly import Batch, LatestByKey

    modes = LatestByKey(read=context.bus.reader("money-mode"), key_of=lambda m: m.segment)
    maturities = Batch(read=context.bus.reader("bot-maturity"))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    closed_trades = Batch(read=context.bus.reader("closed-trade"))
    drawdowns = Batch(read=context.bus.reader("drawdown-episode"))
    publish_limits = context.bus.publisher_for("risk-limit")
    segment = str(context.setting("segment_id").value)
    guard = LiveSwitchGuard(
        minimum_closed_trades=int(context.number("live_switch_minimum_closed_trades")),
        minimum_days_traded=context.number("live_switch_minimum_days_traded"),
        maximum_drawdown_fraction=context.number("live_switch_maximum_drawdown_fraction"),
    )
    bots_seen: set[str] = set()

    def read_mode_and_bots(_guard):
        for maturity in maturities.payloads():
            bots_seen.add(maturity.bot)
            guard.observe_edge_maturity(maturity.bot, maturity.regime, maturity.is_mature)
        for scorecard in scorecards.payloads():
            bots_seen.add(scorecard.bot)
            guard.observe_bot_trades(scorecard.bot, scorecard.trades)
        for trade in closed_trades.payloads():
            guard.observe_closed_trade(
                trade.realised_pnl, trade.fees_paid, trade.opened_at_ns, trade.closed_at_ns
            )
        for episode in drawdowns.payloads():
            guard.observe_drawdown(episode.depth_fraction)
        mode = modes.mapping().get(segment)
        return (mode.mode if mode is not None else PAPER), tuple(sorted(bots_seen))

    return run_live_switch_guard(
        guard=guard,
        control_socket=context.control_socket,
        read_mode_and_bots=read_mode_and_bots,
        publish_limit=lambda limit: publish_limits((limit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
