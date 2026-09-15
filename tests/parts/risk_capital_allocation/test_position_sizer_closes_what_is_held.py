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
    REFUSED_CLOSE_ALREADY_ASKED_FOR,
    REFUSED_NO_POSITION_TO_CLOSE,
    PositionSizer,
)
import pytest

from runtime.trading_types import BUY, LONG, SELL, SHORT


CLOSE_RESTATED_AFTER_SECONDS = 30.0


class Clock:
    """A clock the test moves, so the restatement wait is exercised rather than slept."""

    def __init__(self, at_ns=1_000_000_000_000):
        self.at_ns = at_ns

    def __call__(self):
        return self.at_ns

    def advance_seconds(self, seconds):
        self.at_ns += int(seconds * 1e9)


def sizer(clock=None):
    return PositionSizer(
        taker_fee_rate=0.0005, slippage_fraction=0.0,
        close_restated_after_seconds=CLOSE_RESTATED_AFTER_SECONDS,
        now_ns=clock or (lambda: 1),
    )


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


def test_the_same_close_is_not_asked_for_twice_before_it_could_be_answered():
    """12,168 closes against 16 open symbols, in one live session.

    A close order asks to sell the WHOLE held quantity, and `opinion-arbiter`
    forms a fresh trade-intent -- with a fresh intent_id, so nothing downstream
    deduplicates it -- for every symbol it still wants closed, on every tick.
    Measured on the live spine 2026-09-08: `position-close-detector` refused
    10,005 units as an unmatched exit and `paper-account-index-options` held
    **-1,042.29** of `NIFTY 23650 CE 15 SEP 26` -- a short option written in a
    segment whose own settings say buy-only, because the position was sold
    several times over.
    """
    clock = Clock()
    subject = sizer(clock)

    first = subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )
    assert first.outcome == CLOSED and first.quantity == 1_042.0

    again = subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )
    assert again.outcome == REFUSED_CLOSE_ALREADY_ASKED_FOR
    assert again.quantity == 0.0
    assert subject.standing.closes_not_restated == 1
    assert subject.standing.closed == 1


def test_a_position_that_partly_filled_is_closed_again_at_its_new_size():
    """The held quantity changing is what makes the ask new, not the clock.

    A partial fill leaves less to close, and refusing to re-ask would strand the
    remainder -- which is the opposite failure and just as bad.
    """
    clock = Clock()
    subject = sizer(clock)
    subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )

    after_a_partial_fill = subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=400.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )

    assert after_a_partial_fill.outcome == CLOSED
    assert after_a_partial_fill.quantity == 400.0
    assert subject.standing.closes_not_restated == 0


def test_a_close_nothing_ever_answered_is_asked_again_once_the_wait_passes():
    """The bound covers the order that was cancelled or simply never filled."""
    clock = Clock()
    subject = sizer(clock)
    subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )
    clock.advance_seconds(CLOSE_RESTATED_AFTER_SECONDS + 1.0)

    restated = subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )

    assert restated.outcome == CLOSED
    assert restated.quantity == 1_042.0


def test_two_symbols_do_not_block_each_other():
    clock = Clock()
    subject = sizer(clock)
    one = subject.close_order(
        venue_id="upstox", symbol="NIFTY 23650 CE 15 SEP 26",
        position_quantity=1_042.0, position_side=LONG,
        entry_price=155.3, quantity_increment=1.0,
    )
    other = subject.close_order(
        venue_id="upstox", symbol="AXISBANK 1260 CE 29 SEP 26",
        position_quantity=625.0, position_side=LONG,
        entry_price=24.6, quantity_increment=625.0,
    )
    assert one.outcome == CLOSED and other.outcome == CLOSED


def test_a_sizer_with_no_wait_before_restating_a_close_is_refused_at_construction():
    with pytest.raises(ValueError):
        PositionSizer(
            taker_fee_rate=0.0005, slippage_fraction=0.0,
            close_restated_after_seconds=0.0,
        )


def test_the_outstanding_close_record_does_not_grow_without_bound():
    """One entry per symbol ever closed would be an unbounded dict with a good
    excuse -- the shape T-3 and this project's LatestByKey lesson both name.
    An entry past the wait can never refuse anything again."""
    clock = Clock()
    subject = sizer(clock)
    for index in range(50):
        subject.close_order(
            venue_id="upstox", symbol=f"CONTRACT {index}",
            position_quantity=100.0, position_side=LONG,
            entry_price=10.0, quantity_increment=1.0,
        )
    assert len(subject._closes_asked_for) == 50

    clock.advance_seconds(CLOSE_RESTATED_AFTER_SECONDS + 1.0)
    subject.close_order(
        venue_id="upstox", symbol="ONE MORE",
        position_quantity=100.0, position_side=LONG,
        entry_price=10.0, quantity_increment=1.0,
    )

    assert len(subject._closes_asked_for) == 1


def test_closing_a_position_that_is_already_flat_is_refused_not_a_crash():
    """A flat position has no side to trade against, and asking which was the crash.

    2026-09-15: a close intent arrived for a symbol whose position had just gone flat;
    `order_side_for("flat")` raised inside the refusal, and position-sizer restarted
    rather than refusing.
    """
    from runtime.trading_types import FLAT

    order = sizer().close_order(
        venue_id="upstox", symbol="NIFTY 23400 CE 15 SEP 26",
        position_quantity=0.0, position_side=FLAT,
        entry_price=28.7, quantity_increment=65.0,
    )
    assert order.outcome == REFUSED_NO_POSITION_TO_CLOSE
    assert not order.is_tradeable
