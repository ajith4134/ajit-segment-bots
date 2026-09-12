"""An options position opens, rests its exits, and closes -- picking its own
contract from real Upstox instrument data, and paying Upstox's real charge
stack on both the entry and the exit fill.

Mirrors test_a_paper_position_opens_and_closes.py's closing chain exactly --
same six parts (fill-reconciler, cost-basis-tracker, peak-excursion-tracker,
exit-order-chainer, stop-order-manager, position-close-detector), same
wiring. What this test proves that the crypto one cannot: that no
crypto-only assumption survived the swap to venue_id="upstox" and an option
contract_symbol, and that the two option-specific pieces built this session
-- instrument-selector's ATM pick and paper-fill-simulator's Upstox fee
branch -- compose correctly with the pre-existing, generic closing chain.

**What is and isn't real data here (RL-063).** The NIFTY listing, the
contract's own trading_symbol/lot_size/strike, and the greeks shape are
Upstox's own real field shapes -- the same ones already verified against
Upstox's documented Options sample and reused from
tests/parts/segment_bot/test_instrument_selector_options.py. What is NOT
real yet: no Upstox options tape is captured on this machine (the crypto
live spine is inactive since the goal pivot to Indian markets, and no
options capture has started), so the premium walk below is a stated,
documented sequence rather than a captured print series the way the
BTCUSDT test's real_trades fixture is. This test should move to a captured
run the moment one exists.
"""

from __future__ import annotations

import datetime

import pytest

from parts.paper_live_trading.paper_fill_simulator import FILLED, PaperFillSimulator
from parts.paper_live_trading.stop_order_manager import (
    CANCEL_EXIT,
    PLACE_NEW,
    PLACE_TARGET,
    StopOrderManager,
    as_order_request,
)
from parts.portfolio_state.cost_basis_tracker import CostBasisTracker
from parts.portfolio_state.fill_reconciler import FillReconciler
from parts.portfolio_state.peak_excursion_tracker import PeakExcursionTracker
from parts.portfolio_state.position_close_detector import PositionCloseDetector
from parts.portfolio_state.inr_pnl_accountant import InrPnlAccountant
from parts.risk_capital_allocation.exit_order_chainer import CHAINED, ExitOrderChainer
from parts.segment_bot.instrument_selector import CHOSEN, InstrumentSelector, OPTION
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.brokers.broker_adapter import BrokerOptionGreeks, InstrumentListing
from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.learned_estimator import Estimate
from runtime.price_staleness import PriceStalenessEstimator
from runtime.trade_intent import LONG as INTENT_LONG, OPEN, SOLE_OPINION, TradeIntent
from runtime.trading_types import BUY, LONG, MARKET, SELL, STOP_MARKET, TAKE_PROFIT_MARKET

VENUE = "upstox"
UNDERLYING_SYMBOL = "NIFTY"
NOW_NS = 1_740_000_000_000_000_000

# Upstox's real Equity Options charge stack (upstox.com/brokerage-charges/,
# scraped 2026-09-01 -- runtime/indian_options_fee_model.py's own docstring
# carries the sourced quote per component).
OPTIONS_FLAT_BROKERAGE = 20.0
OPTIONS_STT_SELL_RATE = 0.001
OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE = 0.0003503
OPTIONS_IPFT_CHARGE_RATE = 0.000005
OPTIONS_STAMP_DUTY_BUY_RATE = 0.00003
OPTIONS_GST_RATE = 0.18

# The paper book does not fill outside a trading session (2026-09-02). These
# integration runs are a trading day by construction, so they say so once here
# rather than threading a session through every step.
OPEN_SESSION = MarketSessionState(
    segment="FO", kind=SessionKind.OPEN, as_of_date=datetime.date(2026, 9, 2),
    reason="within stated session hours", observed_at_ns=1_756_800_000_000_000_000,
)
# A crypto venue never fires in this test (venue_id is always "upstox"), but
# PaperFillSimulator still requires these explicitly, same as every other
# construction site in this codebase.
TAKER_FEE_RATE = 0.0004
MAKER_FEE_RATE = 0.0002

# Options trade in whole lots (75 for NIFTY); there is no partial-lot fill to
# tolerate here, so this only bounds float reconciliation noise.
QUANTITY_INCREMENT = 1.0

NIFTY_UNDERLYING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)


def _call(instrument_key, strike, expiry_ms):
    return InstrumentListing(
        instrument_key=instrument_key, exchange="NSE", segment="NSE_FO",
        instrument_type="CE", trading_symbol=f"NIFTY {strike:g} CE",
        lot_size=75, tick_size=0.05, freeze_quantity=1800.0, expiry_ms=expiry_ms,
        strike_price=strike, underlying_key="NSE_INDEX|Nifty 50",
        intraday_margin_percent=None, intraday_leverage=None,
    )


class Mode:
    """What money-mode-reader publishes. Paper, and checked, never assumed."""

    mode = "paper"


def a_selector() -> InstrumentSelector:
    return InstrumentSelector(
        built_segments=("index-options",),
        maximum_cost_fraction=0.5,
        round_trip_cost_fraction=0.001,
        price_staleness=PriceStalenessEstimator(
            materiality_fraction=0.001, anchor_seconds=1.0, quantile=0.95,
            window=50, observations_needed=5, prior_one_second_move=0.0005,
            minimum_age_seconds=0.5, maximum_age_seconds=120.0,
        ),
    )


def a_bullish_intent() -> TradeIntent:
    return TradeIntent(
        venue_id=VENUE, symbol=UNDERLYING_SYMBOL, side=INTENT_LONG, action=OPEN,
        conviction=Estimate(
            value=0.7, is_fitted=True, observations=50, prior=0.5,
            was_clamped=False, bound_low=None, bound_high=None, reason="test",
        ),
        horizon_seconds=3600.0, stop_price=None, agreement=SOLE_OPINION,
        contributing_bots=("bull-bot",), dissenting_bots=(), opinion_weights={},
        evidence={}, reason="test intent", formed_at_ns=NOW_NS,
    )


@pytest.fixture(scope="module")
def chosen_contract():
    """instrument-selector's real ATM pick, from real Upstox instrument data
    -- the entry point every test below trades against. Delta 0.51 is
    closest to the 0.5 ATM target among a one-strike universe, matching
    test_instrument_selector_options.py's own fixture shape."""
    selector = a_selector()
    selector.observe_option_listing(NIFTY_UNDERLYING)
    selector.observe_option_listing(_call("NSE_FO|1001", 24500.0, NOW_NS // 1_000_000 + 100_000_000))
    selector.observe_option_greeks(
        BrokerOptionGreeks(
            instrument_key="NSE_FO|1001", delta=0.51, theta=-2.0, gamma=0.001,
            vega=5.0, rho=0.5, implied_volatility=0.15, broker_time_ns=NOW_NS,
        )
    )
    selector.observe_price(VENUE, UNDERLYING_SYMBOL, 24500.0, NOW_NS)
    selector.observe_option_price("NSE_FO|1001", 220.0, NOW_NS)

    choice = selector.select(a_bullish_intent(), now_ns=NOW_NS)
    assert choice.state == CHOSEN, f"instrument-selector refused every candidate: {choice.rejected}"
    assert choice.chosen.instrument_kind == OPTION
    return choice.chosen


ENTRY_PRICE = 220.0
TARGET_PRICE = 260.0
STOP_PRICE_NEVER_REACHED = 150.0
TARGET_PRICE_NEVER_REACHED = 320.0
STOP_PRICE_REACHED = 190.0

# A documented premium walk, not a captured print series -- see the module
# docstring. Each step is a plausible move for an ATM NIFTY call as the
# underlying drifts; the point of the walk is that it actually crosses the
# level under test, the same requirement the crypto test's real series meets.
RISING_WALK = [220.0, 228.0, 236.0, 244.0, 252.0, 260.0, 268.0]
FALLING_WALK = [220.0, 212.0, 204.0, 196.0, 190.0, 182.0, 174.0]


class TheClosingChain:
    """The six parts that turn a fill into a closed trade, wired as the
    blueprint wires them -- identical to
    test_a_paper_position_opens_and_closes.py's, so this test is a proof
    about the venue/symbol, not a second implementation of the chain."""

    def __init__(self) -> None:
        self.book = PaperFillSimulator(
            taker_fee_rate=TAKER_FEE_RATE, maker_fee_rate=MAKER_FEE_RATE,
            options_flat_brokerage=OPTIONS_FLAT_BROKERAGE,
            options_stt_sell_rate=OPTIONS_STT_SELL_RATE,
            options_exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
            options_ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
            options_stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE,
            options_gst_rate=OPTIONS_GST_RATE,
        )
        self.book.observe_session(OPEN_SESSION)
        self.reconciler = FillReconciler(quantity_tolerance=QUANTITY_INCREMENT)
        self.cost_basis = CostBasisTracker(QUANTITY_INCREMENT)
        self.excursions = PeakExcursionTracker()
        self.chainer = ExitOrderChainer()
        self.stops = StopOrderManager()
        self.closes = PositionCloseDetector(QUANTITY_INCREMENT)
        self.accountant = InrPnlAccountant()
        self.closed_trades = []
        self.statements = []
        self.exit_orders_sent = []
        self.positions_held = {}

    def apply_fill(self, fill) -> None:
        position = self.reconciler.observe_fill(fill)
        self.cost_basis.observe_fill(fill)
        self.excursions.observe_position(position)
        key = (fill.venue_id, fill.symbol)
        was_held = self.positions_held.get(key, 0.0)
        self.positions_held[key] = position.quantity

        trade = self.closes.observe_fill(fill)
        if trade is not None:
            self.closed_trades.append(trade)
            self.statements.append(self.accountant.state(trade, "INR"))

        exits = self.chainer.observe_entry_fill(
            fill_id=fill.fill_id,
            entry_order_id=fill.order_id or fill.fill_id,
            venue_id=fill.venue_id,
            symbol=fill.symbol,
            entry_side=fill.side,
            filled_quantity=fill.quantity,
        )
        if exits is not None and exits.should_be_sent:
            self.send_exits(exits)

        if was_held and position.is_flat:
            for cancel in self.stops.observe_position_closed(*key):
                self.exit_orders_sent.append(cancel)
                self.book.simulate(**as_paper_order(as_order_request(cancel)))

    def send_exits(self, exits) -> None:
        direction = LONG if exits.exit_side == SELL else "short"
        for action in (
            self.stops.apply_adjustment(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, stop_price=exits.stop_price, money_mode=Mode(),
            ),
            self.stops.place_target(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, target_price=exits.target_price, money_mode=Mode(),
            ),
        ):
            if not action.is_actionable:
                continue
            self.exit_orders_sent.append(action)
            self.book.simulate(**as_paper_order(as_order_request(action)))

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        self.excursions.observe_price(venue_id, symbol, price, observed_at_ns=at_ns)
        for result in self.book.evaluate_resting({(venue_id, symbol): price}):
            if result.did_fill:
                excursion = self.excursions.read(venue_id, symbol)
                if excursion is not None:
                    self.closes.observe_excursion(
                        venue_id, symbol,
                        excursion.best_unrealised, excursion.worst_unrealised,
                    )
                self.apply_fill(result.fill)


def as_paper_order(request) -> dict:
    return {
        "client_order_id": request.client_order_id,
        "venue_id": request.venue_id,
        "symbol": request.symbol,
        "side": request.side,
        "quantity": request.quantity,
        "order_type": request.order_type,
        "limit_price": request.limit_price or None,
        "money_mode": "paper",
        "is_in_flight": False,
        "fill_price_estimate": None,
        "market_price": None,
        "stop_price": request.trigger_price,
        "cancels_client_order_id": request.cancels_client_order_id,
    }


def an_entry_order(symbol: str, quantity: float, entry_price: float) -> dict:
    """The market order the router sends for an entry, buying the ATM call
    (Phase A is buy-only options -- goal.md)."""
    return {
        "client_order_id": "entry-1",
        "venue_id": VENUE,
        "symbol": symbol,
        "side": BUY,
        "quantity": quantity,
        "order_type": MARKET,
        "limit_price": None,
        "money_mode": "paper",
        "is_in_flight": False,
        "fill_price_estimate": None,
        "market_price": entry_price,
        "stop_price": None,
    }


def test_an_options_position_opens_rests_its_exits_and_closes_on_its_target(chosen_contract):
    symbol = chosen_contract.contract_symbol
    quantity = 75.0  # one NIFTY lot, per the fixture's lot_size

    chain = TheClosingChain()
    chain.chainer.register_plan(VENUE, symbol, BUY, STOP_PRICE_NEVER_REACHED, TARGET_PRICE)

    opened = chain.book.simulate(**an_entry_order(symbol, quantity, ENTRY_PRICE))
    assert opened.outcome == FILLED, f"the entry did not fill: {opened.reason}"
    chain.apply_fill(opened.fill)

    position = chain.reconciler.reconcile(VENUE, symbol).position
    assert position.quantity == pytest.approx(quantity)
    assert position.direction == LONG

    chained = [action for action in chain.exit_orders_sent if action.action == PLACE_NEW]
    targets = [action for action in chain.exit_orders_sent if action.action == PLACE_TARGET]
    assert chain.chainer.standing.chained == 1
    assert len(chained) == 1 and len(targets) == 1
    assert chain.book.standing.orders_on_the_book == 2

    resting_types = {order.order_type for order in chain.book.resting_orders}
    assert resting_types == {STOP_MARKET, TAKE_PROFIT_MARKET}

    for step, premium in enumerate(RISING_WALK):
        chain.observe_price(VENUE, symbol, premium, NOW_NS + step * 1_000_000_000)
        if chain.closed_trades:
            break

    assert chain.closed_trades, (
        f"the walk rose to {max(RISING_WALK)} against a target at {TARGET_PRICE} "
        f"and nothing closed"
    )

    closed = chain.closed_trades[0]
    assert closed.direction == LONG
    assert closed.quantity == pytest.approx(quantity)
    assert closed.entry_price == pytest.approx(ENTRY_PRICE)
    assert closed.exit_price >= TARGET_PRICE

    # The real Upstox charge stack, on both legs: entry is a buy (stamp duty,
    # no STT), exit is a sell (STT, no stamp duty) -- proving the wiring in
    # paper_fill_simulator._fill() actually charges the sourced components,
    # not an approval-tested "some nonzero fee".
    entry_cost = upstox_options_order_cost(
        quantity * ENTRY_PRICE, BUY,
        flat_brokerage=OPTIONS_FLAT_BROKERAGE, stt_sell_rate=OPTIONS_STT_SELL_RATE,
        exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
        ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
        stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE, gst_rate=OPTIONS_GST_RATE,
    )
    exit_cost = upstox_options_order_cost(
        quantity * closed.exit_price, SELL,
        flat_brokerage=OPTIONS_FLAT_BROKERAGE, stt_sell_rate=OPTIONS_STT_SELL_RATE,
        exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
        ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
        stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE, gst_rate=OPTIONS_GST_RATE,
    )
    assert closed.fees_paid == pytest.approx(entry_cost.total + exit_cost.total)
    assert closed.realised_pnl == pytest.approx(
        (closed.exit_price - closed.entry_price) * quantity, rel=1e-6
    )

    withdrawn = [action for action in chain.exit_orders_sent if action.action == CANCEL_EXIT]
    assert withdrawn, "the stop must be withdrawn when the position closes on its target"
    assert chain.book.standing.orders_on_the_book == 0

    assert chain.statements, "a closed trade with no statement is a trade nobody can add up"
    statement = chain.statements[0]
    # InrPnlAccountant reports every component in rupees (2026-09-12).
    # quote_currency (a pre-existing crypto-era naming wart, not this test's
    # to fix) -- they hold INR here, at parity, since an option premium is
    # already quoted in the account's own currency and needs no conversion.
    assert statement.net_pnl_inr == pytest.approx(
        statement.gross_pnl_inr - statement.fees_inr + statement.funding_inr
    )
    assert statement.quote_currency == "INR"


def test_an_options_position_closes_at_a_loss_when_the_premium_falls(chosen_contract):
    symbol = chosen_contract.contract_symbol
    quantity = 75.0

    chain = TheClosingChain()
    chain.chainer.register_plan(VENUE, symbol, BUY, STOP_PRICE_REACHED, TARGET_PRICE_NEVER_REACHED)
    opened = chain.book.simulate(**an_entry_order(symbol, quantity, ENTRY_PRICE))
    chain.apply_fill(opened.fill)
    assert chain.book.standing.orders_on_the_book == 2

    for step, premium in enumerate(FALLING_WALK):
        chain.observe_price(VENUE, symbol, premium, NOW_NS + step * 1_000_000_000)
        if chain.closed_trades:
            break

    assert chain.closed_trades, (
        f"the walk fell to {min(FALLING_WALK)} against a stop at {STOP_PRICE_REACHED} "
        f"and nothing closed"
    )
    closed = chain.closed_trades[0]
    assert closed.exit_price <= STOP_PRICE_REACHED
    assert closed.realised_pnl < 0, "a bought option that drops in premium loses, and must read as one"
    assert chain.statements[0].net_pnl_inr < 0


def test_an_options_position_never_rests_without_a_stop(chosen_contract):
    symbol = chosen_contract.contract_symbol
    quantity = 75.0

    chain = TheClosingChain()
    opened = chain.book.simulate(**an_entry_order(symbol, quantity, ENTRY_PRICE))

    naked = chain.chainer.observe_entry_fill(
        fill_id=opened.fill.fill_id, entry_order_id="entry-1",
        venue_id=VENUE, symbol=symbol, entry_side=BUY, filled_quantity=quantity,
    )
    assert naked.outcome != CHAINED
    assert "naked" in naked.reason
