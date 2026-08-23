"""decision-cost-accountant: what a decision cost to make, against what it made.

Nothing in a normal PnL statement shows the cost of thinking. A system can spend
more reasoning about a trade than the trade returns, and every report will still
show a profit -- because the reasoning is an operating expense and the trade is a
trading result, and they live in different columns.

This part puts them in the same column, per decision. It is the one number that
makes an expensive prompt visible as what it is: a strategy with a negative
expectancy hiding inside a strategy with a positive one.

Four things it insists on:

- **Costs attach to a decision, not to a part or a period.** A per-part monthly bill
  cannot say whether the reasoning was worth it. The unit is the decision, because
  that is the unit that has a return.
- **The two currencies stay separate.** Metered money is spent and gone;
  subscription quota refills. Netting them into one "cost" hides the irreversible
  half, so both travel and only money is compared against PnL.
- **Nothing is judged until the trade closes.** An open position has no realised
  return, and comparing cost against an unrealised mark rewards whichever way the
  market happened to move today.
- **A decision that led to no trade still cost something.** Those are the ones a
  cost report misses entirely, and a system that reasons expensively and then stands
  down is spending exactly as much as one that trades.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import DecisionCost, METERED, SUBSCRIPTION
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "decision-cost-accountant"

PART_DECLARATION = PartDeclaration(
    part_id="decision-cost-accountant",
    consumes=("llm-call-record", "trade-intent", "usdt-pnl-statement"),
    produces=("decision-cost", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SETTLED = "settled"
NOT_CLOSED = "the-trade-has-not-closed-so-nothing-is-judged"
NO_TRADE = "the-reasoning-produced-no-trade-and-still-cost-something"
NO_COST = "no-call-was-made-for-this-decision"


@dataclass(frozen=True)
class DecisionAccount:
    decision_id: str
    state: str
    cost: DecisionCost
    calls_by_part: dict
    reason: str
    measured_at_ns: int

    @property
    def was_worth_making(self) -> bool | None:
        if not self.cost.is_settled or self.cost.realised_pnl is None:
            return None
        return self.cost.realised_pnl > self.cost.money_spent


@dataclass
class AccountantStanding:
    decisions_seen: int = 0
    decisions_settled: int = 0
    decisions_still_open: int = 0
    decisions_with_no_trade: int = 0
    decisions_that_cost_more_than_they_made: int = 0
    total_money_spent: float = 0.0
    total_money_spent_on_no_trade: float = 0.0
    total_quota_spent: float = 0.0
    total_realised: float = 0.0


class DecisionCostAccountant:
    """Attributes every call to a decision and settles it against realised PnL."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._calls: dict[str, list] = {}
        self._purpose: dict[str, str] = {}
        self._produced_a_trade: dict[str, bool] = {}
        self._realised: dict[str, float] = {}
        self._settled: set = set()
        self.standing = AccountantStanding()

    def observe_call(self, decision_id: str, record) -> None:
        self._calls.setdefault(decision_id, []).append(record)
        self._purpose.setdefault(decision_id, record.purpose)

    def observe_intent(self, decision_id: str, produced_a_trade: bool) -> None:
        """A decision that stood down still cost what it cost."""
        self._produced_a_trade[decision_id] = produced_a_trade

    def observe_realised(self, decision_id: str, realised_pnl: float) -> None:
        """Only a closed trade settles. An unrealised mark is not a result."""
        self._realised[decision_id] = realised_pnl

    def account(self, decision_id: str) -> DecisionAccount:
        self.standing.decisions_seen += 1
        calls = self._calls.get(decision_id, [])
        money = sum(
            record.money_spent for record in calls if record.payment_kind == METERED
        )
        quota = sum(
            record.quota_spent for record in calls if record.payment_kind == SUBSCRIPTION
        )
        tokens = sum(record.input_tokens + record.output_tokens for record in calls)
        by_part: dict = {}
        for record in calls:
            by_part[record.part_id] = by_part.get(record.part_id, 0) + 1

        if not calls:
            return self._account(
                decision_id, NO_COST,
                DecisionCost(
                    decision_id=decision_id, purpose=self._purpose.get(decision_id, ""),
                    calls=0, money_spent=0.0, quota_spent=0.0, tokens=0.0,
                    realised_pnl=self._realised.get(decision_id), is_settled=False,
                    measured_at_ns=self._now_ns(),
                ),
                by_part,
                "no call was made for this decision, so it cost nothing to reason about",
            )

        realised = self._realised.get(decision_id)
        produced_a_trade = self._produced_a_trade.get(decision_id)

        if produced_a_trade is False:
            self.standing.decisions_with_no_trade += 1
            self.standing.total_money_spent += money
            self.standing.total_money_spent_on_no_trade += money
            self.standing.total_quota_spent += quota
            return self._account(
                decision_id, NO_TRADE,
                DecisionCost(
                    decision_id=decision_id, purpose=self._purpose.get(decision_id, ""),
                    calls=len(calls), money_spent=money, quota_spent=quota,
                    tokens=float(tokens), realised_pnl=0.0, is_settled=True,
                    measured_at_ns=self._now_ns(),
                ),
                by_part,
                f"{len(calls)} call(s) costing {money:.4f} produced no trade. These are the "
                f"decisions a cost report misses entirely: reasoning expensively and then "
                f"standing down spends exactly as much as trading",
            )

        if realised is None:
            self.standing.decisions_still_open += 1
            return self._account(
                decision_id, NOT_CLOSED,
                DecisionCost(
                    decision_id=decision_id, purpose=self._purpose.get(decision_id, ""),
                    calls=len(calls), money_spent=money, quota_spent=quota,
                    tokens=float(tokens), realised_pnl=None, is_settled=False,
                    measured_at_ns=self._now_ns(),
                ),
                by_part,
                f"{len(calls)} call(s) costing {money:.4f} so far, against a position that "
                f"has not closed. Comparing cost against an unrealised mark rewards "
                f"whichever way the market moved today",
            )

        if decision_id not in self._settled:
            self._settled.add(decision_id)
            self.standing.decisions_settled += 1
            self.standing.total_money_spent += money
            self.standing.total_quota_spent += quota
            self.standing.total_realised += realised

        cost = DecisionCost(
            decision_id=decision_id, purpose=self._purpose.get(decision_id, ""),
            calls=len(calls), money_spent=money, quota_spent=quota, tokens=float(tokens),
            realised_pnl=realised, is_settled=True, measured_at_ns=self._now_ns(),
        )
        if cost.cost_exceeded_the_gain:
            self.standing.decisions_that_cost_more_than_they_made += 1

        return self._account(
            decision_id, SETTLED, cost, by_part,
            f"{len(calls)} call(s) across {len(by_part)} part(s): {money:.4f} of money and "
            f"{quota:.1f} of quota against {realised:+.4f} realised"
            + (
                ". The reasoning cost more than the trade returned, which no PnL statement "
                "would have shown"
                if cost.cost_exceeded_the_gain
                else ""
            ),
        )

    def _account(self, decision_id, state, cost, by_part, reason) -> DecisionAccount:
        return DecisionAccount(
            decision_id=decision_id, state=state, cost=cost, calls_by_part=by_part,
            reason=reason, measured_at_ns=self._now_ns(),
        )


def describe_decision_costs(accountant: DecisionCostAccountant) -> dict:
    return {
        "part_id": PART_ID,
        "decisions_seen": accountant.standing.decisions_seen,
        "decisions_settled": accountant.standing.decisions_settled,
        "decisions_still_open": accountant.standing.decisions_still_open,
        "decisions_that_produced_no_trade": accountant.standing.decisions_with_no_trade,
        "decisions_that_cost_more_than_they_made": (
            accountant.standing.decisions_that_cost_more_than_they_made
        ),
        "total_money_spent": accountant.standing.total_money_spent,
        "money_spent_on_decisions_that_did_not_trade": (
            accountant.standing.total_money_spent_on_no_trade
        ),
        "total_quota_spent": accountant.standing.total_quota_spent,
        "total_realised": accountant.standing.total_realised,
        "nets_quota_against_money": False,
        "judges_an_open_position": False,
    }


def run_decision_cost_accountant(
    accountant: DecisionCostAccountant, control_socket, read_decisions, publish_costs,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for decision_id in read_decisions(accountant):
            publish_costs(accountant.account(decision_id).cost)

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

    A decision is one venue and symbol's intent. The calls charged to it
    are the records that arrived since the last intent there for the same
    purpose family -- a call record names no venue, so the link is time
    and purpose, which this part states rather than hides. A PnL statement
    for the venue and symbol settles the decision, and settled decisions
    are accounted.
    """
    from runtime.input_assembly import Batch

    records = Batch(read=context.bus.reader("llm-call-record"))
    intents = Batch(read=context.bus.reader("trade-intent"))
    statements = Batch(read=context.bus.reader("usdt-pnl-statement"))
    publish_costs = context.bus.publisher_for("decision-cost")
    accountant = DecisionCostAccountant()
    open_decision: dict[tuple[str, str], str] = {}
    unassigned: list = []

    def read_decisions(_accountant):
        unassigned.extend(records.payloads())
        settled = []
        for intent in intents.payloads():
            key = (intent.venue_id, intent.symbol)
            decision_id = f"{intent.venue_id}:{intent.symbol}:{intent.formed_at_ns}"
            open_decision[key] = decision_id
            for record in unassigned:
                accountant.observe_call(decision_id, record)
            unassigned.clear()
            accountant.observe_intent(decision_id, str(intent.action) != "stand-aside")
        for statement in statements.payloads():
            decision_id = open_decision.pop((statement.venue_id, statement.symbol), None)
            if decision_id is None:
                continue
            accountant.observe_realised(decision_id, float(statement.net_pnl_usdt))
            settled.append(decision_id)
        return tuple(settled)

    return run_decision_cost_accountant(
        accountant=accountant,
        control_socket=context.control_socket,
        read_decisions=read_decisions,
        publish_costs=lambda cost: publish_costs((cost,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
