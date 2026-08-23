"""A position opens, rests its exits, and closes -- on prices BTCUSDT really traded at.

The round trip, end to end, through the parts that make it: the fill becomes a
position, the plan's exits are chained to it the instant it fills, both rest in the
paper book, a live price reaches one of them, the other is withdrawn, and the trade
is closed and accounted for in USDT.

**Why this test exists.** Before it, the system could open a paper position and had
no way to close one: ten parts had working code and no `start_part`, and the paper
book discarded any order the market had not already reached -- so a stop, whose
entire purpose is to wait, could not wait. On the live run of 2026-08-22 17:37-18:42
nothing filled at all, because entries were routed as limit orders at the price the
decision was made at and dropped the moment the market moved off it.

**Every price here is a real trade** (RL-063), read through the venue's own adapter
from the tape this machine recorded on 2026-08-22. The exits are placed at prices
that series actually reaches, which is why the test can assert that one triggers:
a target placed at a round fraction would sit outside the 0.005% the captured run
covers and would prove only that nothing happened.

This wires the engines directly rather than forking processes. The process-level
wiring of the entry half is proven by `test_an_intent_becomes_a_paper_fill`, and
what is under test here is the chain of decisions that closes a position.
"""

from __future__ import annotations

import pytest

from parts.paper_live_trading.paper_fill_simulator import (
    FILLED,
    RESTING_STOP,
    PaperFillSimulator,
)
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
from parts.portfolio_state.usdt_pnl_accountant import UsdtPnlAccountant
from parts.risk_capital_allocation.exit_order_chainer import CHAINED, ExitOrderChainer
from runtime.trading_types import (
    BUY,
    LONG,
    MARKET,
    SELL,
    STOP_MARKET,
    TAKE_PROFIT_MARKET,
)
from runtime.venues.adapter_registry import load_venue_adapter

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
QUANTITY = 0.01

# The venue's published taker rate for USDⓈ-M futures. Not a measurement -- it is
# a schedule -- and it is charged on both the entry and the exit, which is what
# makes a round trip cost twice what a single fill suggests.
TAKER_FEE_RATE = 0.0004
MAKER_FEE_RATE = 0.0002


class Mode:
    """What `money-mode-reader` publishes. Paper, and checked, never assumed."""

    mode = "paper"


@pytest.fixture(scope="module")
def real_trades(read_captured_payloads):
    """Every BTCUSDT trade in the captured run, in the order the venue sent them."""
    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, "2026-08-22-btcusdt-aggtrade-run.jsonl")
    trades = [trade for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(trades) >= 500, f"the captured run holds only {len(trades)} trades"
    return trades


def a_price_the_run_rises_to(prices) -> tuple[float, float]:
    """An entry, and a target above it that this real series actually reaches.

    Returns the entry price and a target between it and the highest price that
    follows, so the target is reached by a move the market genuinely made.
    """
    entry = prices[0]
    highest = max(prices)
    assert highest > entry, (
        f"the captured run never rose above its first trade ({entry} to {highest}); "
        f"a target test on this series would assert nothing"
    )
    return entry, entry + (highest - entry) / 2


class TheClosingChain:
    """The six parts that turn a fill into a closed trade, wired as the blueprint wires them.

    Each edge here is a declared contract: fill -> position, fill -> cost basis,
    position + price -> excursion, plan + fill -> stop adjustment, adjustment ->
    order request, fill -> closed trade, closed trade -> statement. Nothing reaches
    across; every part is handed only what it declares it consumes.
    """

    def __init__(self) -> None:
        self.book = PaperFillSimulator(
            taker_fee_rate=TAKER_FEE_RATE, maker_fee_rate=MAKER_FEE_RATE
        )
        self.reconciler = FillReconciler(quantity_tolerance=0.001)
        self.cost_basis = CostBasisTracker()
        self.excursions = PeakExcursionTracker()
        self.chainer = ExitOrderChainer()
        self.stops = StopOrderManager()
        self.closes = PositionCloseDetector()
        self.accountant = UsdtPnlAccountant()
        self.closed_trades = []
        self.statements = []
        self.exit_orders_sent = []
        self.positions_held = {}

    def apply_fill(self, fill) -> None:
        """One fill through everything that reads a fill."""
        position = self.reconciler.observe_fill(fill)
        self.cost_basis.observe_fill(fill)
        self.excursions.observe_position(position)
        key = (fill.venue_id, fill.symbol)
        was_held = self.positions_held.get(key, 0.0)
        self.positions_held[key] = position.quantity

        trade = self.closes.observe_fill(fill)
        if trade is not None:
            self.closed_trades.append(trade)
            self.statements.append(self.accountant.state(trade, "USDT"))

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

        # A position that has just gone flat has its remaining exit withdrawn.
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

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        """One live price: excursions move, and the book tests what is resting."""
        self.excursions.observe_price(venue_id, symbol, price)
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
    """One `order-request` as the paper book's simulate() reads it."""
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


def an_entry_order(entry_price: float) -> dict:
    """The market order the router sends for an entry (operator, 2026-08-23)."""
    return {
        "client_order_id": "entry-1",
        "venue_id": VENUE,
        "symbol": SYMBOL,
        "side": BUY,
        "quantity": QUANTITY,
        "order_type": MARKET,
        "limit_price": None,
        "money_mode": "paper",
        "is_in_flight": False,
        "fill_price_estimate": None,
        "market_price": entry_price,
        "stop_price": None,
    }


def test_a_paper_position_opens_rests_its_exits_and_closes_on_a_real_price(real_trades):
    prices = [trade.price for trade in real_trades]
    entry_price, target_price = a_price_the_run_rises_to(prices)
    # Far enough below that this run never reaches it, so the trade closes on its
    # target and the stop is the one that must be withdrawn.
    stop_price = min(prices) * 0.99

    chain = TheClosingChain()
    # The plan is registered before the entry is sent, which is the whole point of
    # the chainer: a plan made after the fill is a plan made while the position was
    # already naked.
    chain.chainer.register_plan(VENUE, SYMBOL, BUY, stop_price, target_price)

    # -- the entry, as a market order -----------------------------------------
    opened = chain.book.simulate(**an_entry_order(entry_price))
    assert opened.outcome == FILLED, f"the entry did not fill: {opened.reason}"
    chain.apply_fill(opened.fill)

    position = chain.reconciler.reconcile(VENUE, SYMBOL).position
    assert position.quantity == pytest.approx(QUANTITY)
    assert position.direction == LONG

    # -- the exits, chained the instant the entry filled ------------------------
    chained = [action for action in chain.exit_orders_sent if action.action == PLACE_NEW]
    targets = [action for action in chain.exit_orders_sent if action.action == PLACE_TARGET]
    assert chain.chainer.standing.chained == 1, "the exits were not chained to the fill"
    assert len(chained) == 1 and len(targets) == 1, (
        f"a position must get both exits: {[a.action for a in chain.exit_orders_sent]}"
    )
    assert chain.book.standing.orders_on_the_book == 2, "both exits must rest on the book"

    resting_types = {order.order_type for order in chain.book.resting_orders}
    assert resting_types == {STOP_MARKET, TAKE_PROFIT_MARKET}, (
        f"both exits must be market orders that wait for a price, not limits: {resting_types}"
    )

    # -- the market runs, and reaches the target --------------------------------
    for trade in real_trades:
        chain.observe_price(trade.venue_id, trade.symbol, trade.price)
        if chain.closed_trades:
            break

    assert chain.closed_trades, (
        f"the run rose to {max(prices)} against a target at {target_price} and nothing closed"
    )

    # -- what the closed trade says ---------------------------------------------
    closed = chain.closed_trades[0]
    assert closed.direction == LONG
    assert closed.quantity == pytest.approx(QUANTITY)
    assert closed.entry_price == pytest.approx(entry_price)
    assert closed.exit_price >= target_price, (
        "a take-profit fills at the price the trigger found, at or beyond the target"
    )
    assert closed.fees_paid > 0, "a round trip pays the venue twice, and paper must too"
    assert closed.realised_pnl == pytest.approx(
        (closed.exit_price - closed.entry_price) * QUANTITY, rel=1e-6
    )

    # -- the other exit is withdrawn -------------------------------------------
    withdrawn = [action for action in chain.exit_orders_sent if action.action == CANCEL_EXIT]
    assert withdrawn, "the stop must be withdrawn when the position closes on its target"
    assert chain.book.standing.orders_on_the_book == 0, (
        "a stop left resting on a closed position opens the opposite position when it triggers"
    )

    # -- and what it made, in the currency everything is compared in ------------
    assert chain.statements, "a closed trade with no statement is a trade nobody can add up"
    statement = chain.statements[0]
    assert statement.net_pnl_usdt == pytest.approx(
        statement.gross_pnl_usdt - statement.fees_usdt + statement.funding_usdt
    )
    assert statement.capital_used_usdt is None, (
        "nothing sizes capital per position yet, and the statement must say so rather than "
        "state a return computed from money the trade never used"
    )


def test_the_stop_closes_the_trade_when_the_market_goes_the_other_way(real_trades):
    """The same chain, closing at a loss. Both outcomes must be reachable (Rule 8).

    A round trip that could only close in profit would be a paper account that
    cannot lose, which is the most expensive way for a simulator to be wrong.
    """
    prices = [trade.price for trade in real_trades]
    entry_price = prices[0]
    lowest = min(prices)
    assert lowest < entry_price, "the captured run never fell; this test would assert nothing"
    stop_price = entry_price - (entry_price - lowest) / 2
    target_price = max(prices) * 1.01  # never reached by this run

    chain = TheClosingChain()
    chain.chainer.register_plan(VENUE, SYMBOL, BUY, stop_price, target_price)
    opened = chain.book.simulate(**an_entry_order(entry_price))
    chain.apply_fill(opened.fill)
    assert chain.book.standing.orders_on_the_book == 2

    for trade in real_trades:
        chain.observe_price(trade.venue_id, trade.symbol, trade.price)
        if chain.closed_trades:
            break

    assert chain.closed_trades, (
        f"the run fell to {lowest} against a stop at {stop_price} and nothing closed"
    )
    closed = chain.closed_trades[0]
    assert closed.exit_price <= stop_price, "a stop fills at or through its trigger"
    assert closed.realised_pnl < 0, "closing below entry is a loss, and it must read as one"
    assert chain.statements[0].net_pnl_usdt < 0


def test_a_position_never_rests_without_a_stop(real_trades):
    """A fill with no plan is reported naked, not silently ignored.

    The window between an entry filling and its stop existing is the one this
    whole chain exists to close, and a chainer that produced nothing for a fill it
    had no plan for would look exactly like one with nothing to do.
    """
    chain = TheClosingChain()
    entry_price = real_trades[0].price
    opened = chain.book.simulate(**an_entry_order(entry_price))

    naked = chain.chainer.observe_entry_fill(
        fill_id=opened.fill.fill_id, entry_order_id="entry-1",
        venue_id=VENUE, symbol=SYMBOL, entry_side=BUY, filled_quantity=QUANTITY,
    )
    assert naked.outcome != CHAINED
    assert "naked" in naked.reason
    assert chain.chainer.standing.fills_without_a_plan == 1
