"""The sizer reads fields its producers actually carry.

The third defect of this exact shape, and the second one to take a part off the
air. `position-sizer` was wired to `locked-allocation` on 2026-08-22, before
anything produced one, and it keyed the assembly on `lock.segment` -- a field
`LockedAllocation` has never had. Nothing failed while `fund-lock-ledger` was
unbuilt, because an assembly with no messages never calls its key function.

`fund-lock-ledger` started with phase 8 on 2026-08-25. Within seconds the sizer
was restarting on `AttributeError: 'LockedAllocation' object has no attribute
'segment'`, and it restarted 1,587 times in the two hours before it was found:
**no order was sized in that time**, so the bot's whole trading path was off the
air while every board reported 127 parts running.

The same tests as `test_stop_target_placer_reads_real_fields`, for the same
reason: build the real producer type, so a field rename on either side fails here
rather than in a live process.
"""

from __future__ import annotations

import pytest

from parts.portfolio_state.fund_lock_ledger import LOCKED, RELEASED, LockedAllocation
from parts.risk_capital_allocation.position_sizer import (
    free_capital_from_locks, opening_order_target,
)
from runtime.trade_intent import OPEN
from runtime.trading_types import BUY, LONG


def allocation(order_id: str, free_after: float, at_ns: int, state: str = LOCKED):
    return LockedAllocation(
        order_id=order_id, venue_id="binance-usdm", symbol="BTCUSDT",
        amount=100.0, state=state, free_balance_after=free_after,
        reason="held against this order", decided_at_ns=at_ns,
    )


def test_a_locked_allocation_is_identified_by_its_order_not_by_a_segment():
    """The field the sizer crashed on, pinned on the producer's own type."""
    assert not hasattr(LockedAllocation, "segment")
    assert "order_id" in LockedAllocation.__dataclass_fields__
    assert "free_balance_after" in LockedAllocation.__dataclass_fields__


def test_free_capital_is_the_most_recent_decision_not_the_largest():
    """Free balance is a level: the newest statement of it is the true one."""
    locks = {
        "a": allocation("a", 900.0, at_ns=1),
        "b": allocation("b", 800.0, at_ns=3),
        "c": allocation("c", 850.0, at_ns=2),
    }
    assert free_capital_from_locks(locks.values()) == 800.0


def test_a_release_raises_the_free_balance_again():
    """A released lock is the newest decision, and its free balance is the one to use."""
    locks = {
        "a": allocation("a", 800.0, at_ns=1),
        "b": allocation("b", 1_000.0, at_ns=2, state=RELEASED),
    }
    assert free_capital_from_locks(locks.values()) == 1_000.0


def test_no_lock_ever_seen_is_not_a_free_balance_of_zero():
    """Nothing locked and nothing said are different facts; zero would refuse every order."""
    assert free_capital_from_locks(()) is None


def sizer():
    from parts.risk_capital_allocation.position_sizer import PositionSizer

    return PositionSizer(
        taker_fee_rate=0.0005, slippage_fraction=0.0,
        close_restated_after_seconds=30.0, now_ns=lambda: 1,
    )


def size_with(free_capital, leverage=1.0):
    return sizer().size(
        venue_id="binance-usdm", symbol="BTCUSDT", side="buy",
        entry_price=100.0, stop_price=99.0, allotment=10_000.0,
        risk_limit_fraction=0.01, leverage=leverage, price_increment=0.01,
        quantity_increment=0.001, minimum_quantity=0.001, free_capital=free_capital,
    )


def test_free_capital_the_ledger_never_reported_does_not_cap_the_size():
    """Risk is the only limit until the ledger has something to say."""
    unbounded = size_with(free_capital=None)
    assert unbounded.outcome == "sized"
    assert unbounded.quantity > 0


def test_an_order_is_shrunk_to_what_the_free_balance_pays_for():
    """The account cannot spend money already locked against an order in flight."""
    from parts.risk_capital_allocation.position_sizer import SHRUNK_TO_FIT

    unbounded = size_with(free_capital=None)
    capped = size_with(free_capital=10.0)
    assert capped.quantity < unbounded.quantity
    assert capped.outcome == SHRUNK_TO_FIT
    assert capped.quantity * capped.entry_price <= 10.0 + 1e-9


def test_leverage_is_what_the_free_balance_buys_more_of():
    """A 10x position of the same notional costs a tenth of the balance."""
    assert size_with(free_capital=10.0, leverage=10.0).quantity > size_with(free_capital=10.0).quantity


def test_a_free_balance_too_small_to_trade_is_its_own_refusal():
    """Not 'too small to risk': the stop is fine and the capital is committed."""
    from parts.risk_capital_allocation.position_sizer import REFUSED_NO_FREE_CAPITAL

    refused = size_with(free_capital=0.05)
    assert refused.outcome == REFUSED_NO_FREE_CAPITAL
    assert refused.quantity == 0.0
    assert "free at" in refused.reason


def test_a_size_hint_is_a_multiple_and_not_a_quantity():
    """The field the sizer defaulted away from, pinned on the producer's own type."""
    from runtime.trade_intent import SizeHint

    assert not hasattr(SizeHint, "quantity")
    assert "multiple_of_normal" in SizeHint.__dataclass_fields__


def test_a_conviction_below_normal_sizes_the_trade_down():
    full = size_with(free_capital=None)
    half = sizer().size(
        venue_id="binance-usdm", symbol="BTCUSDT", side="buy", entry_price=100.0,
        stop_price=99.0, allotment=10_000.0, risk_limit_fraction=0.01, leverage=1.0,
        price_increment=0.01, quantity_increment=0.001, minimum_quantity=0.001,
        size_multiple=0.5,
    )
    assert half.quantity == pytest.approx(full.quantity * 0.5, rel=1e-3)


def test_a_hint_above_the_risk_ceiling_is_clipped_and_counted():
    """Conviction may size down inside the limit; it may not size past it."""
    one = sizer()
    full = one.size(
        venue_id="binance-usdm", symbol="BTCUSDT", side="buy", entry_price=100.0,
        stop_price=99.0, allotment=10_000.0, risk_limit_fraction=0.01, leverage=1.0,
        price_increment=0.01, quantity_increment=0.001, minimum_quantity=0.001,
    )
    hinted = one.size(
        venue_id="binance-usdm", symbol="BTCUSDT", side="buy", entry_price=100.0,
        stop_price=99.0, allotment=10_000.0, risk_limit_fraction=0.01, leverage=1.0,
        price_increment=0.01, quantity_increment=0.001, minimum_quantity=0.001,
        size_multiple=2.0,
    )
    assert hinted.quantity == full.quantity
    assert one.standing.hints_above_the_risk_ceiling == 1


def test_a_refused_choice_and_no_choice_at_all_are_different_facts():
    """`opens_without_an_instrument_choice` hid the diagnosis it was meant to give.

    `instrument-selector` publishes every verdict, refusals included, so a
    choice carrying `chosen=None` is the selector saying no with a named reason
    -- while no choice at all is the selector never having spoken. Measured on
    the live spine 2026-09-08: 5,268 opens counted as "no instrument choice"
    against 13,729 intents the selector had refused by name, the largest of them
    `this-symbol-has-no-recent-trade-and-no-recent-quote` at 10,612. One counter
    pointed at the wrong part.
    """
    assert opening_order_target(_AnOpen(), None) is None
    assert opening_order_target(_AnOpen(), _ARefusedChoice()) is None
    # And a choice that names a contract still resolves, unchanged.
    contract, side = opening_order_target(_AnOpen(), _AChosenChoice())
    assert contract == "NIFTY 23650 CE 15 SEP 26"
    assert side == BUY


class _AnOpen:
    """A trade-intent asking to open, as this part reads one (T-4)."""

    action = OPEN
    symbol = "NIFTY"
    side = LONG


class _ARefusedChoice:
    """What instrument-selector publishes when it will not choose."""

    chosen = None
    order_side = None
    state = "this-symbol-has-no-recent-trade-and-no-recent-quote"


class _AChosenChoice:
    class chosen:  # noqa: N801 - a stub matching the payload's shape
        contract_symbol = "NIFTY 23650 CE 15 SEP 26"

    order_side = BUY
    state = "chosen"
