"""position-sizer sizes CLOSE/REDUCE against the position, not a risk budget.

Real incident, 2026-09-08: every actionable trade-intent the live spine formed
all session was a CLOSE (4,483 of 4,483 sampled, zero OPEN). Sized through the
entry/stop path OPEN uses, every one was refused as missing_stop_price -- a
close carries neither by construction, so nothing already held could ever be
exited. See docs/proposals/position-sizer-closes-what-is-held.md.
"""

from __future__ import annotations

from parts.risk_capital_allocation.position_sizer import (
    CLOSE,
    CLOSED,
    REFUSED_NO_POSITION_TO_CLOSE,
    PositionSizer,
)
from runtime.trading_types import BUY, LONG, SELL, SHORT


def sizer():
    return PositionSizer(taker_fee_rate=0.0005, slippage_fraction=0.0, now_ns=lambda: 1)


def test_closing_a_long_position_sells_the_full_quantity_held():
    order = sizer().close_order(
        venue_id="upstox", symbol="ICICIBANK 1440 PE 29 SEP 26",
        position_quantity=700.0, position_side=LONG,
        entry_price=22.525, quantity_increment=700.0,
    )
    assert order.outcome == CLOSED
    assert order.side == SELL
    assert order.quantity == 700.0
    assert order.is_tradeable


def test_closing_a_short_position_buys_the_full_quantity_held():
    order = sizer().close_order(
        venue_id="upstox", symbol="NIFTY 23000 PE 08 SEP 26",
        position_quantity=-65.0, position_side=SHORT,
        entry_price=48.75, quantity_increment=65.0,
    )
    assert order.side == BUY
    assert order.quantity == 65.0


def test_a_close_order_carries_no_stop_and_no_risk_budget():
    """The whole point: nothing here is sized against a stop distance."""
    order = sizer().close_order(
        venue_id="upstox", symbol="ICICIBANK 1440 PE 29 SEP 26",
        position_quantity=700.0, position_side=LONG,
        entry_price=22.525, quantity_increment=700.0,
    )
    assert order.stop_price == 0.0
    assert order.risk_allowed == 0.0
    assert order.risk_at_stop == 0.0


def test_a_close_order_is_stamped_action_close_not_open():
    order = sizer().close_order(
        venue_id="upstox", symbol="ICICIBANK 1440 PE 29 SEP 26",
        position_quantity=700.0, position_side=LONG,
        entry_price=22.525, quantity_increment=700.0, action=CLOSE,
    )
    assert order.action == CLOSE


def test_closing_a_position_this_part_never_saw_is_refused_not_zeroed():
    """Nothing held is not a close of size zero -- it is a decision that names
    a position this part has no record of."""
    order = sizer().close_order(
        venue_id="upstox", symbol="RELIANCE 1310 CE 29 SEP 26",
        position_quantity=0.0, position_side=LONG,
        entry_price=24.9, quantity_increment=550.0,
    )
    assert order.outcome == REFUSED_NO_POSITION_TO_CLOSE
    assert order.quantity == 0.0
    assert not order.is_tradeable


def test_a_close_orders_notional_is_computed_from_the_real_price():
    order = sizer().close_order(
        venue_id="upstox", symbol="ICICIBANK 1440 PE 29 SEP 26",
        position_quantity=700.0, position_side=LONG,
        entry_price=22.525, quantity_increment=700.0,
    )
    assert order.notional == 700.0 * 22.525
