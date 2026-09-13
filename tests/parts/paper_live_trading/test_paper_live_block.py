"""Paper trading: every way a simulator lies, and what stops each one.

The value of this whole block is that paper results predict live ones. Each test
below corresponds to a specific way that prediction breaks -- filling at the touch,
filling instantly, ignoring fees, surviving a liquidation, spending money the
account does not have -- because a simulator with any one of them produces a
strategy that works on paper and loses money live.
"""

import datetime
import importlib
import json
import pathlib

import pytest

from runtime.market_conditions import MarketSessionState, SessionKind
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
    APPLIED, APPLIED_BEYOND_CASH, REFUSED_DUPLICATE, REFUSED_LIVE_FILL, PaperAccountKeeper,
)
from parts.paper_live_trading.paper_fill_simulator import (
    ALREADY_FILLED, CANCELLED, FILLED, HELD_IN_FLIGHT, LIMIT, MARKET, PARTIALLY_FILLED,
    REFUSED_FEED_JUMP, REFUSED_NO_PRICE, RESTING, RESTING_STOP, RESTING_UNPRICED, STOP_MARKET,
    TAKE_PROFIT_MARKET, PaperFillSimulator,
)
from parts.paper_live_trading.paper_liquidation_simulator import (
    LIQUIDATED, NOT_WATCHED, SURVIVED, PaperLiquidationSimulator,
)
from parts.paper_live_trading.stop_order_manager import (
    PLACE_NEW, REFUSED_NO_POSITION, REFUSED_WIDENING, REPLACE, RESIZE, RESIZE_TARGET,
    StopOrderManager,
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


def graduated_bot(bot_id="bull", trades=100, result=500.0, drawdown=0.1, days=30.0,
                  mature_in=("trending",)):
    # `mature_in` carries the fifth test, added 2026-08-25: a bot whose edge has
    # graduated in no regime has an account record and no reason for it.
    return BotMaturity(bot_id, trades, result, drawdown, days, mature_in)


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
    def __init__(self, quantity=1.0, entry=100.0, leverage=1.0):
        self.venue_id = VENUE
        self.symbol = SYMBOL
        self.side = BUY
        self.quantity = quantity
        self.entry_price = entry
        self.stop_price = 98.0
        self.leverage = leverage


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

# Upstox's real Equity Options charge stack (runtime/indian_options_fee_model.py's
# own docstring carries the sourced quotes). Every test in this block trades a
# crypto venue symbol, so these never actually apply to a fill here -- passed
# because PaperFillSimulator requires every fee-model rate explicitly, the
# same as taker/maker below, rather than defaulting to a number nobody chose.
OPTIONS_FLAT_BROKERAGE = 20.0
OPTIONS_STT_SELL_RATE = 0.001
OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE = 0.0003503
OPTIONS_IPFT_CHARGE_RATE = 0.000005
OPTIONS_STAMP_DUTY_BUY_RATE = 0.00003
OPTIONS_GST_RATE = 0.18


OPEN_SESSION = MarketSessionState(
    segment="FO", kind=SessionKind.OPEN, as_of_date=datetime.date(2026, 9, 2),
    reason="within stated session hours", observed_at_ns=1_756_800_000_000_000_000,
)


def fill_simulator(taker=0.0004, maker=0.0002, session=OPEN_SESSION):
    """A simulator that has been told the market is open.

    The session is explicit because a simulator that has never seen one does
    not fill at all (2026-09-02): an unmeasured session is not an open one, and
    filling off it is how a paper account trades on a holiday. Tests about a
    closed market pass their own session, or None.
    """
    simulator = PaperFillSimulator(
        taker_fee_rate=taker, maker_fee_rate=maker,
        options_flat_brokerage=OPTIONS_FLAT_BROKERAGE,
        options_stt_sell_rate=OPTIONS_STT_SELL_RATE,
        options_exchange_transaction_charge_rate=OPTIONS_EXCHANGE_TRANSACTION_CHARGE_RATE,
        options_ipft_charge_rate=OPTIONS_IPFT_CHARGE_RATE,
        options_stamp_duty_buy_rate=OPTIONS_STAMP_DUTY_BUY_RATE,
        options_gst_rate=OPTIONS_GST_RATE,
    )
    if session is not None:
        simulator.observe_session(session)
    return simulator


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


def test_a_symbol_fills_again_once_its_prices_are_continuous():
    """The bar has to lift, or the refusal is permanent.

    `clear_feed_jump` existed with no caller anywhere in the repository until
    2026-09-04, so `_jumped_symbols` only ever grew: 993 of the 1,474 streams on
    that day's tape cross the jump threshold at least once, and each one was
    unfillable for the life of the process. 54 of the 111 orders this part had
    ever seen were refused for a jump and none had ever filled.
    """
    subject = fill_simulator()
    subject.observe_feed_jump(VENUE, SYMBOL)
    assert subject.simulate(**an_order()).outcome == REFUSED_FEED_JUMP

    subject.clear_feed_jump(VENUE, SYMBOL)
    assert subject.standing.feed_jumps_cleared == 1
    assert subject.simulate(**an_order()).outcome == FILLED


def test_clearing_a_symbol_that_was_never_barred_is_not_counted_as_a_release():
    """Otherwise the counter that proves the bar lifts would climb on every
    continuous bar of every symbol, and prove nothing."""
    subject = fill_simulator()
    subject.clear_feed_jump(VENUE, SYMBOL)
    assert subject.standing.feed_jumps_cleared == 0


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


def test_no_price_at_all_rests_a_market_order_instead_of_refusing_it():
    """Operator, 2026-08-30: no price yet is a reason to wait, not to discard the order."""
    subject = fill_simulator()
    result = subject.simulate(**an_order(fill_price_estimate=None, market_price=None))
    assert result.outcome == RESTING_UNPRICED
    assert result.fill is None
    assert subject.standing.market_orders_waiting_for_a_first_price == 1
    assert subject.standing.refused_no_price == 0


# ---- paper-account-keeper ----------------------------------------------------

def keeper(allotted=10_000.0):
    subject = PaperAccountKeeper(SEGMENT)
    subject.set_allotment(allotted)
    return subject


def paper_fill(fill_id, side, price, quantity, fee=0.0, is_paper=True, leverage=1.0):
    return Fill(
        fill_id, VENUE, SYMBOL, side, price, quantity, fee, 1, "o1", is_paper, leverage
    )


# ---- what a position ties up ------------------------------------------------
#
# The operator raised `leverage_ceiling` from 1 to 10 at 12:42 on 2026-08-26 and
# nothing changed: `trade-capital-bounds-gate` measured its bound against the
# notional, so the three trades that opened afterwards committed 99.47, 100.17 and
# 100.09 USDT against a 100 maximum, exactly as they had at 1x. The gate reads the
# commitment now -- notional over leverage -- and this account has to agree with
# it, or it refuses for want of cash the very trades the operator's bounds passed.


def test_a_levered_fill_ties_up_its_notional_over_its_leverage():
    subject = keeper(10_000.0)
    # 10 at 100 is 1,000 of notional; at 10x it posts 100 of margin.
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, leverage=10.0)) == APPLIED

    balance = subject.read_balance()
    assert balance.cash == pytest.approx(9_900.0), "the account paid the whole notional"
    subject.observe_mark_price(VENUE, SYMBOL, 100.0)
    assert subject.read_balance().equity == pytest.approx(10_000.0), (
        "equity grew with leverage, and every risk cap is a fraction of it"
    )


def test_the_same_fill_unlevered_ties_up_the_whole_notional():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, leverage=1.0))
    assert subject.read_balance().cash == pytest.approx(9_000.0)


def test_closing_returns_the_margin_that_was_posted_not_the_notional():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, leverage=10.0))
    subject.apply_fill(paper_fill("f2", SELL, 110.0, 10.0))

    balance = subject.read_balance()
    assert balance.open_positions == 0
    # 100 of margin back, plus the 100 the move made on 1,000 of notional.
    assert balance.realised_total == pytest.approx(100.0)
    assert balance.cash == pytest.approx(10_100.0)


def test_half_a_levered_position_returns_half_its_margin():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, leverage=10.0))
    subject.apply_fill(paper_fill("f2", SELL, 100.0, 5.0))

    balance = subject.read_balance()
    assert balance.open_positions == 1
    assert balance.cash == pytest.approx(9_950.0)
    subject.observe_mark_price(VENUE, SYMBOL, 100.0)
    assert subject.read_balance().equity == pytest.approx(10_000.0)


def test_a_levered_trade_the_account_could_not_afford_unlevered_is_admitted():
    """The operator's ceiling has to reach the account, or it reaches nothing."""
    subject = keeper(200.0)
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, leverage=10.0)) == APPLIED
    assert subject.read_balance().cash == pytest.approx(100.0)
    assert subject.standing.fills_applied_beyond_cash == 0


def test_a_short_posts_margin_rather_than_raising_the_account_s_cash():
    """A perpetual short does not hand the account the sale proceeds.

    It did until 2026-08-26: opening a short *raised* free cash by the whole
    notional, so a segment could short its way to a larger equity and every
    exposure cap computed as a fraction of that equity grew with it.
    """
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", SELL, 100.0, 10.0, leverage=10.0))

    balance = subject.read_balance()
    assert balance.cash == pytest.approx(9_900.0)
    assert balance.open_positions == 1
    subject.observe_mark_price(VENUE, SYMBOL, 90.0)
    reread = subject.read_balance()
    assert reread.unrealised == pytest.approx(100.0), "a short did not gain as the price fell"
    assert reread.equity == pytest.approx(10_100.0)

    subject.apply_fill(paper_fill("f2", BUY, 90.0, 10.0))
    closed = subject.read_balance()
    assert closed.open_positions == 0
    assert closed.realised_total == pytest.approx(100.0)
    assert closed.cash == pytest.approx(10_100.0)


def test_a_checkpoint_written_before_margin_was_tracked_restores_unlevered():
    """Its cash figure was written by an account that had paid the notional."""
    before_margin_existed = {
        "starting": 10_000.0, "cash": 9_000.0, "realised_total": 0.0, "fees_total": 0.0,
        "fills_applied": 1,
        "positions": {f"{VENUE}|{SYMBOL}": {"quantity": 10.0, "average_price": 100.0}},
        "seen_fills": ["f1"],
    }
    subject = PaperAccountKeeper(SEGMENT)
    assert subject.restore_from_checkpoint(before_margin_existed) == 1
    subject.observe_mark_price(VENUE, SYMBOL, 100.0)
    assert subject.read_balance().equity == pytest.approx(10_000.0)

    subject.apply_fill(paper_fill("f2", SELL, 100.0, 10.0))
    assert subject.read_balance().cash == pytest.approx(10_000.0), (
        "the restored position handed back money that never left the account"
    )


# ---- the account survives a restart ------------------------------------------
#
# Measured on the live spine at 16:10 on 2026-08-26: eight positions open,
# restored by fill-reconciler and cost-basis-tracker from their own checkpoints,
# and this part reporting `fills_applied` 0, `open_positions` 0 and equity exactly
# the 10,000 starting balance. The paper account was the one part of the book that
# forgot, and every risk cap in the segment is a fraction of its equity -- so a
# forgetting account is a segment whose caps are fiction.


def test_the_account_comes_back_holding_what_it_held():
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, fee=1.0))
    subject.observe_mark_price(VENUE, SYMBOL, 110.0)
    before = subject.read_balance()

    restored = PaperAccountKeeper(SEGMENT)
    assert restored.restore_from_checkpoint(subject.read_checkpoint_state()) == 1
    # The allotment arrives again on the next tick, as it does on every run, and
    # must not hand the account a second 10,000.
    restored.set_allotment(10_000.0)
    restored.observe_mark_price(VENUE, SYMBOL, 110.0)
    after = restored.read_balance()

    assert after.cash == pytest.approx(before.cash)
    assert after.equity == pytest.approx(before.equity)
    assert after.open_positions == 1
    assert after.unrealised == pytest.approx(100.0)


def test_a_restored_account_does_not_spend_its_cash_twice():
    """A fill redelivered across a restart is still the same fill."""
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, fee=1.0))

    restored = PaperAccountKeeper(SEGMENT)
    restored.restore_from_checkpoint(subject.read_checkpoint_state())
    assert restored.apply_fill(paper_fill("f1", BUY, 100.0, 10.0, fee=1.0)) == REFUSED_DUPLICATE
    assert restored.read_balance().cash == pytest.approx(subject.read_balance().cash)


def test_a_restored_account_closes_what_it_restored():
    """A position that came back must be closable, not a second position."""
    subject = keeper(10_000.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0))

    restored = PaperAccountKeeper(SEGMENT)
    restored.restore_from_checkpoint(subject.read_checkpoint_state())
    restored.set_allotment(10_000.0)
    restored.apply_fill(paper_fill("f2", SELL, 110.0, 10.0))
    balance = restored.read_balance()

    assert balance.open_positions == 0
    assert balance.realised_total == pytest.approx(100.0)
    assert balance.cash == pytest.approx(10_100.0)


def test_an_account_that_never_ran_restores_to_nothing_held():
    """Never run and came back empty are different facts, and both are honest."""
    subject = PaperAccountKeeper(SEGMENT)
    assert subject.restore_from_checkpoint(subject.read_checkpoint_state()) == 0
    assert subject.read_balance().cash == pytest.approx(0.0)


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


def test_an_executed_fill_the_cash_did_not_cover_is_applied_and_counted():
    """A venue refuses the order, not the fill after it executed (2026-09-13).

    Refusing here after execution made this account disagree with every other
    book: on 2026-09-07 it refused 114 executed fills the lot books had applied.
    """
    subject = keeper(100.0)
    assert subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0)) == APPLIED_BEYOND_CASH
    balance = subject.read_balance()
    assert balance.open_positions == 1
    assert balance.cash == pytest.approx(-900.0)
    assert subject.standing.fills_applied == 1
    assert subject.standing.fills_applied_beyond_cash == 1
    assert subject.standing.cash_shortfall_total == pytest.approx(900.0)


def test_closing_a_position_the_cash_did_not_cover_returns_it_to_flat_not_short():
    """The refused buy is what made the later sell a short in this account alone."""
    subject = keeper(100.0)
    subject.apply_fill(paper_fill("f1", BUY, 100.0, 10.0))
    assert subject.apply_fill(paper_fill("f2", SELL, 100.0, 10.0)) == APPLIED
    assert subject.read_balance().open_positions == 0
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
    filler = fill_simulator()
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


def test_the_leverage_the_desk_sized_at_survives_every_hop_to_the_account():
    """Stamped, routed, filled, paid for -- and the number has to survive all four.

    A fill states a price and a quantity, and those are identical at 1x and at
    10x. Every step between `trade-capital-bounds-gate`, which admits a trade on
    what it commits, and the account, which pays for it, has to carry the leverage
    or the account pays the whole notional for a position the gate measured as a
    tenth of it -- and the gate's bound then admits trades the account refuses.
    """
    stamper = OrderIdempotencyStamper()
    router = OrderDestinationRouter()
    filler = fill_simulator()
    account = keeper(10_000.0)

    stamped = stamper.stamp(BoundedStub(quantity=10.0, leverage=10.0), "intent-levered")
    assert stamped.leverage == 10.0, "the stamper dropped it"

    request = router.route(stamped, Mode("paper"))[0]
    assert request.leverage == 10.0, "the router dropped it"

    result = filler.simulate(
        client_order_id=request.client_order_id, venue_id=VENUE, symbol=SYMBOL, side=BUY,
        quantity=request.quantity, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=False, fill_price_estimate=None, market_price=100.0,
        leverage=request.leverage,
    )
    assert result.outcome == FILLED
    assert result.fill.leverage == 10.0, "the book dropped it"

    assert account.apply_fill(result.fill) == APPLIED
    # 10 at 100 is 1,000 of notional, and at 10x that is 100 of margin plus the fee.
    assert account.read_balance().cash == pytest.approx(10_000.0 - 100.0 - result.fill.fee)


def test_an_exit_is_unlevered_and_still_closes_a_levered_position():
    """Nothing states a leverage on the way out, and nothing needs to.

    What comes back is the margin the position posted, which the account already
    knows -- so an exit carrying the default is not a lost number.
    """
    filler = fill_simulator(taker=0.0, maker=0.0)
    account = keeper(10_000.0)
    entry = filler.simulate(
        client_order_id="entry-1", venue_id=VENUE, symbol=SYMBOL, side=BUY, quantity=10.0,
        order_type=MARKET, limit_price=None, money_mode="paper", is_in_flight=False,
        fill_price_estimate=None, market_price=100.0, leverage=10.0,
    )
    account.apply_fill(entry.fill)

    exit_result = filler.simulate(
        client_order_id="exit-1", venue_id=VENUE, symbol=SYMBOL, side=SELL, quantity=10.0,
        order_type=MARKET, limit_price=None, money_mode="paper", is_in_flight=False,
        fill_price_estimate=None, market_price=110.0,
    )
    assert exit_result.fill.leverage == 1.0
    assert account.apply_fill(exit_result.fill) == APPLIED

    balance = account.read_balance()
    assert balance.open_positions == 0
    assert balance.cash == pytest.approx(10_100.0), (
        "the exit returned its own notional rather than the margin the entry posted"
    )


# ---- the paper book: orders that wait, and the position that closes ----------
#
# The defect these cover was measured, not imagined. Before the book existed, an
# order the market had not reached was reported RESTING once and then discarded,
# so nothing could ever be waiting when the market came to it -- and a stop is an
# order whose entire purpose is to wait. On the live run of 2026-08-22 17:37-18:42
# no entry filled at all, because every entry was routed as a limit at the price
# the decision was made at and dropped the moment the market moved off it.
#
# The prices below are the real BTCUSDT trades captured on 2026-08-22 (RL-063).
# A stop tested against a made-up series proves the comparison operator works; a
# stop tested against a real one proves it fires where a real move would fire it.

@pytest.fixture(scope="module")
def real_btc_prices(read_captured_payloads):
    import json

    prices = []
    for _, payload in read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl"):
        message = json.loads(payload)
        if message.get("e") == "aggTrade":
            prices.append(float(message["p"]))
    assert len(prices) >= 500, f"only {len(prices)} real trades were read"
    return prices


def a_stop(**overrides):
    order = dict(
        client_order_id="stop-1", venue_id=VENUE, symbol=SYMBOL, side=SELL, quantity=1.0,
        order_type=STOP_MARKET, limit_price=None, money_mode="paper", is_in_flight=False,
        fill_price_estimate=None, market_price=None, stop_price=99.0,
    )
    order.update(overrides)
    return order


def test_a_sell_stop_rests_until_a_live_price_falls_through_it(real_btc_prices):
    """The whole close path in one test, on prices BTCUSDT actually traded at."""
    subject = fill_simulator()
    entry = real_btc_prices[0]
    lowest = min(real_btc_prices)
    # Halfway into the fall this run actually made. A stop at a round fraction
    # would be outside the 0.005% the captured 28 seconds covered and would never
    # trigger -- which would make the test pass by never testing anything.
    stop_price = entry - (entry - lowest) / 2
    assert lowest < stop_price < entry, (
        f"the captured run did not move ({entry} to {lowest}); this test would prove nothing"
    )

    rested = subject.simulate(**a_stop(stop_price=stop_price, market_price=entry))
    assert rested.outcome == RESTING_STOP
    assert rested.fill is None
    assert subject.standing.orders_on_the_book == 1

    fills = []
    for price in real_btc_prices:
        fills.extend(subject.evaluate_resting({(VENUE, SYMBOL): price}))
        if fills:
            break

    assert len(fills) == 1, "a stop must fill exactly once"
    assert fills[0].outcome == FILLED
    assert fills[0].fill.side == SELL
    assert fills[0].fill.price <= stop_price, (
        "a stop fills at the price the trigger found, which is at or through the stop"
    )
    assert subject.standing.orders_on_the_book == 0, "a filled stop leaves the book"
    assert subject.standing.stops_triggered == 1


def test_a_take_profit_triggers_above_the_market_and_a_stop_below_it():
    """Same side, same mechanism, opposite directions.

    Getting this backwards is not a small error: a sell take-profit that tested
    downwards would fire the instant the trade went against it, closing every
    loser at a profit price it never reached.
    """
    subject = fill_simulator()
    assert subject.is_triggered(STOP_MARKET, SELL, 99.0, 98.0) is True
    assert subject.is_triggered(STOP_MARKET, SELL, 99.0, 100.0) is False
    assert subject.is_triggered(TAKE_PROFIT_MARKET, SELL, 105.0, 106.0) is True
    assert subject.is_triggered(TAKE_PROFIT_MARKET, SELL, 105.0, 104.0) is False
    # The mirror, for a short being closed by a buy.
    assert subject.is_triggered(STOP_MARKET, BUY, 101.0, 102.0) is True
    assert subject.is_triggered(TAKE_PROFIT_MARKET, BUY, 95.0, 94.0) is True


def test_a_triggered_order_fills_at_the_market_not_at_its_trigger():
    """The slippage a real stop pays, which a paper book must not hide.

    A book that filled at the stop price would report a loss smaller than the one
    the strategy actually takes -- and that error is invisible until real money is
    behind it.
    """
    subject = fill_simulator()
    subject.simulate(**a_stop(stop_price=99.0, market_price=100.0))
    fills = subject.evaluate_resting({(VENUE, SYMBOL): 97.5})
    assert len(fills) == 1
    assert fills[0].fill.price == pytest.approx(97.5)
    assert fills[0].fill.fee > 0, "a triggered stop is a taker"


def test_a_stop_already_through_its_trigger_fills_on_arrival():
    """A venue does not rest an order the market has already passed."""
    subject = fill_simulator()
    result = subject.simulate(**a_stop(stop_price=99.0, market_price=98.0))
    assert result.outcome == FILLED
    assert subject.standing.orders_on_the_book == 0


def test_a_resting_order_is_not_duplicated_by_a_repeated_message():
    """Two stops on one position close twice the position that exists."""
    subject = fill_simulator()
    subject.simulate(**a_stop())
    subject.simulate(**a_stop())
    assert subject.standing.orders_on_the_book == 1


def test_the_other_exit_is_withdrawn_when_one_of_them_fills():
    """A stop left resting on a closed position opens the opposite position.

    This is the failure that makes an unmanaged paper book worse than no book:
    the trade closes at its target, the stop stays, price falls back through it,
    and the account is now short something nobody decided to be short.
    """
    subject = fill_simulator()
    subject.simulate(**a_stop(client_order_id="stop-1", stop_price=99.0, market_price=100.0))
    subject.simulate(**a_stop(
        client_order_id="target-1", order_type=TAKE_PROFIT_MARKET,
        stop_price=105.0, market_price=100.0,
    ))
    assert subject.standing.orders_on_the_book == 2

    filled = subject.evaluate_resting({(VENUE, SYMBOL): 106.0})
    assert len(filled) == 1 and filled[0].client_order_id == "target-1"

    withdrawn = subject.cancel("stop-1", "the position closed on its target")
    assert withdrawn is not None
    assert withdrawn.outcome == CANCELLED
    assert subject.standing.orders_on_the_book == 0


def test_a_cancel_for_an_order_the_book_does_not_hold_is_counted_not_crashed():
    subject = fill_simulator()
    assert subject.cancel("never-placed", "tidying up") is None
    assert subject.standing.cancels_for_an_unknown_order == 1


def test_a_replacement_withdraws_the_order_it_replaces():
    """Cancel-replace is one instruction; leaving both is two stops on one position."""
    subject = fill_simulator()
    subject.simulate(**a_stop(client_order_id="stop-1", stop_price=99.0, market_price=100.0))
    subject.simulate(**a_stop(
        client_order_id="stop-2", stop_price=99.5, market_price=100.0,
        cancels_client_order_id="stop-1",
    ))
    assert subject.standing.orders_on_the_book == 1
    assert subject.resting_orders[0].client_order_id == "stop-2"


def test_nothing_on_the_book_triggers_across_a_feed_jump():
    """A gap is not a trade, and a stop fired on one is a loss nobody took."""
    subject = fill_simulator()
    subject.simulate(**a_stop(stop_price=99.0, market_price=100.0))
    subject.observe_feed_jump(VENUE, SYMBOL)
    assert subject.evaluate_resting({(VENUE, SYMBOL): 90.0}) == ()
    assert subject.standing.orders_on_the_book == 1


def test_a_quiet_symbol_does_not_lose_its_protection():
    """No new price is not a reason to withdraw a stop."""
    subject = fill_simulator()
    subject.simulate(**a_stop(stop_price=99.0, market_price=100.0))
    assert subject.evaluate_resting({}) == ()
    assert subject.standing.orders_on_the_book == 1


def test_a_triggered_order_with_no_trigger_price_is_refused():
    subject = fill_simulator()
    result = subject.simulate(**a_stop(stop_price=None))
    assert result.outcome == REFUSED_NO_PRICE
    assert "waits for nothing" in result.reason


# ---- entries are market orders ----------------------------------------------

def test_an_entry_is_routed_as_a_market_order_not_a_limit():
    """Operator, 2026-08-23: market orders, not limit orders.

    A limit at the decision price fills only if the market comes back to it, and
    a decision to be long is not a decision to be long at one price. The price the
    decision was made at is still on the stamped order, as evidence.
    """
    router = OrderDestinationRouter()
    stamped = StampedStub()
    stamped.stop_price = 97.0
    requests = router.route(stamped, Mode("paper"))
    assert len(requests) == 1
    request = requests[0]
    assert request.order_type == MARKET
    assert request.limit_price == 0.0
    assert request.waits_for_a_trigger is False, (
        "an entry carries the stop that will protect it; that must not make the entry itself "
        "wait for the market to fall to that stop"
    )
    assert request.stop_price == 97.0


# ---- live-switch-guard reads the record it judges (2026-08-25) ---------------

def test_a_bot_mature_in_no_regime_does_not_graduate():
    """The fifth test: an account record with no edge behind it is not evidence."""
    subject = guard(trades=30, days=7.0, drawdown=0.2)
    subject.observe_paper_maturity(graduated_bot(mature_in=()))
    verdict = subject.judge("bull")
    assert verdict.verdict == NOT_GRADUATED
    assert any("graduated in no regime" in failure for failure in verdict.failures)


def test_the_net_result_is_taken_after_costs():
    """A strategy profitable before fees is not profitable."""
    subject = guard(trades=1, days=0.5, drawdown=1.0)
    subject.observe_bot_trades("bull", 1)
    subject.observe_edge_maturity("bull", "trending", True)
    subject.observe_closed_trade(realised_pnl=100.0, fees_paid=140.0, opened_at_ns=0, closed_at_ns=1)
    verdict = subject.judge("bull")
    assert verdict.maturity.net_result_after_costs == -40.0
    assert verdict.verdict == NOT_GRADUATED


def test_days_traded_is_the_span_of_the_trades_seen():
    day = 86_400 * 1_000_000_000
    subject = guard(trades=1, days=3.0, drawdown=1.0)
    subject.observe_closed_trade(1.0, 0.0, opened_at_ns=day, closed_at_ns=2 * day)
    subject.observe_closed_trade(1.0, 0.0, opened_at_ns=5 * day, closed_at_ns=6 * day)
    assert subject.days_traded == 5.0


def test_the_worst_drawdown_is_the_deepest_seen_and_not_the_latest():
    """A recovery does not erase the fall a human would have turned off during."""
    subject = guard(trades=1, days=0.5, drawdown=0.2)
    subject.observe_drawdown(0.4)
    subject.observe_drawdown(0.05)
    subject.observe_bot_trades("bull", 5)
    subject.observe_edge_maturity("bull", "trending", True)
    assert subject.judge("bull").maturity.worst_drawdown_fraction == 0.4


def test_a_bot_with_no_record_at_all_is_refused_as_such():
    assert guard().judge("nobody").verdict == NO_RECORD


# ---- a resting stop must survive a restart -------------------------------------
#
# Held in memory alone until 2026-08-26. The consequence is not a board gap: this
# part only ever hears about a stop when something upstream proposes a *new* one,
# so a position already open at restart is left with no protective order and
# nothing reports it. The lot books were fixed for the same reason on 2026-08-25,
# after 46 restarts left 86% of everything ever opened unaccounted for.


def test_a_resting_stop_comes_back_after_a_restart(durable_tmp_path):
    """The whole point: the next process knows what is protecting each position."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.paper_live_trading.stop_order_manager import CHECKPOINT_COMPONENT

    store = DurableStateStore(durable_tmp_path)
    before = StopOrderManager()
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, before, {}
    )
    before.apply_adjustment(VENUE, SYMBOL, LONG, 1.5, 98.0, Mode("paper"))
    write(before.standing.placed)
    assert before.resting_stop(VENUE, SYMBOL) == 98.0

    after = StopOrderManager()
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, after, {}
    )

    assert after.resting_stop(VENUE, SYMBOL) == 98.0, (
        "a restart forgot the stop protecting an open position"
    )
    assert after.standing.stops_resting == 1
    assert after.standing.restored_symbols == 1


def test_a_restored_manager_still_refuses_to_widen(durable_tmp_path):
    """A stop that came back must be a stop, not just a number in a dict.

    The refusal to widen is the manager's whole safety property, and it is decided
    against what it believes is resting -- so a restore that produced a lookalike
    object would silently drop it on exactly the positions that had been open
    longest.
    """
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.paper_live_trading.stop_order_manager import CHECKPOINT_COMPONENT

    store = DurableStateStore(durable_tmp_path)
    before = StopOrderManager()
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, before, {}
    )
    before.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 99.0, Mode("paper"))
    write(before.standing.placed)

    after = StopOrderManager()
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, after, {}
    )
    widening = after.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 98.0, Mode("paper"))

    assert widening.action == REFUSED_WIDENING
    assert after.resting_stop(VENUE, SYMBOL) == 99.0


def test_order_ids_do_not_repeat_across_a_restart(durable_tmp_path):
    """A reused id is an id the venue may still have resting against another order."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.paper_live_trading.stop_order_manager import CHECKPOINT_COMPONENT

    store = DurableStateStore(durable_tmp_path)
    before = StopOrderManager()
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, before, {}
    )
    first = before.apply_adjustment(VENUE, SYMBOL, LONG, 1.0, 99.0, Mode("paper"))
    write(before.standing.placed)

    after = StopOrderManager()
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "stop-order-manager", CHECKPOINT_COMPONENT, after, {}
    )
    # A different symbol, so the widening refusal does not shadow the id check.
    second = after.apply_adjustment(VENUE, "ETHUSDT", LONG, 1.0, 99.0, Mode("paper"))

    assert first.place_order_id != second.place_order_id, (
        f"both processes minted {first.place_order_id}"
    )


def test_a_manager_that_has_never_checkpointed_says_so_rather_than_reading_empty(durable_tmp_path):
    """Came back holding nothing, and never ran, are different facts (Rule 8)."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.paper_live_trading.stop_order_manager import CHECKPOINT_COMPONENT

    cold = StopOrderManager()
    restore_and_arm_checkpoint(
        DurableStateStore(durable_tmp_path), CheckpointSchedule(1),
        "stop-order-manager", CHECKPOINT_COMPONENT, cold, {},
    )

    assert cold.standing.checkpoint_verdict, "a cold start recorded no verdict at all"
    assert cold.standing.stops_resting == 0


# ---- one wire, two adjustment shapes ------------------------------------------
#
# `stop-adjustment` carries two payloads: exit-order-chainer's pair of exits the
# instant an entry fills, and profit-lock's raised stop as a trade goes into
# profit. That is the shape which defeats both blueprint checkers, exactly as
# `market-data` did carrying trades and candles.
#
# Measured on the live spine at 12:01 on 2026-08-26, the hour profit-lock first
# had positions to work on: it trailed 34 stops and published all 34, and this
# part dropped every one as unreadable. `placed` 0, and not one refusal counter
# moved -- a shape it cannot read was never a refusal -- so 20 open positions had
# 0 stops resting and nothing anywhere said why.


class _Trail:
    """profit-lock's shape: a new stop, the position's direction, no quantity."""

    def __init__(self, venue_id, symbol, direction, new_stop, did_move=True):
        self.venue_id = venue_id
        self.symbol = symbol
        self.direction = direction
        self.new_stop = new_stop
        self.did_move = did_move


class _ChainedExits:
    """exit-order-chainer's shape: a fill names the side, quantity and both prices."""

    def __init__(self, venue_id, symbol, exit_side, quantity, stop_price,
                 target_price=None, should_be_sent=True):
        self.venue_id = venue_id
        self.symbol = symbol
        self.exit_side = exit_side
        self.quantity = quantity
        self.stop_price = stop_price
        self.target_price = target_price
        self.should_be_sent = should_be_sent


def test_a_trailed_stop_is_read_and_sized_from_the_position():
    """profit-lock names no quantity, so it comes from what is actually held."""
    from parts.paper_live_trading.stop_order_manager import read_adjustment

    held = {(VENUE, SYMBOL): 1.5}
    read = read_adjustment(_Trail(VENUE, SYMBOL, LONG, 98.0), held)

    assert read is not None, "profit-lock's shape was unreadable -- the live defect"
    assert read["stop_price"] == 98.0
    assert read["direction"] == LONG
    assert read["quantity"] == 1.5, "a trailed stop must cover what is actually held"
    assert read["target_price"] is None


def test_a_trail_on_a_short_is_sized_from_the_absolute_quantity():
    """A short's held quantity is negative; an order quantity never is."""
    from parts.paper_live_trading.stop_order_manager import read_adjustment

    read = read_adjustment(_Trail(VENUE, SYMBOL, SHORT, 102.0), {(VENUE, SYMBOL): -2.0})
    assert read["quantity"] == 2.0
    assert read["direction"] == SHORT


def test_a_trail_that_did_not_move_is_not_sent_when_a_stop_is_already_resting():
    """Replacing a resting stop with an identical one is a window with no stop."""
    from parts.paper_live_trading.stop_order_manager import SKIP, read_adjustment

    read = read_adjustment(
        _Trail(VENUE, SYMBOL, LONG, 98.0, did_move=False), {(VENUE, SYMBOL): 1.5},
        is_already_resting=lambda venue, symbol: True,
    )
    assert read is SKIP


def test_a_stop_that_did_not_move_is_sent_when_nothing_is_resting():
    """"It did not move" is a reason to send nothing only if something is there.

    Since 2026-08-26 profit-lock states every open position's stop on a cadence
    rather than only when it moves, so an unchanged stop now arrives for a
    position that has none at all. Measured that day before this: 12 open
    positions, 0 stops resting, 4,982 adjustments read here, `placed` 0, and
    every refusal counter at zero -- nothing was refused, it was skipped.
    """
    from parts.paper_live_trading.stop_order_manager import SKIP, read_adjustment

    read = read_adjustment(
        _Trail(VENUE, SYMBOL, LONG, 98.0, did_move=False), {(VENUE, SYMBOL): 1.5},
        is_already_resting=lambda venue, symbol: False,
    )
    assert read is not SKIP and read is not None
    assert read["stop_price"] == 98.0
    assert read["quantity"] == 1.5


def test_a_trail_for_a_position_this_part_does_not_know_is_refused():
    """A stop for no quantity protects nothing and would read as one that rests."""
    from parts.paper_live_trading.stop_order_manager import read_adjustment

    assert read_adjustment(_Trail(VENUE, SYMBOL, LONG, 98.0), {}) is None


def test_the_chainer_shape_still_reads():
    """The shape that already worked must survive teaching the part a second one."""
    from parts.paper_live_trading.stop_order_manager import read_adjustment

    read = read_adjustment(
        _ChainedExits(VENUE, SYMBOL, SELL, 2.0, 98.0, target_price=110.0), {}
    )
    assert read["direction"] == LONG, "the exit side is the opposite of the position's"
    assert read["quantity"] == 2.0
    assert read["stop_price"] == 98.0
    assert read["target_price"] == 110.0


def test_a_chained_exit_that_says_not_to_send_is_skipped():
    from parts.paper_live_trading.stop_order_manager import SKIP, read_adjustment

    read = read_adjustment(
        _ChainedExits(VENUE, SYMBOL, SELL, 0.0, 98.0, should_be_sent=False), {}
    )
    assert read is SKIP


def test_a_shape_carrying_neither_is_unreadable():
    """Unreadable is its own answer, not a refusal and not a skip."""
    from parts.paper_live_trading.stop_order_manager import read_adjustment

    class _Nothing:
        venue_id = VENUE
        symbol = SYMBOL

    assert read_adjustment(_Nothing(), {(VENUE, SYMBOL): 1.0}) is None


def test_health_reports_adjustments_that_never_reached_the_manager():
    """The counter that was local, which is how 34 dropped stops stayed invisible."""
    from parts.paper_live_trading.stop_order_manager import describe_stop_orders

    described = describe_stop_orders(
        StopOrderManager(),
        {"unreadable": 34, "last_unreadable": "StopAdjustment", "held_back": 7},
    )
    assert described["unreadable_adjustments"] == 34
    assert described["last_unreadable_adjustment"] == "StopAdjustment"
    assert described["adjustments_that_said_hold"] == 7


def test_a_trail_does_not_ask_for_a_target_it_never_carried():
    """A refusal counted for something nobody requested reads as a failure.

    Measured on the live spine at 12:07 on 2026-08-26: 14 trailed stops placed
    correctly, and 14 `refused_no_target` beside them, because the tick asked for
    a target on every adjustment including the ones that carry none by design.
    """
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 1.5, 98.0, Mode("paper"))
    assert manager.standing.refused_no_target == 0


# ---- a stop is cut to the position it protects, not to the one it was proposed for

# The step below which a stop and its position cannot be made to differ by any
# order, matching the shipped `order_quantity_increment`.
STOP_QUANTITY_INCREMENT = 0.001


def test_a_position_that_grew_gets_its_stop_re_cut_to_the_whole_of_it():
    """`binance-usdm|AKEUSDT`: 231,812 held, 198.634 behind the stop, board green.

    The stop was placed for what was held when something upstream proposed it.
    Every fill after that changed the position and proposed nothing.
    """
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 198.634, 98.0, Mode("paper"))
    action = manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=231812.256,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action is not None
    assert action.action == RESIZE
    assert action.quantity == pytest.approx(231812.256)
    # The stop price is not this method's decision and must come across untouched.
    assert action.stop_price == 98.0
    assert manager.resting_stop(VENUE, SYMBOL) == 98.0
    assert manager.resting_quantity(VENUE, SYMBOL) == pytest.approx(231812.256)
    # Placed before cancelled, like every other replacement.
    assert action.place_order_id and action.cancel_order_id


def test_a_stop_that_already_fits_the_position_is_not_re_cut():
    """Otherwise every fill churns an order that changes nothing."""
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 10.0, 98.0, Mode("paper"))
    assert manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=10.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None
    # Nor for a difference smaller than one tradeable step.
    assert manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=10.0002,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None
    assert manager.standing.resized_to_the_position == 0


def test_nothing_is_re_cut_for_a_position_with_no_stop_resting():
    """A resize places no new protection: an unprotected position stays unprotected
    and is reported as such, rather than acquiring a stop nobody chose a price for."""
    manager = StopOrderManager()
    assert manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=5.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None


def test_a_position_scaled_out_of_has_its_stop_cut_down_too():
    """A stop for more than is held is a naked short the moment it fills."""
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 10.0, 98.0, Mode("paper"))
    action = manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=4.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action.action == RESIZE
    assert action.quantity == pytest.approx(4.0)


def test_a_position_that_grew_gets_its_target_re_cut_to_the_whole_of_it():
    """ADAUSDT, live, 2026-08-30: 244.379 held when the target was placed, grew to
    244.618, and the target -- never resized -- closed only its original 244.379
    when it filled, leaving exactly 0.239 as unprotected, ungated dust.
    """
    manager = StopOrderManager()
    manager.place_target(VENUE, SYMBOL, LONG, 244.379, 110.0, Mode("paper"))
    action = manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=244.618,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action is not None
    assert action.action == RESIZE_TARGET
    assert action.quantity == pytest.approx(244.618)
    # The target price is not this method's decision and must come across untouched.
    assert action.stop_price == 110.0
    assert manager.resting_target_quantity(VENUE, SYMBOL) == pytest.approx(244.618)
    # Placed before cancelled, like the stop's own resize.
    assert action.place_order_id and action.cancel_order_id
    assert manager.standing.resized_target_to_the_position == 1


def test_a_target_that_already_fits_the_position_is_not_re_cut():
    """Otherwise every fill churns an order that changes nothing."""
    manager = StopOrderManager()
    manager.place_target(VENUE, SYMBOL, LONG, 10.0, 110.0, Mode("paper"))
    assert manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=10.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None
    # Nor for a difference smaller than one tradeable step.
    assert manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=10.0002,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None
    assert manager.standing.resized_target_to_the_position == 0


def test_nothing_is_re_cut_for_a_position_with_no_target_resting():
    """A resize places no new target: a position with only a stop stays that way."""
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 5.0, 98.0, Mode("paper"))
    assert manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=5.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    ) is None


def test_a_position_scaled_out_of_has_its_target_cut_down_too():
    """A target for more than is held would close a quantity that is not there."""
    manager = StopOrderManager()
    manager.place_target(VENUE, SYMBOL, LONG, 10.0, 110.0, Mode("paper"))
    action = manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=4.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action.action == RESIZE_TARGET
    assert action.quantity == pytest.approx(4.0)


def test_a_target_restored_with_no_known_quantity_still_resizes():
    """Crashed the live spine 2026-08-30: a checkpoint written before
    target_quantity existed restores a resting target with `target_order_id`
    set and `target_quantity=None`. The resize must not assume that quantity
    is known -- it re-cuts to the position regardless -- and must not crash
    formatting a None into the reason string.
    """
    manager = StopOrderManager()
    manager.restore_from_checkpoint({
        "resting": {
            f"{VENUE}|{SYMBOL}": {
                "order_id": "stop-1", "stop_price": 98.0, "quantity": 10.0,
                "target_order_id": "target-1", "target_price": 110.0,
                # target_quantity omitted, as a pre-2026-08-30 checkpoint would.
            },
        },
        "sequence": 1,
    })
    action = manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=15.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action is not None
    assert action.action == RESIZE_TARGET
    assert action.quantity == pytest.approx(15.0)
    assert manager.resting_target_quantity(VENUE, SYMBOL) == pytest.approx(15.0)


def test_resizing_the_target_leaves_the_stop_untouched():
    """The two exits are resized independently -- one moving must not perturb the other."""
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, LONG, 10.0, 98.0, Mode("paper"))
    manager.place_target(VENUE, SYMBOL, LONG, 10.0, 110.0, Mode("paper"))
    manager.resize_target_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=15.0,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert manager.resting_stop(VENUE, SYMBOL) == 98.0
    assert manager.resting_quantity(VENUE, SYMBOL) == pytest.approx(10.0)
    assert manager.resting_target_quantity(VENUE, SYMBOL) == pytest.approx(15.0)


def test_a_short_position_has_its_stop_re_cut_too():
    """`Position.quantity` is signed; an order's quantity is not.

    A resize handed the signed number would read every short as having no
    position to protect and would silently never re-cut one.
    """
    manager = StopOrderManager()
    manager.apply_adjustment(VENUE, SYMBOL, SHORT, 2.0, 102.0, Mode("paper"))
    action = manager.resize_stop_to_the_position(
        venue_id=VENUE, symbol=SYMBOL, direction=SHORT, quantity=abs(-9.0),
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert action.action == RESIZE
    assert action.side == BUY
    assert action.quantity == pytest.approx(9.0)


def test_a_fill_id_is_not_reused_after_the_simulator_restarts():
    """`_fill_sequence` restarts at zero with the part (2026-09-13).

    Three real fills on 2026-09-07 carried an earlier fill's id, because the same
    client order id was filled again after a restart, and every book dropped them
    as duplicates. Two simulators here are that restart.
    """
    before = fill_simulator().simulate(**an_order(client_order_id="stop-upstox-NIFTY-2"))
    after_restart = fill_simulator().simulate(**an_order(client_order_id="stop-upstox-NIFTY-2"))
    assert before.fill.fill_id != after_restart.fill.fill_id


# ---- an exit never outlives its position (2026-09-13) ------------------------

NIFTY_FILLS = json.loads((
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-08-nifty-23700-ce-fills-and-a-stale-stop.json"
).read_text())["fills"]


def nifty_fill(side, quantity):
    """The captured fill of that side and quantity, from this project's own journal."""
    return next(
        fill for fill in NIFTY_FILLS
        if fill["side"] == side and abs(fill["quantity"] - quantity) < 1e-6
    )


def orders_left_on_the_book(actions):
    """The ids a book holds after these actions, applied the way the book applies them."""
    from parts.paper_live_trading.stop_order_manager import as_order_request

    book = set()
    for action in actions:
        if not action.is_actionable:
            continue
        request = as_order_request(action)
        if request.cancels_client_order_id:
            book.discard(request.cancels_client_order_id)
        if action.place_order_id:
            book.add(action.place_order_id)
    return book


def test_a_re_cut_stop_replaces_the_old_one_on_the_book_and_leaves_nothing_after_the_close():
    """NIFTY 23700 CE 15 SEP 26, 2026-09-08, quantities from the captured fills.

    A stop for the 1,430.081 entry, re-cut when 240.304 more filled, then the
    position closed. Until 2026-09-13 the re-cut was never published: the manager
    held the new id, the book held the old one, the close cancelled the new id,
    and the old stop sold 1,430.081 on a flat position.
    """
    venue, symbol = "upstox", "NIFTY 23700 CE 15 SEP 26"
    first = nifty_fill(BUY, 1430.081)["quantity"]
    whole = first + nifty_fill(BUY, 240.3040000000001)["quantity"]
    manager = StopOrderManager()
    actions = [manager.apply_adjustment(venue, symbol, LONG, first, 126.57, Mode("paper"))]
    resize = manager.resize_stop_to_the_position(
        venue_id=venue, symbol=symbol, direction=LONG, quantity=whole,
        money_mode=Mode("paper"), quantity_increment=STOP_QUANTITY_INCREMENT,
    )
    assert resize.action == RESIZE
    assert resize.is_actionable
    actions.append(resize)
    assert orders_left_on_the_book(actions) == {resize.place_order_id}

    actions.extend(manager.observe_position_closed(venue, symbol))
    assert orders_left_on_the_book(actions) == set()


def test_exits_for_an_entry_the_position_had_already_closed_past_are_not_placed():
    """The target closed the position at 09:31:36.909; the entry's exits arrived after."""
    from parts.paper_live_trading.stop_order_manager import ALREADY_CLOSED, read_adjustment
    from parts.risk_capital_allocation.exit_order_chainer import ExitOrders

    venue, symbol = "upstox", "NIFTY 23700 CE 15 SEP 26"
    entry = nifty_fill(BUY, 1430.081)
    closing = nifty_fill(SELL, 1670.385)
    later_entry = nifty_fill(BUY, 1560.0)
    assert entry["filled_at_ns"] < closing["filled_at_ns"] < later_entry["filled_at_ns"]
    closed_at = {(venue, symbol): closing["filled_at_ns"]}

    def exits_for(fill):
        return ExitOrders(
            venue_id=venue, symbol=symbol, entry_order_id=fill["order_id"], exit_side=SELL,
            quantity=fill["quantity"], stop_price=126.57, target_price=130.5,
            outcome="chained", filled_quantity_so_far=fill["quantity"], reason="",
            chained_at_ns=closing["filled_at_ns"] + 1, entry_filled_at_ns=fill["filled_at_ns"],
        )

    assert read_adjustment(exits_for(entry), {}, None, closed_at) is ALREADY_CLOSED
    placed = read_adjustment(exits_for(later_entry), {}, None, closed_at)
    assert isinstance(placed, dict) and placed["quantity"] == 1560.0
    # Held again: the exits apply to a live position, whatever closed before.
    held = {(venue, symbol): 1560.0}
    assert isinstance(read_adjustment(exits_for(entry), held, None, closed_at), dict)
