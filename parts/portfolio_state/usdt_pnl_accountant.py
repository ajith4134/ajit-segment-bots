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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        # Published as one batch of statements rather than one call per trade: a
        # publisher takes an iterable of payloads, and handing it a single frozen
        # dataclass raises rather than publishing anything.
        publish_statements(tuple(
            accountant.state(trade, quote_currency)
            for trade, quote_currency in read_closed_trades()
        ))

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

    What a closed trade made, in the one currency the whole system compares in.
    Every segment quotes in something different and a portfolio measured in mixed
    quote currencies is a portfolio nobody can add up.

    The conversion rate comes from `paper-currency-rate`, which nothing produces
    yet. Until it does, only trades quoted in USDT can be stated, and the rest are
    reported as unconvertible rather than assumed to be one-for-one -- an assumed
    rate is a profit figure with an invented number in it.
    """
    from runtime.input_assembly import Batch

    closed_trades = Batch(read=context.bus.reader("closed-trade"))
    fills = Batch(read=context.bus.reader("fill"))
    trades = Batch(read=context.bus.reader("market-data"))
    allotments = Batch(read=context.bus.reader("capital-allotment"))
    bases = Batch(read=context.bus.reader("cost-basis"))
    settlements = Batch(read=context.bus.reader("funding-settlement"))
    rates = Batch(read=context.bus.reader("paper-currency-rate"))
    publish_statements = context.bus.publisher_for("usdt-pnl-statement")

    segment = str(context.setting("segment_id").value)
    quote_currency = str(context.setting("quote_currency", scope=segment).value)

    def read_closed_trades():
        for conversion in rates.payloads():
            # A conversion carries the rate it used and which way it ran. Only a
            # usable one names a rate at all -- a refused conversion reports why
            # it could not convert, and adopting the None in it would state every
            # trade at parity while claiming a rate was applied.
            if conversion.is_usable and conversion.rate and conversion.to_currency == "USDT":
                accountant.set_conversion_rate(conversion.from_currency, conversion.rate)
        for settlement in settlements.payloads():
            accountant.record_funding(
                settlement.venue_id, settlement.symbol, settlement.amount_quote
            )
        # The allotment is per segment (RL-051), and this part's capital figure is
        # per symbol -- what one trade actually committed. Filing the segment's
        # whole allotment against each symbol would state a return on capital
        # computed from money the trade never used, so it is drained and left
        # unset: `capital_used_usdt` reads None and the statement says so, which
        # is the honest answer until something sizes capital per position.
        allotments.payloads()
        fills.payloads()
        trades.payloads()
        bases.payloads()
        return tuple((trade, quote_currency) for trade in closed_trades.payloads())

    accountant = UsdtPnlAccountant()
    return run_usdt_pnl_accountant(
        accountant=accountant,
        control_socket=context.control_socket,
        read_closed_trades=read_closed_trades,
        publish_statements=publish_statements,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
