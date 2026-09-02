"""A paper fill outside a trading session is a trade that could not have
happened.

Without this, an order placed at 18:00 IST fills at the last price the part saw
at 15:29 and is journalled as a real trade -- which closed-trade-decoding then
decodes, the learning loop learns from, and the trade board shows. The session
states are what market-session-calendar publishes from NSE's own holiday list.
"""

import datetime

from runtime.market_conditions import MarketSessionState, SessionKind
from parts.paper_live_trading.paper_fill_simulator import (
    FILLED,
    RESTING_MARKET_CLOSED,
    PaperFillSimulator,
)

OBSERVED_AT_NS = 1_756_800_000_000_000_000
TRADING_DAY = datetime.date(2026, 9, 2)

OPEN = MarketSessionState(
    segment="FO", kind=SessionKind.OPEN, as_of_date=TRADING_DAY,
    reason="within stated session hours", observed_at_ns=OBSERVED_AT_NS,
)
CLOSED = MarketSessionState(
    segment="FO", kind=SessionKind.CLOSED, as_of_date=TRADING_DAY,
    reason="outside stated session hours", observed_at_ns=OBSERVED_AT_NS,
)
HOLIDAY = MarketSessionState(
    segment="FO", kind=SessionKind.HOLIDAY, as_of_date=datetime.date(2026, 1, 26),
    reason="Republic Day", observed_at_ns=OBSERVED_AT_NS,
)


def _simulator(session=None):
    simulator = PaperFillSimulator(
        taker_fee_rate=0.0004, maker_fee_rate=0.0002,
        options_flat_brokerage=20.0, options_stt_sell_rate=0.001,
        options_exchange_transaction_charge_rate=0.0003503,
        options_ipft_charge_rate=0.000005, options_stamp_duty_buy_rate=0.00003,
        options_gst_rate=0.18,
    )
    if session is not None:
        simulator.observe_session(session)
    return simulator


def _market_order(simulator, client_order_id="order-1", price=222.0):
    return simulator.simulate(
        client_order_id=client_order_id, venue_id="upstox",
        symbol="NIFTY 24500 CE", side="buy", quantity=75.0, order_type="market",
        limit_price=None, money_mode="paper", is_in_flight=False,
        market_price=price,
    )


def test_a_market_order_fills_while_the_session_is_open():
    """The control: everything below must differ only in the session."""
    result = _market_order(_simulator(OPEN))
    assert result.outcome == FILLED


def test_a_market_order_rests_instead_of_filling_while_the_market_is_closed():
    result = _market_order(_simulator(CLOSED))
    assert result.outcome == RESTING_MARKET_CLOSED
    assert result.did_fill is False


def test_it_rests_on_a_holiday_too_and_says_which_holiday():
    result = _market_order(_simulator(HOLIDAY))
    assert result.outcome == RESTING_MARKET_CLOSED
    assert "Republic Day" in result.reason


def test_a_simulator_that_has_never_seen_a_session_does_not_fill():
    """Rule 8: absence of evidence is its own state. An unmeasured session is
    not an open one."""
    simulator = _simulator()
    assert simulator.may_fill is False
    result = _market_order(simulator)
    assert result.outcome == RESTING_MARKET_CLOSED
    assert "no trading session has been measured" in result.reason


def test_an_order_rested_by_the_close_is_on_the_book_not_lost():
    """It waits for the open. Refusing it would drop an instruction the
    operator gave; filling it would invent a trade."""
    simulator = _simulator(CLOSED)
    _market_order(simulator)
    assert simulator.standing.orders_on_the_book == 1
    assert simulator.standing.rested_market_closed == 1


def test_the_order_rested_overnight_fills_when_the_session_opens():
    simulator = _simulator(CLOSED)
    _market_order(simulator)
    simulator.observe_session(OPEN)
    fills = simulator.evaluate_resting({("upstox", "NIFTY 24500 CE"): 222.0})
    assert [fill.outcome for fill in fills] == [FILLED]


def test_no_resting_order_is_tested_against_a_price_while_the_market_is_closed():
    """A price can still arrive after the close -- a late print, a republished
    level. Testing a stop against it fills at a price nothing traded at in a
    session that was not running."""
    simulator = _simulator(OPEN)
    simulator.simulate(
        client_order_id="stop-1", venue_id="upstox", symbol="NIFTY 24500 CE",
        side="sell", quantity=75.0, order_type="stop-market", limit_price=None,
        money_mode="paper", is_in_flight=False, stop_price=200.0, market_price=222.0,
    )
    simulator.observe_session(CLOSED)
    assert simulator.evaluate_resting({("upstox", "NIFTY 24500 CE"): 190.0}) == ()


def test_a_resting_stop_is_not_withdrawn_by_the_close():
    """Protection is not cancelled because the day ended."""
    simulator = _simulator(OPEN)
    simulator.simulate(
        client_order_id="stop-1", venue_id="upstox", symbol="NIFTY 24500 CE",
        side="sell", quantity=75.0, order_type="stop-market", limit_price=None,
        money_mode="paper", is_in_flight=False, stop_price=200.0, market_price=222.0,
    )
    simulator.observe_session(CLOSED)
    simulator.evaluate_resting({("upstox", "NIFTY 24500 CE"): 190.0})
    assert simulator.standing.orders_on_the_book == 1


def test_a_stop_already_through_its_trigger_rests_rather_than_filling_after_hours():
    """The one path that fills on arrival rather than resting. After the close
    the price that put it through the trigger is from before the close."""
    simulator = _simulator(CLOSED)
    result = simulator.simulate(
        client_order_id="stop-2", venue_id="upstox", symbol="NIFTY 24500 CE",
        side="sell", quantity=75.0, order_type="stop-market", limit_price=None,
        money_mode="paper", is_in_flight=False, stop_price=200.0, market_price=190.0,
    )
    assert result.did_fill is False
