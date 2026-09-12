"""trade-capital-bounds-gate against the seven orders that lost the money (RL-063).

Every number here was measured, none invented. The orders are the ones this gate
really emitted on 2026-09-08, read off `bounded-order` records in
`journal.trade-lifecycle-recorder.18649654.sqlite`; the contract facts are
Upstox's own instrument master, `~/.local/share/ajit-segment-bots/instrument-
master/complete.json.gz`, which states NIFTY at `lot_size` 65 and
`freeze_quantity` 1,755.

## What actually happened

    bounded-order  capital_used 200000.0  quantity 2000000.0  entry_price 0.1
                   outcome "capped-at-maximum"
                   reason  "cut from 2,321,631.74 to the 200,000.00 that may
                            still be committed"
    fill           price 0.169539375      quantity 2000000.0

The capital ceiling was enforced, in capital, at the touch price of 0.10 -- and
then converted into a quantity 1,140 times the largest single order NSE accepts
for that contract. The paper book, which reads no freeze quantity either, filled
it by walking 69.5% up its own depth curve, and the trade committed 339,079
against a 200,000 ceiling.

**A cap expressed in capital but enforced as a quantity is not a cap.** The
missing bound was never the desk's; it was the venue's, and nothing in this
project read it: `grep -rn freeze_quantity --include=*.py` hit the adapter that
parses it and the tests, and nothing else.

Seven fills of this shape are 91.4% of every rupee this project has lost.
"""

from __future__ import annotations

import pytest

from parts.risk_capital_allocation.position_sizer import SIZED, SizedOrder
from parts.risk_capital_allocation.trade_capital_bounds_gate import (
    CAPPED_AT_MAXIMUM,
    REFUSED_ABOVE_THE_EXCHANGE_FREEZE_QUANTITY,
    REFUSED_SMALLER_THAN_ONE_LOT,
    TradeCapitalBoundsGate,
    WITHIN_BOUNDS,
)
from runtime.risk_types import TradeCapitalBounds
from runtime.trading_types import BUY

VENUE = "upstox"
SEGMENT = "index-options"

# Upstox's instrument master for the NIFTY weekly chain, read 2026-09-12.
NIFTY_CONTRACT = "NIFTY 24550 CE 08 SEP 26"
NIFTY_LOT = 65.0
NIFTY_FREEZE = 1755.0

# The segment's own bounds, from
# ~/.config/ajit-segment-bots/settings/segments/index-options.toml.
MINIMUM_CAPITAL = 100_000.0
MAXIMUM_CAPITAL = 200_000.0

# The touch price the gate bound against, and what the book actually filled at.
DECISION_PRICE = 0.1
WALKED_FILL_PRICE = 0.169539375
# The quantity it emitted, and what that turned out to commit.
QUANTITY_EMITTED = 2_000_000.0
CAPITAL_ACTUALLY_COMMITTED = 339_078.75


def bounds(minimum=MINIMUM_CAPITAL, maximum=MAXIMUM_CAPITAL):
    return TradeCapitalBounds(SEGMENT, minimum, maximum, "INR")


def a_nifty_order(quantity, entry=DECISION_PRICE, lot=NIFTY_LOT, freeze=NIFTY_FREEZE):
    """One order for the real contract, with the real venue facts attached."""
    return SizedOrder(
        venue_id=VENUE, symbol=NIFTY_CONTRACT, side=BUY, quantity=quantity,
        entry_price=entry, stop_price=0.05, outcome=SIZED,
        risk_allowed=777_439.85, risk_at_stop=100_215.0,
        fees_charged=0.0, notional=quantity * entry, leverage=1.0, reason="",
        sized_at_ns=1, segment=SEGMENT,
        quantity_increment=lot, freeze_quantity=freeze,
    )


def a_gate():
    return TradeCapitalBoundsGate(quantity_increment=0.001)


def test_the_order_that_lost_the_most_money_is_cut_to_what_the_exchange_takes():
    """The exact order of 09:40:11 on 2026-09-08, re-run through the gate."""
    gate = a_gate()
    result = gate.bound(a_nifty_order(QUANTITY_EMITTED), bounds(), True)

    assert result.quantity <= NIFTY_FREEZE, (
        f"the gate emitted {result.quantity:g} units of a contract NSE accepts "
        f"{NIFTY_FREEZE:g} of in one order"
    )
    assert result.quantity == NIFTY_FREEZE - (NIFTY_FREEZE % NIFTY_LOT), (
        "the cut must land on a whole number of lots, not on the freeze quantity itself"
    )
    assert gate.standing.capped_at_the_freeze_quantity == 1
    assert result.may_be_sent, "a smaller order is still the trade the operator asked for"


def test_the_capital_that_reaches_the_book_can_no_longer_exceed_the_ceiling():
    """The bound has to survive the price the order itself creates.

    The old order committed 339,078.75 against a 200,000 ceiling *at the price it
    was filled at*, because 2,000,000 units is far more than the book holds. At a
    size the exchange would actually accept there is no walk of that size to make.
    """
    gate = a_gate()
    result = gate.bound(a_nifty_order(QUANTITY_EMITTED), bounds(), True)

    committed_at_the_walked_price = result.quantity * WALKED_FILL_PRICE
    assert committed_at_the_walked_price <= MAXIMUM_CAPITAL, (
        f"{committed_at_the_walked_price:,.2f} would still breach the "
        f"{MAXIMUM_CAPITAL:,.2f} ceiling even at the price the book really gave"
    )
    # What it used to be, kept as the number this test exists to prevent.
    assert QUANTITY_EMITTED * WALKED_FILL_PRICE == pytest.approx(
        CAPITAL_ACTUALLY_COMMITTED, rel=1e-6
    )


def test_an_order_inside_the_bounds_is_still_snapped_to_whole_lots():
    """The path that had no snapping at all until 2026-09-12.

    712,985 and 559,703.1818181821 units reached the book on 2026-09-08 by this
    route: the capital was inside the bounds, so neither the bump branch nor the
    cap branch ran, and those were the only two that snapped.
    """
    gate = a_gate()
    # 1,500 units at 100 rupees is 150,000 -- inside [100,000, 200,000], so no
    # capital adjustment happens and the old code emitted it untouched.
    order = a_nifty_order(1_500.5, entry=100.0, freeze=0.0)
    result = gate.bound(order, bounds(), True)

    assert result.outcome == WITHIN_BOUNDS
    assert result.quantity % NIFTY_LOT == 0, (
        f"{result.quantity:g} is not a whole number of {NIFTY_LOT:g}-unit lots"
    )
    assert result.quantity == 1_495.0
    assert gate.standing.snapped_to_whole_lots == 1
    assert result.capital_used == pytest.approx(149_500.0), (
        "the capital must be restated at the quantity actually emitted"
    )


def test_an_order_smaller_than_one_lot_is_refused_by_name():
    """Cutting to zero would be a refusal reported as a success.

    The same argument `REFUSED_MAXIMUM_BUYS_NOTHING` already carries: on
    2026-09-05 a zero-quantity order went out reported as `capped-at-maximum`,
    and the gate, the router and the book each reported nothing wrong while no
    order existed.
    """
    gate = a_gate()
    result = gate.bound(a_nifty_order(40.0, entry=100.0, freeze=0.0), bounds(minimum=1.0), True)

    assert result.outcome == REFUSED_SMALLER_THAN_ONE_LOT
    assert not result.may_be_sent
    assert result.quantity == 0.0
    assert gate.standing.refused_smaller_than_one_lot == 1


def test_a_contract_whose_lot_is_already_above_the_freeze_has_no_tradeable_size():
    """Refused outright rather than cut, because there is nothing to cut to."""
    gate = a_gate()
    # 10,000 units at 15 rupees is 150,000, inside [100,000, 200,000], so this
    # reaches the venue-limit test rather than the bump or cap branches.
    result = gate.bound(
        a_nifty_order(10_000.0, entry=15.0, lot=2_000.0, freeze=1_755.0), bounds(), True
    )

    assert result.outcome == REFUSED_ABOVE_THE_EXCHANGE_FREEZE_QUANTITY
    assert not result.may_be_sent
    assert gate.standing.refused_above_the_freeze_quantity == 1


def test_an_instrument_with_no_stated_freeze_is_not_treated_as_capped_at_nothing():
    """Unstated is not zero, and a share really does have no freeze quantity."""
    gate = a_gate()
    result = gate.bound(
        a_nifty_order(1_495.0, entry=100.0, lot=1.0, freeze=0.0), bounds(), True
    )

    assert result.may_be_sent
    assert result.quantity == 1_495.0
    assert gate.standing.capped_at_the_freeze_quantity == 0
    assert gate.standing.refused_above_the_freeze_quantity == 0


def test_a_missing_lot_size_is_counted_rather_than_silently_assumed():
    """The fallback to the global step is legitimate, and it is also the state in
    which a wrong quantity is possible. An uncounted fallback is indistinguishable
    from a lot that arrived."""
    gate = a_gate()
    gate.bound(a_nifty_order(1_500.0, entry=100.0, lot=0.0, freeze=0.0), bounds(), True)

    assert gate.standing.sized_without_the_instruments_own_lot == 1


def test_a_close_is_capped_at_the_freeze_but_never_refused():
    """A position that cannot be closed is the worst state this system has.

    The unsnapped orders of 2026-09-08 left positions holding fractional
    quantities of lot-traded contracts. Applying the part-lot refusal to an exit
    would strand exactly those positions forever, so a close is cut to what the
    exchange takes and leaves in pieces.
    """
    from runtime.trade_intent import CLOSE

    gate = a_gate()
    order = a_nifty_order(6_110_301.363636364, entry=WALKED_FILL_PRICE)
    result = gate.bound(
        SizedOrder(**{**order.__dict__, "action": CLOSE}), bounds(), True
    )

    assert result.may_be_sent, "an exit must never be refused"
    assert result.quantity == NIFTY_FREEZE
    assert gate.standing.closed == 1
    assert gate.standing.refused_smaller_than_one_lot == 0


def test_four_orders_in_one_millisecond_cannot_each_spend_the_whole_ceiling():
    """The stack of 09:42:00.309 on 2026-09-08.

    Four separate orders for `NIFTY 24550 CE 08 SEP 26` -- client order ids
    55d0de91, a0be0a0c, 594fd4b9 and 679e2e0d, eight fills between them -- were
    bound and filled inside the same millisecond. Each passed the position
    ceiling honestly, because `position` had not yet reported what the one
    before it did, and the position walked from 712,985 to 6,110,301 units.

    The ceiling is on the trade. A gate that only counts what a *message* has
    reported back is counting the wrong thing.
    """
    gate = a_gate()
    # 1,300 units at 100 is 130,000 -- inside the bounds on its own.
    order = a_nifty_order(1_300.0, entry=100.0, freeze=0.0)

    first = gate.bound(order, bounds(), True)
    assert first.may_be_sent
    assert first.capital_used == pytest.approx(130_000.0)

    # Nothing has published a position yet -- exactly the state of 09:42:00.309.
    second = gate.bound(order, bounds(), True)
    third = gate.bound(order, bounds(), True)

    committed = first.capital_used + second.capital_used + third.capital_used
    assert committed <= MAXIMUM_CAPITAL, (
        f"three orders in one window committed {committed:,.2f} against a "
        f"{MAXIMUM_CAPITAL:,.2f} ceiling; each one was inside the bounds by itself"
    )


def test_a_reservation_expires_so_an_order_that_never_filled_cannot_hold_the_ceiling():
    """Unbounded reservations are the trap this project has paid for three times.

    Most recently 2026-08-26, when four parts read `part-resource-usage` with no
    age bound and every part the governor had ever switched off still counted as
    running.
    """
    now = [1_000_000_000]
    gate = TradeCapitalBoundsGate(
        quantity_increment=0.001, now_ns=lambda: now[0],
        bound_capital_stands_for_seconds=30.0,
    )
    order = a_nifty_order(1_950.0, entry=100.0, freeze=0.0)

    first = gate.bound(order, bounds(), True)
    assert first.may_be_sent

    # Thirty-one seconds later no position ever arrived. The reserve must lapse
    # rather than bound the ceiling for the rest of the session.
    now[0] += int(31 * 1e9)
    later = gate.bound(order, bounds(), True)

    assert later.may_be_sent, "an order that never filled cannot hold the ceiling for ever"
    assert gate.standing.reservations_that_expired == 1


def test_a_position_message_replaces_the_reservation_rather_than_adding_to_it():
    """Otherwise the same capital is counted twice and the ceiling halves itself."""
    gate = a_gate()
    order = a_nifty_order(1_300.0, entry=100.0, freeze=0.0)

    gate.bound(order, bounds(), True)
    # The fill lands and `position` reports what is actually held.
    gate.observe_position(VENUE, NIFTY_CONTRACT, 130_000.0)

    assert gate.held_capital(order) == pytest.approx(130_000.0), (
        "the reserve and the reported position are the same capital, not two lots of it"
    )
