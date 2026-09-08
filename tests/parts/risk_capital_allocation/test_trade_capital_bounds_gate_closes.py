"""trade-capital-bounds-gate skips its capital-ceiling economics for a close.

Real incident, 2026-09-08: `bound()` checks a sized order's own notional
against the position's *already-committed* capital, refusing when the
position already sits at or past the ceiling (`REFUSED_POSITION_AT_THE_CEILING`)
-- correct for an order adding to a position, backwards for one closing it. A
position sized exactly at the ceiling would have refused the one order that
brings it under. See docs/proposals/position-sizer-closes-what-is-held.md.
"""

from __future__ import annotations

from parts.risk_capital_allocation.position_sizer import CLOSE, CLOSED, OPEN, SIZED, SizedOrder
from parts.risk_capital_allocation.trade_capital_bounds_gate import (
    REFUSED_POSITION_AT_THE_CEILING,
    WITHIN_BOUNDS,
    TradeCapitalBoundsGate,
)
from runtime.risk_types import TradeCapitalBounds
from runtime.trading_types import SELL


def gate():
    return TradeCapitalBoundsGate(quantity_increment=1.0, now_ns=lambda: 1)


def a_close_order(quantity=700.0, entry_price=22.525, action=CLOSE):
    return SizedOrder(
        venue_id="upstox", symbol="ICICIBANK 1440 PE 29 SEP 26", side=SELL,
        quantity=quantity, entry_price=entry_price, stop_price=0.0,
        outcome=CLOSED if action == CLOSE else SIZED,
        risk_allowed=0.0, risk_at_stop=0.0, fees_charged=0.0,
        notional=quantity * entry_price, leverage=1.0,
        reason="closing what is held", sized_at_ns=1, intent_id="d1",
        segment="stock-options", quantity_increment=700.0, action=action,
    )


def tight_bounds():
    """A ceiling already fully committed -- would refuse any OPEN this size."""
    return TradeCapitalBounds(
        segment="stock-options", minimum_capital=1.0, maximum_capital=100.0,
        currency="INR",
    )


def test_a_close_passes_the_gate_even_at_a_position_already_at_the_ceiling():
    g = gate()
    g.observe_position("upstox", "ICICIBANK 1440 PE 29 SEP 26", capital=15_767.5)
    bounded = g.bound(a_close_order(), tight_bounds(), settings_are_valid=True)
    assert bounded.outcome == WITHIN_BOUNDS
    assert bounded.quantity == 700.0
    assert bounded.may_be_sent


def test_the_same_notional_as_an_open_would_be_refused_at_the_ceiling():
    """Proves the bypass is actually doing something: the identical order,
    stamped OPEN instead of CLOSE, hits the ceiling this test's own fixture
    was built to trigger."""
    g = gate()
    g.observe_position("upstox", "ICICIBANK 1440 PE 29 SEP 26", capital=15_767.5)
    bounded = g.bound(a_close_order(action=OPEN), tight_bounds(), settings_are_valid=True)
    assert bounded.outcome == REFUSED_POSITION_AT_THE_CEILING


def test_a_close_is_never_bumped_or_capped_to_a_different_quantity():
    """The close quantity is what is held, not what the minimum/maximum say."""
    g = gate()
    g.observe_position("upstox", "ICICIBANK 1440 PE 29 SEP 26", capital=0.0)
    tiny = TradeCapitalBounds(
        segment="stock-options", minimum_capital=1_000_000.0,
        maximum_capital=2_000_000.0, currency="INR",
    )
    bounded = g.bound(a_close_order(), tiny, settings_are_valid=True)
    assert bounded.quantity == 700.0
    assert bounded.outcome == WITHIN_BOUNDS


def test_a_close_orders_action_travels_onto_the_bounded_order():
    g = gate()
    bounded = g.bound(a_close_order(), tight_bounds(), settings_are_valid=True)
    assert bounded.action == CLOSE
