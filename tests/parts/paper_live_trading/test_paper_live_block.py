"""Paper trading: every way a simulator lies, and what stops each one.

The value of this whole block is that paper results predict live ones. Each test
below corresponds to a specific way that prediction breaks -- filling at the touch,
filling instantly, ignoring fees, surviving a liquidation, spending money the
account does not have -- because a simulator with any one of them produces a
strategy that works on paper and loses money live.
"""

import importlib

import pytest

from parts.paper_live_trading.book_walk_fill_pricer import (
    EMPTY_SIDE, FILLED as BOOK_FILLED, NO_BOOK, PARTIAL, BookWalkFillPricer,
)
from parts.paper_live_trading.live_switch_guard import (
    GRADUATED, LIVE, NOT_GRADUATED, NO_RECORD, PAPER, BotMaturity, LiveSwitchGuard,
)
from parts.paper_live_trading.money_mode_reader import MoneyModeReader
from parts.paper_live_trading.order_destination_router import (
    LIVE_VENUE, PAPER_BOOK, REFUSED_NO_MODE, REFUSED_UNSTAMPED, ROUTED,
    OrderDestinationRouter,
)
from parts.paper_live_trading.order_idempotency_stamper import (
    ALREADY_STAMPED, STAMPED, OrderIdempotencyStamper,
)
from parts.paper_live_trading.order_latency_simulator import (
    HELD, LIVE_NOT_DELAYED, RELEASED, OrderLatencySimulator,
)
from parts.paper_live_trading.paper_account_keeper import (
    APPLIED, REFUSED_DUPLICATE, REFUSED_INSUFFICIENT, REFUSED_LIVE_FILL, PaperAccountKeeper,
)
from parts.paper_live_trading.paper_fill_simulator import (
    ALREADY_FILLED, FILLED, HELD_IN_FLIGHT, LIMIT, MARKET, PARTIALLY_FILLED, REFUSED_FEED_JUMP,
    REFUSED_NO_PRICE, RESTING, PaperFillSimulator,
)
from parts.paper_live_trading.paper_liquidation_simulator import (
    LIQUIDATED, NOT_WATCHED, SURVIVED, PaperLiquidationSimulator,
)
from parts.paper_live_trading.stop_order_manager import (
    PLACE_NEW, REFUSED_NO_POSITION, REFUSED_WIDENING, REPLACE, StopOrderManager,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trading_types import BUY, LONG, SELL, SHORT, Fill

BLOCK_PARTS = {
    "money-mode-reader": "parts.paper_live_trading.money_mode_reader",
    "order-destination-router": "parts.paper_live_trading.order_destination_router",
    "stop-order-manager": "parts.paper_live_trading.stop_order_manager",
    "paper-fill-simulator": "parts.paper_live_trading.paper_fill_simulator",
    "paper-account-keeper": "parts.paper_live_trading.paper_account_keeper",
    "live-switch-guard": "parts.paper_live_trading.live_switch_guard",
    "book-walk-fill-pricer": "parts.paper_live_trading.book_walk_fill_pricer",
    "order-latency-simulator": "parts.paper_live_trading.order_latency_simulator",
    "paper-liquidation-simulator": "parts.paper_live_trading.paper_liquidation_simulator",
    "order-idempotency-stamper": "parts.paper_live_trading.order_idempotency_stamper",
}

SEGMENT = "futures"
VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class Mode:
    def __init__(self, mode):
        self.mode = mode


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- money-mode-reader -------------------------------------------------------

def write_mode(tmp_path, value):
    path = tmp_path / f"{SEGMENT}.toml"
    path.write_text(f'[money_mode]\nvalue = {value}\nunit = "mode"\nnote = "a test"\n')
    return path


def test_an_absent_settings_file_is_paper():
    """Real money is never a default."""
    reader = MoneyModeReader(SEGMENT, settings_path="/nonexistent/segments/futures.toml")
    mode = reader.read()
    assert mode.mode == PAPER
    assert mode.is_explicit is False


def test_exactly_live_is_live(tmp_path):
    reader = MoneyModeReader(SEGMENT, settings_path=write_mode(tmp_path, '"live"'))
    assert reader.read().is_live is True


def test_a_near_miss_is_paper_and_says_so(tmp_path):
    """A typo must not be able to start spending real money."""
    reader = MoneyModeReader(SEGMENT, settings_path=write_mode(tmp_path, '"LIVE"'))
    mode = reader.read()
    assert mode.mode == PAPER
    assert "not exactly" in mode.reason


def test_going_live_is_recorded(tmp_path):
    path = write_mode(tmp_path, '"paper"')
    reader = MoneyModeReader(SEGMENT, settings_path=path)
    reader.read()
    write_mode(tmp_path, '"live"')
    reader.read()
    assert reader.standing.transitions_to_live == 1
    assert reader.standing.went_live_at_ns is not None


# ---- live-switch-guard -------------------------------------------------------

def guard(trades=30, days=7.0, drawdown=0.2):
    return LiveSwitchGuard(
        minimum_closed_trades=trades, minimum_days_traded=days,
        maximum_drawdown_fraction=drawdown,
    )


def graduated_bot(bot_id="bull", trades=100, result=500.0, drawdown=0.1, days=30.0):
    return BotMaturity(bot_id, trades, result, drawdown, days)


def test_paper_mode_needs_no_graduation():
    assert guard().read_limit(PAPER, ("bull",)).fraction_of_allotment == 1.0


def test_live_with_an_ungraduated_bot_permits_nothing():
    """RL-005: what earns profit on paper goes live, not the other way round."""
    subject = guard()
    subject.observe_paper_maturity(graduated_bot(trades=5))
    limit = subject.read_limit(LIVE, ("bull",))
    assert limit.fraction_of_allotment == 0.0
    assert "RL-005" in limit.reason


def test_live_with_no_bots_permits_nothing():
    assert guard().read_limit(LIVE, ()).fraction_of_allotment == 0.0


def test_a_bot_with_no_paper_record_cannot_graduate():
    assert guard().judge("unknown").verdict == NO_RECORD


def test_every_test_must_pass_to_graduate():
    subject = guard(trades=30, days=7.0, drawdown=0.2)
    subject.observe_paper_maturity(graduated_bot(result=-100.0))
    assert subject.judge("bull").verdict == NOT_GRADUATED
    subject.observe_paper_maturity(graduated_bot(drawdown=0.5))
    assert subject.judge("bull").verdict == NOT_GRADUATED
    subject.observe_paper_maturity(graduated_bot())
    assert subject.judge("bull").verdict == GRADUATED


def test_one_ungraduated_bot_blocks_the_whole_segment():
    """The segment shares one allocation."""
    subject = guard()
    subject.observe_paper_maturity(graduated_bot("bull"))
    subject.observe_paper_maturity(graduated_bot("bear", trades=2))
    assert subject.read_limit(LIVE, ("bull", "bear")).fraction_of_allotment == 0.0


# ---- order-idempotency-stamper -----------------------------------------------

class BoundedStub:
    def __init__(self, quantity=1.0, entry=100.0):
        self.venue_id = VENUE
        self.symbol = SYMBOL
        self.side = BUY
        self.quantity = quantity
        self.entry_price = entry
        self.stop_price = 98.0


def test_the_same_order_stamps_the_same_id():
    stamper = OrderIdempotencyStamper()
    first = stamper.stamp(BoundedStub(), "intent-1")
    second = stamper.stamp(BoundedStub(), "intent-1")
    assert first.client_order_id == second.client_order_id


def test_two_intents_wanting_the_same_order_get_different_ids():
    stamper = OrderIdempotencyStamper()
    one = stamper.stamp(BoundedStub(), "intent-1")
    two = stamper.stamp(BoundedStub(), "intent-2")
    assert one.client_order_id != two.client_order_id


def test_an_already_stamped_order_keeps_its_id():
    """Re-stamping would break a retry at the moment it is needed."""
    stamper = OrderIdempotencyStamper()
    result = stamper.stamp(BoundedStub(), "intent-1", existing_id="original-id")
    assert result.client_order_id == "original-id"
    assert result.outcome == ALREADY_STAMPED


# ---- order-destination-router ------------------------------------------------

class StampedStub:
    def __init__(self, client_order_id="cid-1", quantity=1.0):
        self.client_order_id = client_order_id
        self.venue_id = VENUE
        self.symbol = SYMBOL
        self.side = BUY
        self.quantity = quantity
        self.entry_price = 100.0
        self.stop_price = 98.0


def test_paper_mode_routes_to_the_paper_book():
    requests = OrderDestinationRouter().route(StampedStub(), Mode("paper"))
    assert requests[0].destination == PAPER_BOOK
    assert requests[0].is_live_money is False


def test_live_mode_routes_to_the_venue():
    requests = OrderDestinationRouter().route(StampedStub(), Mode("live"))
    assert requests[0].destination == LIVE_VENUE
    assert requests[0].is_live_money is True


def test_an_unreadable_mode_routes_nowhere():
    """Paper would silently drop a live trade; live would spend on an unread setting."""
    requests = OrderDestinationRouter().route(StampedStub(), None)
    assert requests[0].outcome == REFUSED_NO_MODE
    assert requests[0].may_be_sent is False


def test_an_unstamped_order_routes_nowhere():
    requests = OrderDestinationRouter().route(StampedStub(client_order_id=""), Mode("paper"))
    assert requests[0].outcome == REFUSED_UNSTAMPED


def test_a_split_order_stays_split():
    """Reassembling it would spend exactly what the split was meant to save."""
    from parts.risk_capital_allocation.participation_capped_order_splitter import (
        ExecutionSchedule, ExecutionSlice,
    )

    schedule = ExecutionSchedule(
        VENUE, SYMBOL, BUY, 10.0,
        (ExecutionSlice(1, 5.0, 0.0, False), ExecutionSlice(2, 5.0, 10.0, True)),
        "split", 0.1, 100.0, 300.0, "", 1,
    )
    requests = OrderDestinationRouter().route(StampedStub(quantity=10.0), Mode("paper"), schedule)
    assert len(requests) == 2
    assert [r.quantity for r in requests] == [5.0, 5.0]
    assert requests[1].at_second == 10.0
    assert len({r.client_order_id for r in requests}) == 2


# ---- book-walk-fill-pricer ---------------------------------------------------

def pricer_with_book(bids=((99.0, 1.0), (98.0, 5.0)), asks=((100.0, 1.0), (101.0, 5.0))):
    pricer = BookWalkFillPricer()
    pricer.observe_book(VENUE, SYMBOL, bids, asks)
    return pricer


def test_a_small_order_pays_the_touch():
    estimate = pricer_with_book().price(VENUE, SYMBOL, BUY, 0.5)
    assert estimate.average_price == pytest.approx(100.0)
    assert estimate.levels_consumed == 1


def test_a_large_order_eats_the_levels_it_needs():
    """The largest source of fantasy in paper trading, priced properly."""
    estimate = pricer_with_book().price(VENUE, SYMBOL, BUY, 3.0)
    # 1 at 100 and 2 at 101 averages 100.667.
    assert estimate.average_price == pytest.approx((100.0 + 2 * 101.0) / 3)
    assert estimate.levels_consumed == 2
    assert estimate.slippage_fraction > 0


def test_a_sell_walks_the_bids():
    estimate = pricer_with_book().price(VENUE, SYMBOL, SELL, 3.0)
    assert estimate.average_price == pytest.approx((99.0 + 2 * 98.0) / 3)


def test_an_order_the_book_cannot_fill_is_partial_not_filled():
    """Filling the rest at the deepest level would be a fill that never happened."""
    estimate = pricer_with_book().price(VENUE, SYMBOL, BUY, 100.0)
    assert estimate.outcome == PARTIAL
    assert estimate.fillable_quantity == pytest.approx(6.0)
    assert estimate.fills_completely is False


def test_no_book_prices_nothing():
    assert BookWalkFillPricer().price(VENUE, SYMBOL, BUY, 1.0).outcome == NO_BOOK


# ---- order-latency-simulator -------------------------------------------------

def latency(clock, prior=0.05, minimum=3):
    return OrderLatencySimulator(
        prior_latency_seconds=prior, maximum_latency_seconds=5.0,
        minimum_observations=minimum, window=50, monotonic=clock.monotonic,
        draw=lambda: 0.5,
    )


def test_a_paper_order_is_held_for_a_round_trip():
    """A paper order that fills instantly is trading on the future."""
    clock = Clock()
    subject = latency(clock, prior=0.05)
    held = subject.hold("cid-1", VENUE, SYMBOL, "paper")
    assert held.state == HELD
    assert held.may_fill_now is False
    assert subject.released_orders() == ()


def test_the_order_is_released_once_the_round_trip_elapses():
    clock = Clock()
    subject = latency(clock, prior=0.05)
    subject.hold("cid-1", VENUE, SYMBOL, "paper")
    clock.now += 0.06
    released = subject.released_orders()
    assert len(released) == 1 and released[0].state == RELEASED


def test_a_live_order_is_not_delayed_by_this_part():
    subject = latency(Clock())
    assert subject.hold("cid-1", VENUE, SYMBOL, "live").state == LIVE_NOT_DELAYED


def test_the_delay_follows_measured_round_trips():
    clock = Clock()
    subject = latency(clock, prior=0.001, minimum=3)
    for _ in range(5):
        subject.observe_live_round_trip(VENUE, 0.4)
    held = subject.hold("cid-1", VENUE, SYMBOL, "paper")
    assert held.latency_estimate.is_fitted is True
    assert held.delay_seconds == pytest.approx(0.4)


# ---- paper-fill-simulator ----------------------------------------------------

def fill_simulator(taker=0.0004, maker=0.0002):
    return PaperFillSimulator(taker_fee_rate=taker, maker_fee_rate=maker)


class Estimate:
    def __init__(self, average_price, fillable_quantity, slippage=0.001):
        self.average_price = average_price
        self.fillable_quantity = fillable_quantity
        self.slippage_fraction = slippage


def an_order(**overrides):
    order = dict(
        client_order_id="cid-1", venue_id=VENUE, symbol=SYMBOL, side=BUY, quantity=1.0,
        order_type=MARKET, limit_price=None, money_mode="paper", is_in_flight=False,
        fill_price_estimate=Estimate(100.5, 1.0), market_price=100.0,
    )
    order.update(overrides)
    return order


def test_a_paper_market_order_fills_at_the_walked_price_with_a_fee():
    result = fill_simulator().simulate(**an_order())
    assert result.outcome == FILLED
    assert result.fill.price == pytest.approx(100.5)
    assert result.fill.fee > 0
    assert result.fill.is_paper is True


def test_an_order_still_in_flight_does_not_fill():
    result = fill_simulator().simulate(**an_order(is_in_flight=True))
    assert result.outcome == HELD_IN_FLIGHT
    assert result.fill is None


def test_a_limit_the_market_never_reached_rests():
    result = fill_simulator().simulate(
        **an_order(order_type=LIMIT, limit_price=99.0, fill_price_estimate=Estimate(100.5, 1.0))
    )
    assert result.outcome == RESTING
    assert result.fill is None


def test_a_limit_the_market_came_to_fills_as_a_maker():
    subject = fill_simulator(taker=0.001, maker=0.0)
    result = subject.simulate(
        **an_order(order_type=LIMIT, limit_price=101.0, fill_price_estimate=Estimate(100.5, 1.0))
    )
    assert result.outcome == FILLED
    assert result.fill.fee == pytest.approx(0.0)
    assert result.fill.price == pytest.approx(101.0)


def test_nothing_fills_across_a_feed_jump():
    """Filling there manufactures profit from a data artefact."""
    subject = fill_simulator()
    subject.observe_feed_jump(VENUE, SYMBOL)
    assert subject.simulate(**an_order()).outcome == REFUSED_FEED_JUMP


def test_an_order_larger_than_the_book_fills_partially():
    result = fill_simulator().simulate(
        **an_order(quantity=5.0, fill_price_estimate=Estimate(100.5, 2.0))
    )
    assert result.outcome == PARTIALLY_FILLED
    assert result.filled_quantity == pytest.approx(2.0)
    assert result.remaining_quantity == pytest.approx(3.0)


def test_an_order_id_already_filled_is_not_filled_again():
    """The paper book holds one order per client id, the way a venue does.

    `order-idempotency-stamper` derives a stable id precisely so that a retry is
    the same order -- "a venue that already has it rejects the duplicate rather
    than opening a second position". The simulator counted what it had filled and
    then filled it again anyway, so on paper the retry opened the second position
    the id exists to prevent, and paper and live diverged exactly where it costs.
    """
    subject = fill_simulator()
    first = subject.simulate(**an_order(quantity=2.0, fill_price_estimate=Estimate(100.5, 5.0)))
    assert first.outcome == FILLED
    assert first.filled_quantity == pytest.approx(2.0)

    again = subject.simulate(**an_order(quantity=2.0, fill_price_estimate=Estimate(100.5, 5.0)))
    assert again.outcome == ALREADY_FILLED
    assert again.fill is None
    assert again.filled_quantity == 0.0
    assert "already been filled" in again.reason


def test_a_partially_filled_order_fills_only_what_is_left():
    """The remainder, not the whole order again."""
    subject = fill_simulator()
    first = subject.simulate(**an_order(quantity=5.0, fill_price_estimate=Estimate(100.5, 2.0)))
    assert first.outcome == PARTIALLY_FILLED
    assert first.filled_quantity == pytest.approx(2.0)

    rest = subject.simulate(**an_order(quantity=5.0, fill_price_estimate=Estimate(100.5, 10.0)))
    assert rest.outcome == FILLED
    assert rest.filled_quantity == pytest.approx(3.0), (
        "the whole order filled a second time, so the position is 7 of an order for 5"
    )
    assert rest.remaining_quantity == pytest.approx(0.0)


def test_no_price_at_all_fills_nothing():
    result = fill_simulator().simulate(
        **an_order(fill_price_estimate=None, market_price=None)
    )
    assert result.outcome == REFUSED_NO_PRICE


# ---- paper-account-keeper ----------------------------------------------------

def keeper(allotted=10_000.0):
    subject = PaperAccountKeeper(SEGMENT)
    subject.set_allotment(allotted)
    return subject


def paper_fill(fill_id, side, price, quantity, fee=0.0, is_paper=True):
    return Fill(fill_id, VENUE, SYMBOL, side, price, quantity, fee, 1, "o1", is_paper)


def test_the_paper_account_starts_at_the_allotment():
    assert keeper(10_000.0).read_balance().cash == pytest.approx(10_000.0)


def test_a_buy_moves_cash_into_a_position_and_equity_holds():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, fee=1.0))
    subject.observe_mark_price(VENUE, SYMBOL, 100.0)
    balance = subject.read_balance()
    assert balance.cash == pytest.approx(10_000.0 - 1000.0 - 1.0)
    assert balance.equity == pytest.approx(10_000.0 - 1.0)
    assert balance.open_positions == 1


def test_an_unrealised_loss_shows_in_equity_but_not_in_cash():
    """A simulator with one number would let a losing position hide."""
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0))
    subject.observe_mark_price(VENUE, SYMBOL, 90.0)
    balance = subject.read_balance()
    assert balance.cash == pytest.approx(9000.0)
    assert balance.unrealised == pytest.approx(-100.0)
    assert balance.equity == pytest.approx(9900.0)


def test_closing_realises_the_result():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0))
    subject.apply_fill(paper_fill("f2", SELL, 110.0, 10.0))
    balance = subject.read_balance()
    assert balance.realised_total == pytest.approx(100.0)
    assert balance.cash == pytest.approx(10_100.0)
    assert balance.open_positions == 0


def test_a_live_fill_never_touches_the_paper_account():
    subject = keeper()
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 1.0, is_paper=False)) == REFUSED_LIVE_FILL


def test_the_paper_account_cannot_spend_what_it_does_not_have():
    """A venue would have refused it, and the paper record must show the same."""
    subject = keeper(100.0)
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0)) == REFUSED_INSUFFICIENT
    assert subject.read_balance().cash == pytest.approx(100.0)


def test_a_repeated_fill_is_applied_once():
    subject = keeper()
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 1.0))
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 1.0)) == REFUSED_DUPLICATE


# ---- stop-order-manager ------------------------------------------------------

def test_a_stop_becomes_a_real_resting_order():
    """A stop that exists only as a number in this system is not a stop."""
    manager = StopOrderManager()
    action = manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 98.0, Mode("paper"))
    assert action.action == PLACE_NEW
    assert action.side == SELL
    assert action.place_order_id is not None
    assert manager.resting_stop(VENUE, SYMBOL) == 98.0


def test_tightening_replaces_by_placing_before_cancelling():
    """No stop briefly is an unbounded loss; two stops briefly is recoverable."""
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 98.0, Mode("paper"))
    action = manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 99.0, Mode("paper"))
    assert action.action == REPLACE
    assert action.place_order_id is not None and action.cancel_order_id is not None
    assert "before the old one is cancelled" in action.reason


def test_a_stop_is_never_widened():
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 99.0, Mode("paper"))
    action = manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 95.0, Mode("paper"))
    assert action.action == REFUSED_WIDENING
    assert manager.resting_stop(VENUE, SYMBOL) == 99.0


def test_a_short_stop_is_a_buy_above_the_market():
    manager = StopOrderManager()
    action = manager.apply_adjustment(VENUE, SYMBOL, SHORT, 1.0, 102.0, Mode("paper"))
    assert action.side == BUY
    tightened = manager.apply_adjustment(VENUE, SYMBOL, SHORT, 1.0, 101.0, Mode("paper"))
    assert tightened.action == REPLACE


def test_no_position_means_no_stop_order():
    manager = StopOrderManager()
    assert manager.apply_adjustment(VENUE, SYMBOL, LONG, 0.0, 98.0, Mode("paper")).action == REFUSED_NO_POSITION


# ---- paper-liquidation-simulator ---------------------------------------------

def liquidator(fee=0.005, slippage=0.005):
    return PaperLiquidationSimulator(liquidation_fee_rate=fee, bankruptcy_slippage_fraction=slippage)


def test_a_wick_through_the_liquidation_price_liquidates():
    """A candle that wicked through and closed above still liquidated the position."""
    subject = liquidator()
    subject.watch_position(VENUE, SYMBOL, LONG, 1.0, 100.0, liquidation_price=90.0)
    result = subject.check_interval(VENUE, SYMBOL, high_price=101.0, low_price=89.0, money_mode="paper")
    assert result.outcome == LIQUIDATED
    assert result.fill is not None and result.fill.side == SELL


def test_the_close_is_at_the_bankruptcy_price_not_the_trigger():
    """A forced market order does not get the trigger price."""
    subject = liquidator(slippage=0.01)
    subject.watch_position(VENUE, SYMBOL, LONG, 1.0, 100.0, liquidation_price=90.0)
    result = subject.check_interval(VENUE, SYMBOL, 101.0, 89.0, "paper")
    assert result.bankruptcy_price == pytest.approx(89.1)
    assert result.fill.price == pytest.approx(89.1)
    assert result.liquidation_fee > 0


def test_a_position_that_never_reached_its_liquidation_survives():
    subject = liquidator()
    subject.watch_position(VENUE, SYMBOL, LONG, 1.0, 100.0, liquidation_price=90.0)
    assert subject.check_interval(VENUE, SYMBOL, 101.0, 95.0, "paper").outcome == SURVIVED


def test_a_short_is_liquidated_by_the_high_not_the_low():
    subject = liquidator()
    subject.watch_position(VENUE, SYMBOL, SHORT, 1.0, 100.0, liquidation_price=110.0)
    assert subject.check_interval(VENUE, SYMBOL, 111.0, 99.0, "paper").outcome == LIQUIDATED


def test_a_position_with_no_liquidation_price_is_not_assumed_safe():
    subject = liquidator()
    subject.watch_position(VENUE, SYMBOL, LONG, 1.0, 100.0, liquidation_price=None)
    result = subject.check_interval(VENUE, SYMBOL, 101.0, 1.0, "paper")
    assert result.outcome == NOT_WATCHED
    assert "must not be assumed safe" in result.reason


def test_a_liquidated_position_is_gone():
    subject = liquidator()
    subject.watch_position(VENUE, SYMBOL, LONG, 1.0, 100.0, liquidation_price=90.0)
    subject.check_interval(VENUE, SYMBOL, 101.0, 89.0, "paper")
    assert subject.check_interval(VENUE, SYMBOL, 101.0, 89.0, "paper").outcome == NOT_WATCHED


# ---- the block together ------------------------------------------------------

def test_a_paper_trade_end_to_end_costs_what_it_should():
    """Stamp, route, price against the book, delay, fill, and apply to the account.

    The point of the block: the paper account ends up down by the spread it
    walked and the fees it paid, not level as a naive simulator would leave it.
    """
    clock = Clock()
    stamper = OrderIdempotencyStamper()
    router = OrderDestinationRouter()
    pricer = BookWalkFillPricer()
    delayer = OrderLatencySimulator(
        prior_latency_seconds=0.05, maximum_latency_seconds=1.0,
        minimum_observations=3, window=10, monotonic=clock.monotonic, draw=lambda: 0.5,
    )
    filler = PaperFillSimulator(taker_fee_rate=0.0004, maker_fee_rate=0.0002)
    account = keeper(10_000.0)

    stamped = stamper.stamp(BoundedStub(quantity=3.0), "intent-1")
    request = router.route(stamped, Mode("paper"))[0]
    assert request.destination == PAPER_BOOK

    pricer.observe_book(VENUE, SYMBOL, ((99.0, 10.0),), ((100.0, 1.0), (101.0, 5.0)))
    estimate = pricer.price(VENUE, SYMBOL, BUY, 3.0)
    assert estimate.average_price > 100.0, "the order walked past the touch"

    held = delayer.hold(request.client_order_id, VENUE, SYMBOL, "paper")
    assert held.state == HELD
    assert filler.simulate(
        client_order_id=request.client_order_id, venue_id=VENUE, symbol=SYMBOL, side=BUY,
        quantity=3.0, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=True, fill_price_estimate=estimate,
    ).fill is None

    clock.now += 0.06
    assert len(delayer.released_orders()) == 1

    result = filler.simulate(
        client_order_id=request.client_order_id, venue_id=VENUE, symbol=SYMBOL, side=BUY,
        quantity=3.0, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=False, fill_price_estimate=estimate,
    )
    assert result.outcome == FILLED
    assert account.apply_fill(result.fill) == APPLIED

    account.observe_mark_price(VENUE, SYMBOL, 100.0)
    balance = account.read_balance()
    # Bought above the touch and paid a fee, so equity is below where it started
    # the instant the trade is done -- which is the truth a naive simulator hides.
    assert balance.equity < 10_000.0
    assert balance.fees_total > 0
