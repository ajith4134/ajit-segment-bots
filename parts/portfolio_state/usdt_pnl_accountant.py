"""usdt-pnl-accountant: each trade's profit or loss in USDT against the capital it used (RL-028)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "usdt-pnl-accountant"

PART_DECLARATION = PartDeclaration(
    part_id="usdt-pnl-accountant",
    consumes=(
        "closed-trade", "fill", "market-data", "capital-allotment",
        "cost-basis", "funding-settlement", "paper-currency-rate",
    ),
    produces=("usdt-pnl-statement", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)


@dataclass(frozen=True)
class UsdtPnlStatement:
    """One trade's result in USDT, with every component kept separate.

    Gross, fees and funding are not summed away: a strategy that is profitable
    gross and loses to funding is a different problem from one that never had an
    edge, and a single net figure cannot tell them apart.
    """

    venue_id: str
    symbol: str
    direction: str
    gross_pnl_usdt: float
    fees_usdt: float
    funding_usdt: float
    net_pnl_usdt: float
    capital_used_usdt: float | None
    return_on_capital: float | None
    quote_currency: str
    conversion_rate: float
    holding_seconds: float
    reason: str
    stated_at_ns: int


@dataclass
class AccountantStanding:
    statements: int = 0
    without_capital: int = 0
    non_usdt_converted: int = 0
    funding_applied: int = 0
    net_total_usdt: float = 0.0


class UsdtPnlAccountant:
    """States every result in USDT, converting where a symbol is quoted otherwise.

    RL-028: one currency, so results are comparable across symbols and venues. A
    USDC-quoted perpetual settles in a different unit, and adding the two numbers
    without converting is an error nothing downstream can see.

    A trade whose capital allotment is unknown gets a net figure and no return:
    the profit is a fact, the return on capital is not, and inventing a
    denominator would make an unsized trade look like a great one.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._rates: dict[str, float] = {"USDT": 1.0}
        self._capital: dict[tuple[str, str], float] = {}
        self._funding: dict[tuple[str, str], float] = {}
        self.standing = AccountantStanding()

    def set_conversion_rate(self, quote_currency: str, rate_to_usdt: float) -> None:
        self._rates[quote_currency] = rate_to_usdt

    def set_capital_allotment(self, venue_id: str, symbol: str, capital_usdt: float) -> None:
        self._capital[(venue_id, symbol)] = capital_usdt

    def record_funding(self, venue_id: str, symbol: str, amount_quote: float) -> None:
        key = (venue_id, symbol)
        self._funding[key] = self._funding.get(key, 0.0) + amount_quote
        self.standing.funding_applied += 1

    def state(self, trade, quote_currency: str = "USDT") -> UsdtPnlStatement:
        rate = self._rates.get(quote_currency)
        if rate is None:
            rate = 1.0
            reason = f"no rate for {quote_currency}; stated at parity and flagged"
        else:
            reason = "converted at the recorded rate" if quote_currency != "USDT" else "quoted in USDT"
        if quote_currency != "USDT":
            self.standing.non_usdt_converted += 1

        key = (trade.venue_id, trade.symbol)
        gross = trade.realised_pnl * rate
        fees = trade.fees_paid * rate
        funding = self._funding.pop(key, 0.0) * rate
        net = gross - fees + funding

        capital = self._capital.get(key)
        if capital is None:
            self.standing.without_capital += 1
        return_on_capital = (net / capital) if capital else None

        self.standing.statements += 1
        self.standing.net_total_usdt += net
        return UsdtPnlStatement(
            venue_id=trade.venue_id,
            symbol=trade.symbol,
            direction=trade.direction,
            gross_pnl_usdt=gross,
            fees_usdt=fees,
            funding_usdt=funding,
            net_pnl_usdt=net,
            capital_used_usdt=capital,
            return_on_capital=return_on_capital,
            quote_currency=quote_currency,
            conversion_rate=rate,
            holding_seconds=trade.holding_seconds,
            reason=reason,
            stated_at_ns=self._now_ns(),
        )


def describe_pnl(accountant: UsdtPnlAccountant) -> dict:
    return {
        "part_id": PART_ID,
        "statements": accountant.standing.statements,
        "net_total_usdt": accountant.standing.net_total_usdt,
        "statements_without_capital": accountant.standing.without_capital,
        "non_usdt_converted": accountant.standing.non_usdt_converted,
        "funding_events_applied": accountant.standing.funding_applied,
    }


def run_usdt_pnl_accountant(
    accountant: UsdtPnlAccountant, control_socket, read_closed_trades, publish_statements,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for trade, quote_currency in read_closed_trades():
            publish_statements(accountant.state(trade, quote_currency))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
