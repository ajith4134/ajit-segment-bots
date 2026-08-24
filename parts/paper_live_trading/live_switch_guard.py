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
    consumes=("money-mode", "bot-maturity"),
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


@dataclass(frozen=True)
class BotMaturity:
    """What one bot has actually done on paper."""

    bot_id: str
    closed_trades: int
    net_result_after_costs: float
    worst_drawdown_fraction: float
    days_traded: float


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
        self._maturity: dict[str, BotMaturity] = {}
        self.standing = GuardStanding()

    def observe_paper_maturity(self, maturity: BotMaturity) -> None:
        """One bot's paper record. Only paper results are ever admitted here."""
        self._maturity[maturity.bot_id] = maturity
        self.standing.bots_known = len(self._maturity)

    def judge(self, bot_id: str) -> GraduationVerdict:
        self.standing.judgements += 1
        maturity = self._maturity.get(bot_id)

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
                f"{maturity.worst_drawdown_fraction:.1%}"
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
    the case today. The maturity edge-graduation-gate publishes carries the
    bot's trade count and its verdict but not the net result, worst drawdown
    or days traded this guard judges live on; those are recorded here as
    unknown -- zero result, total drawdown, no days -- so a segment switched
    live before the maturity type carries them is refused, and the refusal
    names what is missing. A guard that filled them in would be the
    graduation it is meant to check.
    """
    from runtime.input_assembly import Batch, LatestByKey

    modes = LatestByKey(read=context.bus.reader("money-mode"), key_of=lambda m: m.segment)
    maturities = Batch(read=context.bus.reader("bot-maturity"))
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
            bot_id = getattr(maturity, "bot", None) or getattr(maturity, "bot_id", "")
            bots_seen.add(bot_id)
            guard.observe_paper_maturity(
                BotMaturity(
                    bot_id=bot_id,
                    closed_trades=int(getattr(maturity, "trades_here", getattr(maturity, "closed_trades", 0))),
                    net_result_after_costs=float(getattr(maturity, "net_result_after_costs", 0.0)),
                    worst_drawdown_fraction=float(getattr(maturity, "worst_drawdown_fraction", 1.0)),
                    days_traded=float(getattr(maturity, "days_traded", 0.0)),
                )
            )
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
