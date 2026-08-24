"""trade-capital-bounds-gate: no order reaches execution outside the user's bounds (RL-054).

The last thing between sizing and spending. Everything upstream reasons about
risk; this reasons about the operator's stated limits on capital per trade, and
it is the only part that guarantees execution never sees an order outside them.

Two directions, and they are not symmetric:

- **Below the minimum, bump up** (RL-054). A trade too small to be worth its fees
  is worse than no trade, and the operator has said what "worth it" means.
- **Above the maximum, cap down.** Never refuse: a trade at the maximum is the
  trade the operator asked for.

But a bump can breach the risk limit the sizer worked to respect, so a bump is
only made when the larger size still fits inside the risk allowed. Where it does
not, the order is refused and says which of the two bounds it could not satisfy
-- silently trading a size that breaches the risk limit would make every limiter
upstream decorative.

The settings verdict gates everything: an order is refused outright while the
capital settings are inconsistent, because bounds that contradict each other
cannot be enforced and guessing which one the operator meant is not this part's
decision to make.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-capital-bounds-gate"

PART_DECLARATION = PartDeclaration(
    part_id="trade-capital-bounds-gate",
    consumes=("sized-order", "trade-capital-bounds", "capital-settings-verdict"),
    produces=("bounded-order", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WITHIN_BOUNDS = "within-bounds"
BUMPED_TO_MINIMUM = "bumped-to-minimum"
CAPPED_AT_MAXIMUM = "capped-at-maximum"
REFUSED_SETTINGS_INVALID = "refused-capital-settings-invalid"
REFUSED_BUMP_BREACHES_RISK = "refused-bump-would-breach-risk"
REFUSED_NOT_TRADEABLE = "refused-order-not-tradeable"


@dataclass(frozen=True)
class BoundedOrder:
    """One order, inside the operator's capital bounds or refused with the reason."""

    venue_id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    capital_used: float
    outcome: str
    minimum_capital: float
    maximum_capital: float
    risk_at_stop: float
    risk_allowed: float
    reason: str
    bounded_at_ns: int
    # The decision this order serves. Carried so the stamper can give every
    # order for one decision the same id, which is what makes a republished
    # intent one order rather than one order per tick.
    intent_id: str = ""

    @property
    def may_be_sent(self) -> bool:
        return self.outcome in (WITHIN_BOUNDS, BUMPED_TO_MINIMUM, CAPPED_AT_MAXIMUM) and self.quantity > 0


@dataclass
class GateStanding:
    passed: int = 0
    bumped: int = 0
    capped: int = 0
    refused_settings: int = 0
    refused_bump: int = 0
    refused_not_tradeable: int = 0
    largest_capital_used: float = 0.0


class TradeCapitalBoundsGate:
    """Bumps, caps or refuses a sized order against the operator's per-trade bounds."""

    def __init__(self, quantity_increment: float, now_ns=time.time_ns) -> None:
        if quantity_increment <= 0:
            raise ValueError("a quantity increment of zero cannot snap anything")
        self._increment = quantity_increment
        self._now_ns = now_ns
        self.standing = GateStanding()

    def bound(self, sized_order, bounds, settings_are_valid: bool) -> BoundedOrder:
        if not settings_are_valid:
            self.standing.refused_settings += 1
            return self._refusal(
                sized_order, bounds, REFUSED_SETTINGS_INVALID,
                "the capital settings are inconsistent; which bound the operator meant is "
                "not this part's decision to guess",
            )

        if not sized_order.is_tradeable:
            self.standing.refused_not_tradeable += 1
            return self._refusal(
                sized_order, bounds, REFUSED_NOT_TRADEABLE,
                f"the sizer produced no tradeable order: {sized_order.reason}",
            )

        capital = sized_order.quantity * sized_order.entry_price

        if capital < bounds.minimum_capital:
            return self._bump(sized_order, bounds, capital)
        if capital > bounds.maximum_capital:
            return self._cap(sized_order, bounds, capital)

        self.standing.passed += 1
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, capital)
        return self._bounded(
            sized_order, bounds, sized_order.quantity, capital, WITHIN_BOUNDS,
            sized_order.risk_at_stop,
            f"{capital:,.2f} is inside [{bounds.minimum_capital:,.2f}, {bounds.maximum_capital:,.2f}]",
        )

    def _bump(self, sized_order, bounds, capital: float) -> BoundedOrder:
        """Raise to the minimum, unless that would risk more than allowed (RL-054)."""
        quantity = self._snap_up(bounds.minimum_capital / sized_order.entry_price)
        scaled_risk = self._risk_at(sized_order, quantity)

        if scaled_risk > sized_order.risk_allowed:
            self.standing.refused_bump += 1
            return self._refusal(
                sized_order, bounds, REFUSED_BUMP_BREACHES_RISK,
                f"bumping {capital:,.2f} to the {bounds.minimum_capital:,.2f} minimum would risk "
                f"{scaled_risk:,.2f} against {sized_order.risk_allowed:,.2f} allowed; the two "
                f"bounds cannot both be satisfied for this trade",
            )

        bumped_capital = quantity * sized_order.entry_price
        self.standing.bumped += 1
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, bumped_capital)
        return self._bounded(
            sized_order, bounds, quantity, bumped_capital, BUMPED_TO_MINIMUM, scaled_risk,
            f"raised from {capital:,.2f} to the {bounds.minimum_capital:,.2f} minimum",
        )

    def _cap(self, sized_order, bounds, capital: float) -> BoundedOrder:
        """Cut to the maximum. Never a refusal: the maximum is a tradeable size."""
        quantity = self._snap_down(bounds.maximum_capital / sized_order.entry_price)
        capped_capital = quantity * sized_order.entry_price
        self.standing.capped += 1
        self.standing.largest_capital_used = max(self.standing.largest_capital_used, capped_capital)
        return self._bounded(
            sized_order, bounds, quantity, capped_capital, CAPPED_AT_MAXIMUM,
            self._risk_at(sized_order, quantity),
            f"cut from {capital:,.2f} to the {bounds.maximum_capital:,.2f} maximum",
        )

    def _risk_at(self, sized_order, quantity: float) -> float:
        """The risk this order would carry at a different size, scaled from its own."""
        if sized_order.quantity <= 0:
            return 0.0
        return sized_order.risk_at_stop * (quantity / sized_order.quantity)

    def _snap_up(self, quantity: float) -> float:
        return round(math.ceil(quantity / self._increment) * self._increment, 12)

    def _snap_down(self, quantity: float) -> float:
        return round(math.floor(quantity / self._increment) * self._increment, 12)

    def _bounded(self, sized_order, bounds, quantity, capital, outcome, risk, reason) -> BoundedOrder:
        return BoundedOrder(
            venue_id=sized_order.venue_id,
            symbol=sized_order.symbol,
            side=sized_order.side,
            quantity=quantity,
            entry_price=sized_order.entry_price,
            stop_price=sized_order.stop_price,
            capital_used=capital,
            outcome=outcome,
            minimum_capital=bounds.minimum_capital,
            maximum_capital=bounds.maximum_capital,
            risk_at_stop=risk,
            risk_allowed=sized_order.risk_allowed,
            reason=reason,
            bounded_at_ns=self._now_ns(),
            # Straight through: bounding an order does not make it a different
            # decision, and the id has to survive every step between the intent
            # and the venue or it stops being an identity.
            intent_id=getattr(sized_order, "intent_id", ""),
        )

    def _refusal(self, sized_order, bounds, outcome, reason) -> BoundedOrder:
        return self._bounded(sized_order, bounds, 0.0, 0.0, outcome, 0.0, reason)


def describe_bounding(gate: TradeCapitalBoundsGate) -> dict:
    return {
        "part_id": PART_ID,
        "passed": gate.standing.passed,
        "bumped_to_minimum": gate.standing.bumped,
        "capped_at_maximum": gate.standing.capped,
        "refused_settings_invalid": gate.standing.refused_settings,
        "refused_bump_breaches_risk": gate.standing.refused_bump,
        "refused_not_tradeable": gate.standing.refused_not_tradeable,
        "largest_capital_used": gate.standing.largest_capital_used,
    }


def does_verdict_permit_trading(verdict) -> bool | None:
    """Whether a capital-settings verdict permits an order, or None if none arrived.

    A named function rather than an attribute read at the call site, because the
    call site got it wrong and nothing noticed: it asked for `is_valid`, which
    `CapitalSettingsVerdict` has never had, so every order was refused as
    "settings inconsistent" while the validator published `consistent` beside it
    -- and that refusal is indistinguishable from the deliberate one for settings
    that really do contradict each other.

    None means no verdict has been read, which the gate treats as unverified
    rather than as permission. A bound checked against settings nobody verified is
    a bound with no authority behind it.
    """
    if verdict is None:
        return None
    return verdict.permits_trading


def run_trade_capital_bounds_gate(
    gate: TradeCapitalBoundsGate, control_socket, read_sized_orders, publish_bounded_orders,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_bounded_orders(
            tuple(
                gate.bound(sized_order, bounds, valid)
                for sized_order, bounds, valid in read_sized_orders()
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_bounding(gate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The last gate before an order carries an identity. It bounds a sized order
    against what the operator said one trade may commit, and it needs the capital
    settings to have been validated: a bound checked against settings nobody
    verified is a bound with no authority behind it.

    A verdict that has not arrived is unverified, not valid: `settings_are_valid`
    stays None until `capital-settings-validator` publishes one, and the gate
    refuses meanwhile. That is the correct direction to fail -- a bound checked
    against settings nobody verified is a bound with no authority behind it.
    """
    from runtime.input_assembly import Batch, LatestValue

    sized = Batch(read=context.bus.reader("sized-order"))
    bounds = LatestValue(read=context.bus.reader("trade-capital-bounds"))
    verdicts = LatestValue(read=context.bus.reader("capital-settings-verdict"))
    publish_bounded_orders = context.bus.publisher_for("bounded-order")

    def read_sized_orders():
        current_bounds = bounds.value()
        return tuple(
            (order, current_bounds, does_verdict_permit_trading(verdicts.value()))
            for order in sized.payloads()
        )

    return run_trade_capital_bounds_gate(
        gate=TradeCapitalBoundsGate(quantity_increment=context.number("order_quantity_increment")),
        control_socket=context.control_socket,
        read_sized_orders=read_sized_orders,
        publish_bounded_orders=publish_bounded_orders,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
