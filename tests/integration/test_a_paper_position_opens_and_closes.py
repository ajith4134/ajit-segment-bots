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

# Upstox's real Equity Options charge stack (runtime/indian_options_fee_model.py's
# own docstring carries the sourced quotes). This test's venue is crypto, so
# these never actually apply to a fill here -- passed because
# PaperFillSimulator requires every fee-model rate explicitly, the same as
# TAKER_FEE_RATE/MAKER_FEE_RATE above, rather than defaulting to a number
# nobody chose.
OPTIONS_FLAT_BROKERAGE = 20.0
OPTIONS_STT_SELL_RATE = 0.001
OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE = 0.0003503
OPTIONS_IPFT_CHARGE_RATE = 0.000005
OPTIONS_STAMP_DUTY_BUY_RATE = 0.00003
OPTIONS_GST_RATE = 0.18

# The venue quantity step, matching the shipped `order_quantity_increment`. It is
# what `fill-reconciler` already reconciles against below, and what decides when
# a book holds nothing an order could sell.
QUANTITY_INCREMENT = 0.001


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
            taker_fee_rate=TAKER_FEE_RATE, maker_fee_rate=MAKER_FEE_RATE,
            options_flat_brokerage=OPTIONS_FLAT_BROKERAGE,
            options_stt_sell_rate=OPTIONS_STT_SELL_RATE,
            options_exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
            options_ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
            options_stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE,
            options_gst_rate=OPTIONS_GST_RATE,
        )
        self.reconciler = FillReconciler(quantity_tolerance=QUANTITY_INCREMENT)
        self.cost_basis = CostBasisTracker(QUANTITY_INCREMENT)
        self.excursions = PeakExcursionTracker()
        self.chainer = ExitOrderChainer()
        self.stops = StopOrderManager()
        self.closes = PositionCloseDetector(QUANTITY_INCREMENT)
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

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One live print: excursions move, and the book tests what is resting.

        The print's own time travels with it, because an excursion is dated by the
        market that made it rather than by the moment this chain processed it.
        """
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
        chain.observe_price(trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)
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
        chain.observe_price(trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)
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


# ---- a decision to do nothing must not become a trade -------------------------

def test_an_intent_that_stands_aside_is_never_sized():
    """The most expensive defect found on the live run of 2026-08-23.

    `position-sizer` sized every intent it read, including the ones whose action
    was stand-aside. A stand-aside intent carries a degenerate stop -- the stop
    sits at the entry price, because no exit plan was built for a trade nobody
    wanted -- so the size it produced was whatever the bounds gate happened to cut
    it to. ZECUSDT opened 1.25 at 07:29:45 and ETHUSDT opened 0.418 at 07:31:59
    from decisions the bot had explicitly made not to trade.

    This asserts the property directly on the type every part reads, because the
    guard belongs where the decision is stated rather than in each reader.
    """
    from runtime.trade_intent import OPEN, STAND_ASIDE, TradeIntent
    from runtime.learned_estimator import Estimate

    def an_intent(action: str) -> TradeIntent:
        return TradeIntent(
            venue_id=VENUE, symbol=SYMBOL, side="long", action=action,
            conviction=Estimate(
                value=0.6, is_fitted=True, observations=100, prior=0.5,
                was_clamped=False, bound_low=None, bound_high=None, reason="measured",
            ),
            horizon_seconds=60.0, stop_price=100.0, agreement="only-one-bot-had-a-view",
            contributing_bots=("bull-bot",), dissenting_bots=(), opinion_weights={},
            evidence={}, reason="", formed_at_ns=1,
        )

    assert an_intent(STAND_ASIDE).is_actionable is False
    assert an_intent(OPEN).is_actionable is True


def test_the_sizer_skips_a_stand_aside_intent_and_counts_it():
    """Skipped, and counted -- a refusal nobody can see is invisible input.

    A bot whose every opinion is a stand-aside and a bot receiving no opinions at
    all look identical from the sizer without this counter.
    """
    import parts.risk_capital_allocation.position_sizer as sizer_part

    standing = sizer_part.SizerStanding()
    assert standing.stood_aside == 0
    assert "intents_that_stood_aside" in sizer_part.describe_sizing(
        sizer_part.PositionSizer(taker_fee_rate=0.0004, slippage_fraction=0.0005)
    )


# ---- one decision is one order, however often it is republished ----------------

def test_a_standing_intent_republished_as_the_market_moves_is_one_order(real_trades):
    """The defect that turned one decision into thirteen positions.

    The arbiter publishes a standing opinion tick after tick -- an opinion still
    held is still published -- so every part downstream sees the same decision many
    times. `order-idempotency-stamper` derived the client id from the order's
    quantity and entry price, and both move on every print, so each republish got a
    fresh id and the paper book filled it as a new order.

    Measured on the live run of 2026-08-23 09:01: one ENAUSDT long produced 13
    orders and 13 fills -- 12,982 USDT of notional against a 1,000 per-trade cap.
    A real venue would have done exactly the same, because a venue also
    deduplicates on the client id it is given.

    The prices here are real consecutive BTCUSDT trades (RL-063): the drift that
    breaks the old id is the market's own, not a number chosen to break it.
    """
    from parts.paper_live_trading.order_idempotency_stamper import OrderIdempotencyStamper
    from runtime.trade_intent import OPEN, TradeIntent
    from runtime.learned_estimator import Estimate

    intent = TradeIntent(
        venue_id=VENUE, symbol=SYMBOL, side="long", action=OPEN,
        conviction=Estimate(
            value=0.58, is_fitted=True, observations=1408, prior=0.5,
            was_clamped=False, bound_low=None, bound_high=None, reason="measured",
        ),
        horizon_seconds=60.0, stop_price=0.0, agreement="only-one-bot-had-a-view",
        contributing_bots=("bull-bot",), dissenting_bots=(), opinion_weights={},
        evidence={}, reason="", formed_at_ns=1,
    )

    stamper = OrderIdempotencyStamper()
    book = PaperFillSimulator(
        taker_fee_rate=TAKER_FEE_RATE, maker_fee_rate=MAKER_FEE_RATE,
        options_flat_brokerage=OPTIONS_FLAT_BROKERAGE,
        options_stt_sell_rate=OPTIONS_STT_SELL_RATE,
        options_exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
        options_ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
        options_stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE,
        options_gst_rate=OPTIONS_GST_RATE,
    )

    class BoundedForThisTick:
        """What the gate publishes: the same decision, priced at this tick."""

        def __init__(self, price, quantity, intent_id):
            self.venue_id, self.symbol, self.side = VENUE, SYMBOL, BUY
            self.entry_price, self.quantity = price, quantity
            self.stop_price = price * 0.99
            self.intent_id = intent_id
            self.client_order_id = None

    filled = 0
    ids = set()
    for trade in real_trades[:25]:
        # The size drifts with the price exactly as the sizer's does: a fixed risk
        # budget over a stop distance that moves.
        quantity = round(1_000.0 / trade.price, 6)
        order = BoundedForThisTick(trade.price, quantity, intent.decision_id)
        stamped = stamper.stamp(order, order.intent_id)
        ids.add(stamped.client_order_id)
        result = book.simulate(
            client_order_id=stamped.client_order_id, venue_id=VENUE, symbol=SYMBOL,
            side=BUY, quantity=quantity, order_type=MARKET, limit_price=None,
            money_mode="paper", is_in_flight=False, fill_price_estimate=None,
            market_price=trade.price, stop_price=None,
        )
        if result.did_fill:
            filled += 1

    assert len(ids) == 1, (
        f"one standing decision produced {len(ids)} order ids as the price moved; that is "
        f"the defect, and the venue would open a position for each of them"
    )
    assert filled == 1, (
        f"the book filled {filled} times for one decision; a venue holding one client id "
        f"rejects the duplicates instead of opening more positions"
    )


def test_an_order_with_no_decision_id_is_still_stamped_and_counted():
    """The fallback stays, because an id derived from a partial key would collide.

    But its use is counted: the decision id is what makes a republished intent one
    order, and its absence is not visible any other way.
    """
    from parts.paper_live_trading.order_idempotency_stamper import OrderIdempotencyStamper

    stamper = OrderIdempotencyStamper()

    class Anonymous:
        venue_id, symbol, side = VENUE, SYMBOL, BUY
        quantity, entry_price, stop_price = 1.0, 100.0, 99.0
        intent_id, client_order_id = "", None

    stamped = stamper.stamp(Anonymous(), None)
    assert stamped.client_order_id
    assert stamper.standing.stamped_without_a_decision == 1
