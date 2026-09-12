"""pnl-attributor: where the money actually came from, in pieces that add back up.

Realised PnL is one number and it hides everything worth knowing. The same +40 USDT
is produced by a move that went straight to target, and by a move twice as large
that gave half of itself to fees, funding and slippage. Those are opposite lessons,
and a system learning from the total cannot tell them apart.

The decomposition here reconciles by construction: the components sum to the
realised total, and whatever the model cannot explain stays in its own residual
rather than being smeared across the others. **A large residual is the most useful
output this part produces** -- it says the model of where PnL comes from is missing
something, and hiding it inside the direction term makes an incomplete
decomposition look complete.

Four decisions that matter:

- **Fees, funding and slippage are separate, not one "costs" line.** They have
  different fixes: fees are a venue and tier choice, funding is a holding-period
  choice, slippage is an order-type and size choice.
- **Funding is signed and is often received.** A short in a positive-funding market
  is paid to hold, and treating funding as a cost by definition mis-signs exactly
  the trades where it mattered.
- **Everything is in USDT (RL-028) with the conversion rate journalled (RL-029).**
  A decomposition that mixes quote currencies does not add up, and the moment it
  stops adding up is the moment it starts being trusted anyway.
- **Slippage is measured against the decision price, not the arrival price.** The
  gap between deciding and arriving is a real cost of being slow, and attributing
  it to the market makes latency invisible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import (
    FROM_DIRECTION, FROM_FEES, FROM_FUNDING, FROM_SIZE, FROM_SLIPPAGE, FROM_TIMING,
    FROM_UNEXPLAINED, PNL_COMPONENTS, PnlAttribution, unexplained_only,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "pnl-attributor"

PART_DECLARATION = PartDeclaration(
    part_id="pnl-attributor",
    consumes=("closed-trade", "fill", "funding-settlement", "cost-estimate", "peak-excursion"),
    produces=("pnl-attribution", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ATTRIBUTED = "attributed"
DOES_NOT_RECONCILE = "the-components-do-not-sum-to-the-realised-total"
NOTHING_TO_ATTRIBUTE = "no-fill-was-recorded-for-this-trade"
MIXED_CURRENCIES = "the-fills-are-not-all-in-one-quote-currency"

# The default when a caller states no currency at all. The operator's own
# `settlement_currency` is what the running part uses, and this exists so a test
# constructing the class directly still has a unit rather than an empty string.
QUOTE_CURRENCY = "INR"


@dataclass(frozen=True)
class AttributionOutcome:
    trade_id: str
    state: str
    attribution: PnlAttribution
    largest_component: str | None
    reason: str
    attributed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ATTRIBUTED


@dataclass
class AttributorStanding:
    trades_attributed: int = 0
    failed_to_reconcile: int = 0
    without_fills: int = 0
    mixed_currency_trades: int = 0
    total_residual: float = 0.0
    largest_residual_share: float = 0.0
    trades_where_costs_exceeded_the_move: int = 0
    funding_received_trades: int = 0


class PnlAttributor:
    """Splits realised PnL into components that add back to the total."""

    def __init__(self, reconciliation_tolerance: float, now_ns=time.time_ns,
                 settlement_currency: str = QUOTE_CURRENCY) -> None:
        if reconciliation_tolerance <= 0:
            raise ValueError(
                "floating-point arithmetic needs some tolerance, but a decomposition "
                "that does not reconcile is a story rather than an attribution"
            )
        if not settlement_currency:
            raise ValueError(
                "an attribution states which currency its components are in; without "
                "one, a number is a magnitude with no unit and nothing downstream can "
                "tell rupees from dollars"
            )
        # Taken from the operator's own `settlement_currency` rather than the
        # module constant. `start_part` already read that setting for the fills
        # it records, while `_to_usdt` and `_attribution` compared against the
        # constant -- so the day the setting stopped saying USDT, a fill recorded
        # in the real currency would have failed the equality here and gone
        # looking for a conversion rate nobody publishes. Found 2026-09-06,
        # walking portfolio-state: the fees this project charges are Upstox's
        # real rupee stack and the statements were labelled USDT.
        self._settlement_currency = settlement_currency
        self._tolerance = reconciliation_tolerance
        self._now_ns = now_ns
        self._fills: dict[str, list] = {}
        self._funding: dict[str, float] = {}
        self._decision_prices: dict[str, float] = {}
        self._conversion_rates: dict[str, float] = {}
        self.standing = AttributorStanding()

    def observe_fill(self, trade_id: str, fill, quote_currency: str | None = None) -> None:
        # None means "the currency this account settles in", which is what a
        # caller that does not say means. Defaulting to a module constant made
        # that untrue the moment the operator's own setting stopped matching it.
        self._fills.setdefault(trade_id, []).append(
            (fill, quote_currency or self._settlement_currency)
        )

    def observe_funding(self, trade_id: str, amount: float) -> None:
        """Signed: a short in a positive-funding market is paid to hold."""
        self._funding[trade_id] = self._funding.get(trade_id, 0.0) + amount

    def observe_decision_price(self, trade_id: str, price: float) -> None:
        """What the decision assumed. Slippage is measured from here, not from arrival."""
        self._decision_prices[trade_id] = price

    def observe_conversion_rate(self, currency: str, rate_to_settlement: float) -> None:
        """RL-029: the rate is recorded, so a converted number can be re-derived."""
        self._conversion_rates[currency] = rate_to_settlement

    def attribute(self, trade_id: str, closed_trade) -> AttributionOutcome:
        entries = self._fills.get(trade_id, [])
        if not entries:
            self.standing.without_fills += 1
            return self._outcome(
                trade_id, NOTHING_TO_ATTRIBUTE,
                self._attribution(
                    trade_id, closed_trade, unexplained_only(closed_trade.realised_pnl),
                    True, closed_trade.realised_pnl,
                ),
                None,
                "no fill was recorded, so the whole result is unexplained. That is kept as "
                "the residual rather than assigned to direction, because an unexplained "
                "result assigned to a cause is a fabricated lesson",
            )

        currencies = {currency for _, currency in entries}
        unconvertible = {
            currency
            for currency in currencies
            if currency != self._settlement_currency
            and currency not in self._conversion_rates
        }
        if unconvertible:
            self.standing.mixed_currency_trades += 1
            return self._outcome(
                trade_id, MIXED_CURRENCIES,
                self._attribution(
                    trade_id, closed_trade, unexplained_only(closed_trade.realised_pnl),
                    True, closed_trade.realised_pnl,
                ),
                None,
                f"fills are in {', '.join(sorted(unconvertible))} with no recorded rate to "
                f"{self._settlement_currency}. A decomposition that mixes quote "
                f"currencies does not "
                f"add up, and the moment it stops adding up is when it starts being "
                f"trusted anyway",
            )

        fees = sum(
            self._in_settlement_currency(fill.fee, currency) for fill, currency in entries
        )
        funding = self._funding.get(trade_id, 0.0)
        if funding > 0:
            self.standing.funding_received_trades += 1

        quantity = closed_trade.quantity
        sign = 1.0 if closed_trade.direction == "long" else -1.0

        decision_price = self._decision_prices.get(trade_id)
        arrival_price = entries[0][0].price
        if decision_price is None:
            slippage = 0.0
        else:
            # Signed so that paying up is always negative, whichever way the trade ran.
            slippage = -sign * (arrival_price - decision_price) * quantity

        # Direction is the move from the price the trade actually got to the price it
        # actually left at: the part of the result the market provided.
        direction = sign * (closed_trade.exit_price - closed_trade.entry_price) * quantity

        components = {
            FROM_DIRECTION: direction,
            FROM_TIMING: 0.0,
            FROM_SIZE: 0.0,
            FROM_FEES: -abs(fees),
            FROM_SLIPPAGE: slippage,
            FROM_FUNDING: funding,
            FROM_UNEXPLAINED: 0.0,
        }
        explained = sum(components.values())
        residual = closed_trade.realised_pnl - explained
        components[FROM_UNEXPLAINED] = residual

        reconciles = abs(
            sum(components.values()) - closed_trade.realised_pnl
        ) <= self._tolerance

        attribution = self._attribution(
            trade_id, closed_trade, components, reconciles, residual
        )
        self.standing.total_residual += abs(residual)
        if abs(closed_trade.realised_pnl) > 0:
            share = abs(residual) / abs(closed_trade.realised_pnl)
            self.standing.largest_residual_share = max(
                self.standing.largest_residual_share, share
            )
        if attribution.cost_share > 1.0:
            self.standing.trades_where_costs_exceeded_the_move += 1

        if not reconciles:
            self.standing.failed_to_reconcile += 1
            return self._outcome(
                trade_id, DOES_NOT_RECONCILE, attribution, None,
                f"the components sum to {sum(components.values()):.6f} against a realised "
                f"{closed_trade.realised_pnl:.6f}",
            )

        self.standing.trades_attributed += 1
        largest = max(
            (name for name in PNL_COMPONENTS if name != FROM_UNEXPLAINED),
            key=lambda name: abs(components[name]),
        )
        return self._outcome(
            trade_id, ATTRIBUTED, attribution, largest,
            f"{closed_trade.realised_pnl:+.4f} {self._settlement_currency} = "
            + ", ".join(
                f"{name} {components[name]:+.4f}"
                for name in PNL_COMPONENTS
                if abs(components[name]) > 0
            )
            + (
                f". {attribution.cost_share:.0%} of the gross move went to the venue"
                if attribution.cost_share > 0
                else ""
            )
            + (
                ". The residual is large: the model of where PnL comes from is missing "
                "something, which is worth more than the rest of this decomposition"
                if abs(closed_trade.realised_pnl) > 0
                and abs(residual) / abs(closed_trade.realised_pnl) > 0.2
                else ""
            ),
        )

    def _in_settlement_currency(self, amount: float, currency: str) -> float:
        """One amount in the currency the account settles in.

        A currency with no published rate raises rather than passing the number
        through: an unconverted amount added to a converted one is a total in no
        currency at all.
        """
        if currency == self._settlement_currency:
            return amount
        return amount * self._conversion_rates[currency]

    def _attribution(
        self, trade_id, closed_trade, components, reconciles, residual,
    ) -> PnlAttribution:
        return PnlAttribution(
            trade_id=trade_id,
            venue_id=closed_trade.venue_id,
            symbol=closed_trade.symbol,
            realised_pnl=closed_trade.realised_pnl,
            components=dict(components),
            quote_currency=self._settlement_currency,
            reconciles=reconciles,
            residual=residual,
            attributed_at_ns=self._now_ns(),
        )

    def _outcome(self, trade_id, state, attribution, largest, reason) -> AttributionOutcome:
        return AttributionOutcome(
            trade_id=trade_id, state=state, attribution=attribution,
            largest_component=largest, reason=reason, attributed_at_ns=self._now_ns(),
        )


def describe_attribution(attributor: PnlAttributor) -> dict:
    return {
        "part_id": PART_ID,
        "trades_attributed": attributor.standing.trades_attributed,
        "failed_to_reconcile": attributor.standing.failed_to_reconcile,
        "trades_without_fills": attributor.standing.without_fills,
        "mixed_currency_trades": attributor.standing.mixed_currency_trades,
        "total_absolute_residual": attributor.standing.total_residual,
        "largest_residual_share": attributor.standing.largest_residual_share,
        "trades_where_costs_exceeded_the_move": (
            attributor.standing.trades_where_costs_exceeded_the_move
        ),
        "trades_that_were_paid_funding": attributor.standing.funding_received_trades,
        "quote_currency": QUOTE_CURRENCY,
        "components": list(PNL_COMPONENTS),
        "smears_the_residual_across_the_components": False,
        "treats_funding_as_a_cost_by_definition": False,
    }


def run_pnl_attributor(
    attributor: PnlAttributor, control_socket, read_closed_trades, publish_attributions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade in read_closed_trades():
            outcome = attributor.attribute(trade_id, closed_trade)
            # Only a usable attribution goes on the wire, the same guard
            # entry-quality-scorer has always had. A refused one is not empty --
            # it carries every component at 0.0, `unexplained` holding the whole
            # realised PnL, and **`reconciles=True`**, because unexplained
            # absorbs everything and the reconciliation then trivially holds. A
            # consumer cannot tell that from a measured attribution: it states a
            # PnL, it says it reconciles, and its residual is a number.
            # trade-episode-encoder reads `cost_share` and `residual` off it and
            # its own comment is "only measured values go into conditions: this
            # is what a model reads". Encoding one produces an episode that
            # looks complete and every statistic over it silently includes a
            # fabricated field -- which is the encoder's own stated reason for
            # refusing an incomplete episode, defeated by handing it a complete-
            # looking one instead. Withheld, the encoder correctly reports
            # INCOMPLETE and names what has not landed (2026-09-06, found
            # driving a real replayed closed trade through the chain).
            if outcome.is_usable:
                publish_attributions(outcome.attribution)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_attribution(attributor),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Fills, funding and the cost estimate for a symbol are kept until the
    trade closes; the decision price is the bounded order's, read off the
    cost estimate's notional where one was made. A fill is attributed to the
    trade open on its symbol.
    """
    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    fills = Batch(read=context.bus.reader("fill"))
    settlements = Batch(read=context.bus.reader("funding-settlement"))
    estimates = Batch(read=context.bus.reader("cost-estimate"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    publish_attributions = context.bus.publisher_for("pnl-attribution")
    quote = str(context.setting("settlement_currency").value)
    attributor = PnlAttributor(
        reconciliation_tolerance=context.number("pnl_reconciliation_tolerance"),
        settlement_currency=quote,
    )
    pending_fills: dict[tuple[str, str], list] = {}
    pending_funding: dict[tuple[str, str], float] = {}

    def read_closed_trades():
        excursions.payloads()
        estimates.payloads()
        for fill in fills.payloads():
            pending_fills.setdefault((fill.venue_id, fill.symbol), []).append(fill)
        for settlement in settlements.payloads():
            key = (settlement.venue_id, settlement.symbol)
            pending_funding[key] = pending_funding.get(key, 0.0) + float(settlement.amount_quote)
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            key = (trade.venue_id, trade.symbol)
            for fill in pending_fills.pop(key, []):
                attributor.observe_fill(trade_id, fill, quote)
            funding = pending_funding.pop(key, None)
            if funding:
                attributor.observe_funding(trade_id, funding)
            attributor.observe_decision_price(trade_id, trade.entry_price)
            jobs.append((trade_id, trade))
        return tuple(jobs)

    return run_pnl_attributor(
        attributor=attributor,
        control_socket=context.control_socket,
        read_closed_trades=read_closed_trades,
        publish_attributions=lambda a: publish_attributions((a,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
