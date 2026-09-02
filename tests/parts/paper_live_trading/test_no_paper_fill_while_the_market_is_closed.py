"""A paper fill outside a trading session is a trade that could not have
happened.

Without this, an order placed at 18:00 IST fills at the last price the part saw
at 15:29 and is journalled as a real trade -- which closed-trade-decoding then
decodes, the learning loop learns from, and the trade board shows. The session
states are what market-session-calendar publishes from NSE's own holiday list.
"""

import datetime

from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.tape import TradeFidelity
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


# ---- filling against history, while the wall clock says the market is shut ----

def test_a_historical_bar_close_fills_while_the_wall_clock_says_closed():
    """Phase A paper-trades on history for the hours the market is shut.

    The evidence that this fill was possible is the bar itself: Upstox serves a
    one-minute bar for 09:15 IST on 2026-09-01 only because the market traded
    that minute. A bar's existence proves its own session, so no calendar lookup
    is needed and none is done -- what would be wrong is filling a *live* price
    at a moment nobody was trading, which is what the guard above still stops.
    """
    result = _market_order_at(
        _simulator(CLOSED), fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE
    )
    assert result.outcome == FILLED
    assert result.did_fill is True


def test_a_live_price_still_does_not_fill_while_the_market_is_closed():
    """The regression this must not undo. Everything above about an 18:00 order
    filling at the 15:29 price stays true for a live price."""
    result = _market_order_at(_simulator(CLOSED), fidelity=TradeFidelity.EVERY_PRINT)
    assert result.outcome == RESTING_MARKET_CLOSED


def test_a_historical_bar_fills_even_before_any_session_has_been_measured():
    """A replay runs when nothing is publishing a session at all. The bar is its
    own evidence, so it does not need one."""
    result = _market_order_at(
        _simulator(), fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE
    )
    assert result.outcome == FILLED


def test_a_last_traded_price_is_a_live_price_and_is_still_gated():
    """LAST_TRADED_PRICE_ONLY is Upstox's live ticker, not history. Only the
    historical value is evidence of its own session -- a coarse live price is
    still a live price."""
    result = _market_order_at(
        _simulator(CLOSED), fidelity=TradeFidelity.LAST_TRADED_PRICE_ONLY
    )
    assert result.outcome == RESTING_MARKET_CLOSED


def _market_order_at(simulator, fidelity, client_order_id="order-h", price=222.0):
    return simulator.simulate(
        client_order_id=client_order_id, venue_id="upstox",
        symbol="NIFTY 24500 CE", side="buy", quantity=75.0, order_type="market",
        limit_price=None, money_mode="paper", is_in_flight=False,
        market_price=price, price_fidelity=fidelity,
    )


def test_a_resting_stop_triggers_on_a_historical_bar_while_the_clock_says_shut():
    """The other half of the win condition: a position has to be able to close.

    An exit is a resting stop, and it fills when the market reaches it. On a
    replay the market reaching it is a historical bar -- so a stop that could
    only trigger against a live price could open a position on history and never
    close it, which is a paper account that only ever loses its exits.
    """
    simulator = _simulator(CLOSED)
    _market_order_at(simulator, fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE)
    simulator.simulate(
        client_order_id="stop-1", venue_id="upstox", symbol="NIFTY 24500 CE",
        side="sell", quantity=75.0, order_type="stop-market", limit_price=None,
        money_mode="paper", is_in_flight=False, stop_price=200.0, market_price=222.0,
        price_fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE,
    )

    fills = simulator.evaluate_resting(
        {("upstox", "NIFTY 24500 CE"): 195.0},
        price_fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE,
    )

    assert [fill.client_order_id for fill in fills] == ["stop-1"]


def test_a_resting_stop_still_does_not_trigger_on_a_live_price_while_shut():
    """The regression guard, on the resting path too."""
    simulator = _simulator(CLOSED)
    simulator.simulate(
        client_order_id="stop-2", venue_id="upstox", symbol="NIFTY 24500 CE",
        side="sell", quantity=75.0, order_type="stop-market", limit_price=None,
        money_mode="paper", is_in_flight=False, stop_price=200.0, market_price=222.0,
        price_fidelity=TradeFidelity.HISTORICAL_BAR_CLOSE,
    )

    assert simulator.evaluate_resting(
        {("upstox", "NIFTY 24500 CE"): 195.0},
        price_fidelity=TradeFidelity.EVERY_PRINT,
    ) == ()
