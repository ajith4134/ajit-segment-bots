"""Portfolio state and the ledger: what was held, what it made, and the account of it.

These two blocks are tested together because they are two halves of one question.
Portfolio state works out what happened; the ledger is the record that it did.
A test that checked either alone would miss the case that matters -- a position
that changed with no journal entry behind it, or a journal describing a position
nothing ever held.

The arithmetic here is checked against worked examples rather than against the
code's own output, because every one of these numbers is a claim about money.
"""

import importlib
import json
from types import SimpleNamespace

import pytest

from parts.ledger.control_recorder import ControlRecorder
from parts.ledger.funding_settlement_recorder import FundingSettlementRecorder
from parts.ledger.journal_integrity_checker import (
    CHAIN_BREAK, DIGEST_MISMATCH, SEQUENCE_GAP, JournalIntegrityChecker,
)
from parts.ledger.learning_recorder import LearningRecorder
from parts.ledger.position_recorder import CHANGED, CLOSED, OPENED, PositionRecorder
from parts.ledger.trade_lifecycle_recorder import TradeLifecycleRecorder
from parts.portfolio_state.cost_basis_tracker import CostBasisTracker
from parts.portfolio_state.fill_reconciler import AGREED, DIVERGED, UNCHECKED, FillReconciler
from parts.portfolio_state.fund_lock_ledger import LOCKED, REFUSED, RELEASED, FundLockLedger
from parts.portfolio_state.liquidation_price_tracker import LiquidationPriceTracker
from parts.portfolio_state.peak_excursion_tracker import PeakExcursionTracker
from parts.portfolio_state.position_close_detector import PositionCloseDetector
from parts.portfolio_state.usdt_pnl_accountant import UsdtPnlAccountant
from runtime.journal import GENESIS_DIGEST, Journal, JournalEntry
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trading_types import BUY, FLAT, LONG, SELL, SHORT, Fill, Position

BLOCK_PARTS = {
    "fill-reconciler": "parts.portfolio_state.fill_reconciler",
    "position-close-detector": "parts.portfolio_state.position_close_detector",
    "peak-excursion-tracker": "parts.portfolio_state.peak_excursion_tracker",
    "usdt-pnl-accountant": "parts.portfolio_state.usdt_pnl_accountant",
    "liquidation-price-tracker": "parts.portfolio_state.liquidation_price_tracker",
    "fund-lock-ledger": "parts.portfolio_state.fund_lock_ledger",
    "cost-basis-tracker": "parts.portfolio_state.cost_basis_tracker",
    "trade-lifecycle-recorder": "parts.ledger.trade_lifecycle_recorder",
    "position-recorder": "parts.ledger.position_recorder",
    "learning-recorder": "parts.ledger.learning_recorder",
    "control-recorder": "parts.ledger.control_recorder",
    "journal-integrity-checker": "parts.ledger.journal_integrity_checker",
    "funding-settlement-recorder": "parts.ledger.funding_settlement_recorder",
}

SECOND_NS = 1_000_000_000
VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
SECOND = 1_000_000_000


def fill(fill_id, side, price, quantity, at=1, fee=0.0, leverage=1.0):
    return Fill(
        fill_id=fill_id, venue_id=VENUE, symbol=SYMBOL, side=side,
        price=price, quantity=quantity, fee=fee, filled_at_ns=at * SECOND,
        leverage=leverage,
    )


def position(quantity, entry=100.0, at=1, leverage=None):
    return Position(
        venue_id=VENUE, symbol=SYMBOL, quantity=quantity, average_entry_price=entry,
        realised_pnl=0.0, fees_paid=0.0, opened_at_ns=at * SECOND, updated_at_ns=at * SECOND,
        leverage=leverage,
    )

# The venue quantity step these tests reason against, matching the shipped
# `order_quantity_increment`. Stated rather than defaulted, because it is the
# bound that decides when a round trip is over.
QUANTITY_INCREMENT = 0.001


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- cost-basis-tracker ------------------------------------------------------

def test_the_average_is_weighted_by_quantity_not_by_fill_count():
    """1 at 100 and 3 at 200 averages 175, not 150."""
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    basis = tracker.observe_fill(fill("f2", BUY, 200.0, 3.0))
    assert basis.quantity == 4.0
    assert basis.average_price == pytest.approx(175.0)


def test_a_repeated_fill_id_does_not_move_the_basis():
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    basis = tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    assert basis.quantity == 1.0
    assert tracker.standing.duplicates_ignored == 1


def test_a_partial_close_leaves_the_oldest_lots_consumed_first():
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    tracker.observe_fill(fill("f2", BUY, 200.0, 1.0))
    basis = tracker.observe_fill(fill("f3", SELL, 300.0, 1.0))
    # The 100 lot went; what remains was bought at 200.
    assert basis.quantity == 1.0
    assert basis.average_price == pytest.approx(200.0)


def test_a_fill_through_flat_opens_the_other_side_at_its_own_price():
    """A long of 1 hit by a sell of 3 is a short of 2 at the sell price."""
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    basis = tracker.observe_fill(fill("f2", SELL, 300.0, 3.0))
    assert basis.direction == SHORT
    assert basis.quantity == 2.0
    assert basis.average_price == pytest.approx(300.0)
    assert tracker.standing.reversals == 1


# ---- fill-reconciler ---------------------------------------------------------

def test_a_position_is_built_from_fills():
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0, fee=0.1))
    held = reconciler.observe_fill(fill("f2", BUY, 200.0, 2.0, fee=0.1))
    assert held.quantity == 4.0
    assert held.average_entry_price == pytest.approx(150.0)
    assert held.fees_paid == pytest.approx(0.2)


def test_closing_realises_the_difference_from_the_average():
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0))
    held = reconciler.observe_fill(fill("f2", SELL, 150.0, 1.0))
    assert held.quantity == 1.0
    assert held.realised_pnl == pytest.approx(50.0)


def test_a_short_realises_the_opposite_way():
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", SELL, 100.0, 1.0))
    held = reconciler.observe_fill(fill("f2", BUY, 90.0, 1.0))
    assert held.realised_pnl == pytest.approx(10.0)


def test_a_reopen_starts_its_own_fees_not_the_last_round_trips():
    """ADAUSDT, live, 2026-08-30: a position closed with fees_paid=37.23 (five
    round trips deep), reopened, and reported fees_paid=37.26 -- barely moved
    by the new fill's own ~0.03, because the reopen kept accumulating instead
    of starting fresh.
    """
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 1.0, fee=5.0))
    reconciler.observe_fill(fill("f2", SELL, 100.0, 1.0, fee=5.0))  # closes flat
    held = reconciler.observe_fill(fill("f3", BUY, 100.0, 1.0, fee=0.03))
    assert held.quantity == 1.0
    assert held.fees_paid == pytest.approx(0.03)


def test_a_reopen_starts_its_own_realised_pnl_not_the_last_round_trips():
    """The same accumulation bug, one field over: `bull`/`bear-position-
    invalidation-watcher` score a close as a win or loss off `realised_pnl > 0`,
    which must be this round trip's result, not the symbol's lifetime total.
    """
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 1.0))
    reconciler.observe_fill(fill("f2", SELL, 200.0, 1.0))  # +100, closes flat
    held = reconciler.observe_fill(fill("f3", BUY, 100.0, 1.0))
    assert held.quantity == 1.0
    assert held.realised_pnl == pytest.approx(0.0)


def test_adding_to_an_open_position_still_accumulates_fees_and_realised():
    """The reset is only for a reopen from flat -- an add-to or a partial close
    on an already-open position must keep accumulating exactly as before.
    """
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0, fee=0.1))
    held = reconciler.observe_fill(fill("f2", BUY, 100.0, 1.0, fee=0.1))
    assert held.fees_paid == pytest.approx(0.2)
    held = reconciler.observe_fill(fill("f3", SELL, 150.0, 1.0, fee=0.1))
    assert held.fees_paid == pytest.approx(0.3)
    assert held.realised_pnl == pytest.approx(50.0)


def test_the_venue_disagreeing_is_reported_not_adopted():
    """A divergence means a fill was missed, duplicated or invented."""
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0))
    reconciler.observe_venue_report(VENUE, SYMBOL, 3.0)
    check = reconciler.reconcile(VENUE, SYMBOL)
    assert check.verdict == DIVERGED
    assert check.difference == pytest.approx(-1.0)
    assert check.position.quantity == 2.0, "our figure must survive the disagreement"


def test_agreement_within_tolerance_is_agreement():
    reconciler = FillReconciler(quantity_tolerance=1e-6)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0))
    reconciler.observe_venue_report(VENUE, SYMBOL, 2.0000001)
    assert reconciler.reconcile(VENUE, SYMBOL).verdict == AGREED


def test_no_venue_report_is_unchecked_not_agreed():
    reconciler = FillReconciler(quantity_tolerance=1e-9)
    reconciler.observe_fill(fill("f1", BUY, 100.0, 2.0))
    assert reconciler.reconcile(VENUE, SYMBOL).verdict == UNCHECKED


# ---- position-close-detector -------------------------------------------------

def test_a_partial_close_is_not_a_closed_trade():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 2.0))
    assert detector.observe_fill(fill("f2", SELL, 150.0, 1.0)) is None
    assert detector.standing.partial_closes == 1


def test_reaching_flat_emits_the_round_trip():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 2.0, at=1))
    detector.observe_fill(fill("f2", SELL, 150.0, 1.0, at=2))
    trade = detector.observe_fill(fill("f3", SELL, 150.0, 1.0, at=3))
    assert trade is not None
    assert trade.direction == LONG
    assert trade.realised_pnl == pytest.approx(100.0)
    assert trade.holding_seconds == pytest.approx(2.0)


def test_a_reopened_symbol_s_closed_trade_reports_only_its_own_fees():
    """ADAUSDT, live, 2026-08-30: a $50.07 trade closed with fees_paid=37.287 --
    every round trip this process had ever made on the symbol, summed, because
    only realised/entered/entry-cost reset on a reopen and fees never did.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1, fee=5.0))
    first = detector.observe_fill(fill("f2", SELL, 100.0, 1.0, at=2, fee=5.0))
    assert first.fees_paid == pytest.approx(10.0)
    detector.observe_fill(fill("f3", BUY, 100.0, 1.0, at=3, fee=0.02))
    second = detector.observe_fill(fill("f4", SELL, 100.0, 1.0, at=4, fee=0.02))
    assert second.fees_paid == pytest.approx(0.04)


def test_a_round_trip_closed_in_slices_reaches_flat_and_emits_its_trade():
    """The defect: 1.0 sold as 0.99 then 0.01 never reached flat.

    `1.0 - 0.99` is `0.010000000000000009` in float, so the book held 9e-18 back,
    `total_quantity` stayed above zero, and no closed trade was ever emitted. The
    position stayed open forever and the round trip could never be scored.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    assert detector.observe_fill(fill("f2", SELL, 110.0, 0.99, at=2)) is None
    trade = detector.observe_fill(fill("f3", SELL, 110.0, 0.01, at=3))
    assert trade is not None, "the round trip never reached flat"
    assert trade.quantity == pytest.approx(1.0)
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.realised_pnl == pytest.approx(10.0)


def test_a_position_dribbled_out_in_ten_slices_still_closes():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    trade = None
    for index in range(10):
        trade = detector.observe_fill(fill(f"x{index}", SELL, 110.0, 0.1, at=2 + index))
    assert trade is not None, "ten slices never summed back to the position"
    assert trade.quantity == pytest.approx(1.0)
    assert trade.realised_pnl == pytest.approx(10.0)


def test_exits_mirroring_ragged_entry_fills_cancel_exactly():
    """The shape production actually makes, and why float quantities are safe here.

    An entry split by participation-capped-order-splitter arrives as ragged
    slices, and exit-order-chainer chains an exit for each fill's own quantity.
    So the same values go into the book and come back out, and in decimal they
    cancel exactly however ugly they are. This pins that property, because it is
    the reason `Fill.quantity` can stay a float while the book counts exactly.
    """
    ragged = [0.3, 0.3, 0.3, 0.07, 0.03]
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    for index, size in enumerate(ragged):
        detector.observe_fill(fill(f"in{index}", BUY, 100.0, size, at=1 + index))
    trade = None
    for index, size in enumerate(ragged):
        trade = detector.observe_fill(fill(f"out{index}", SELL, 110.0, size, at=20 + index))
    assert trade is not None, "a ragged round trip never reached flat"
    assert trade.quantity == pytest.approx(sum(ragged))
    assert trade.entry_price == pytest.approx(100.0)


def test_a_cost_basis_returns_to_flat_when_a_position_is_closed_in_slices():
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    tracker.observe_fill(fill("f2", SELL, 110.0, 0.99, at=2))
    tracker.observe_fill(fill("f3", SELL, 110.0, 0.01, at=3))
    basis = tracker.read(VENUE, SYMBOL)
    assert basis.direction == FLAT
    assert basis.quantity == pytest.approx(0.0)


def test_the_round_trip_is_priced_by_what_it_entered_not_by_its_last_slice():
    """Same round trip, sold in one slice or in two: the same entry and quantity.

    The entry used to be backed out of the closing fill, so the whole trip's
    profit was divided by whatever fraction happened to close last. A trade sold
    0.9 then 0.1 reported an entry the market never printed.
    """
    whole = PositionCloseDetector(QUANTITY_INCREMENT)
    whole.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    in_one = whole.observe_fill(fill("f2", SELL, 110.0, 1.0, at=2))

    sliced = PositionCloseDetector(QUANTITY_INCREMENT)
    sliced.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    sliced.observe_fill(fill("f2", SELL, 110.0, 0.9, at=2))
    in_two = sliced.observe_fill(fill("f3", SELL, 110.0, 0.1, at=3))

    assert in_one.entry_price == pytest.approx(100.0)
    assert in_one.quantity == pytest.approx(1.0)
    assert in_two.entry_price == pytest.approx(in_one.entry_price)
    assert in_two.quantity == pytest.approx(in_one.quantity)


def test_the_entry_price_is_weighted_across_every_opening_fill():
    """Bought 1 at 90 and 1 at 110: the round trip entered at 100, not at either."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 90.0, 1.0, at=1))
    detector.observe_fill(fill("f2", BUY, 110.0, 1.0, at=2))
    trade = detector.observe_fill(fill("f3", SELL, 105.0, 2.0, at=3))
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.quantity == pytest.approx(2.0)


def test_a_reversal_prices_the_new_round_trip_from_its_own_entry():
    """The side opened by a reversal entered at the reversing fill's price."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0, at=1))
    first = detector.observe_fill(fill("f2", SELL, 120.0, 3.0, at=2))
    second = detector.observe_fill(fill("f3", BUY, 110.0, 2.0, at=3))
    assert first.entry_price == pytest.approx(100.0)
    assert first.quantity == pytest.approx(1.0)
    assert second.entry_price == pytest.approx(120.0)
    assert second.quantity == pytest.approx(2.0)


def test_the_reported_entry_and_exit_reproduce_the_realised_profit():
    """A closed trade's four numbers must agree with each other."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 2.0, at=1))
    detector.observe_fill(fill("f2", SELL, 130.0, 1.5, at=2))
    trade = detector.observe_fill(fill("f3", SELL, 130.0, 0.5, at=3))
    implied = (trade.exit_price - trade.entry_price) * trade.quantity
    assert implied == pytest.approx(trade.realised_pnl)


def test_closing_fills_match_the_oldest_lots_first():
    """Bought at 100 then 200, sold once at 150: the 100 lot is what closed."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    detector.observe_fill(fill("f2", BUY, 200.0, 1.0))
    assert detector.observe_fill(fill("f3", SELL, 150.0, 1.0)) is None
    trade = detector.observe_fill(fill("f4", SELL, 150.0, 1.0))
    # +50 on the first lot, -50 on the second: flat overall.
    assert trade.realised_pnl == pytest.approx(0.0)


def test_a_reversal_closes_one_trade_and_opens_another():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 120.0, 3.0))
    assert trade.realised_pnl == pytest.approx(20.0)
    assert detector.standing.reversals == 1
    # The new short of 2 is open, and closing it emits its own trade.
    second = detector.observe_fill(fill("f3", BUY, 110.0, 2.0))
    assert second.direction == SHORT
    assert second.realised_pnl == pytest.approx(20.0)


def test_a_closed_trade_carries_its_excursion():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_excursion(VENUE, SYMBOL, best=75.0, worst=-10.0)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 110.0, 1.0))
    assert trade.best_unrealised == 75.0 and trade.worst_unrealised == -10.0


def test_a_stale_excursion_is_widened_by_the_exit_itself():
    """The live bug (2026-08-30): a trade closing exactly at its own best point
    can beat peak-excursion-tracker's relayed update back here, since the two
    parts learn of the same moment from two independent, unsynchronised feeds.
    A closed trade's net must never exceed its own recorded peak."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_excursion(VENUE, SYMBOL, best=5.0, worst=-10.0)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 110.0, 1.0))
    assert trade.best_unrealised == pytest.approx(10.0)
    assert trade.realised_pnl <= trade.best_unrealised


def test_a_stale_excursion_is_widened_by_a_worse_exit_too():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_excursion(VENUE, SYMBOL, best=50.0, worst=-5.0)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 90.0, 1.0))
    assert trade.worst_unrealised == pytest.approx(-10.0)
    assert trade.realised_pnl >= trade.worst_unrealised


def test_no_excursion_at_all_falls_back_to_the_exit_itself():
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 110.0, 1.0))
    assert trade.best_unrealised == pytest.approx(10.0)
    assert trade.worst_unrealised == pytest.approx(10.0)


# ---- peak-excursion-tracker --------------------------------------------------

def test_the_best_and_worst_points_are_both_kept():
    """RL-042: a trade that closed flat after being 5% up was a missed exit."""
    tracker = PeakExcursionTracker()
    tracker.observe_position(position(2.0, entry=100.0))
    tracker.observe_price(VENUE, SYMBOL, 105.0, observed_at_ns=1 * SECOND_NS)
    tracker.observe_price(VENUE, SYMBOL, 90.0, observed_at_ns=2 * SECOND_NS)
    excursion = tracker.observe_price(VENUE, SYMBOL, 100.0, observed_at_ns=3 * SECOND_NS)
    assert excursion.observed_at_ns == 3 * SECOND_NS, (
        "an excursion is dated by the print it describes, not by when the part read it: "
        "8.5 million of these are what stop placement and exit timing are learned from"
    )
    assert excursion.best_unrealised == pytest.approx(10.0)
    assert excursion.worst_unrealised == pytest.approx(-20.0)
    assert excursion.current_unrealised == pytest.approx(0.0)
    assert excursion.best_price == 105.0 and excursion.worst_price == 90.0


def test_a_short_position_profits_when_the_price_falls():
    tracker = PeakExcursionTracker()
    tracker.observe_position(position(-1.0, entry=100.0))
    excursion = tracker.observe_price(VENUE, SYMBOL, 90.0, observed_at_ns=4 * SECOND_NS)
    assert excursion.best_unrealised == pytest.approx(10.0)


def test_a_price_with_no_position_is_counted_not_estimated():
    tracker = PeakExcursionTracker()
    assert tracker.observe_price(VENUE, SYMBOL, 100.0, observed_at_ns=5 * SECOND_NS) is None
    assert tracker.standing.without_cost_basis == 1


def test_going_flat_clears_the_excursion():
    tracker = PeakExcursionTracker()
    tracker.observe_position(position(1.0, entry=100.0))
    tracker.observe_price(VENUE, SYMBOL, 200.0, observed_at_ns=6 * SECOND_NS)
    tracker.observe_position(position(0.0))
    assert tracker.read(VENUE, SYMBOL) is None


def test_a_reversal_starts_its_own_excursion_rather_than_inheriting_the_old_one():
    """The live bug (2026-08-30): a single fill can close one side and open the
    opposite one without ever publishing an exactly-flat position in between
    (position-close-detector's own reversal path does this). A position open
    0 seconds read a peak of +153.18, inherited whole from the opposite-
    direction position that had just closed into it."""
    tracker = PeakExcursionTracker()
    tracker.observe_position(position(1.0, entry=100.0))  # long
    tracker.observe_price(VENUE, SYMBOL, 200.0, observed_at_ns=6 * SECOND_NS)  # huge favourable peak
    tracker.observe_position(position(-1.0, entry=200.0, at=7))  # reverses to short, same tick
    excursion = tracker.observe_price(VENUE, SYMBOL, 199.0, observed_at_ns=8 * SECOND_NS)
    assert excursion.best_unrealised == pytest.approx(1.0)  # only this leg's own move
    assert excursion.samples == 1


# ---- an open position's excursion survives a restart -------------------------
#
# Held in memory alone until 2026-08-29, the same defect regime-classifier and
# cointegration-pair-finder had before their own checkpoints: a restart wiped
# every open position's true best/worst back to zero. Measured this session:
# five spine restarts in one sitting, each a silent reset.


def test_read_checkpoint_state_round_trips_into_a_fresh_tracker():
    subject = PeakExcursionTracker()
    subject.observe_position(position(2.0, entry=100.0))
    subject.observe_price(VENUE, SYMBOL, 110.0, observed_at_ns=1 * SECOND_NS)
    subject.observe_price(VENUE, SYMBOL, 90.0, observed_at_ns=2 * SECOND_NS)

    restored = PeakExcursionTracker()
    assert restored.restore_from_checkpoint(subject.read_checkpoint_state()) == 1
    excursion = restored.read(VENUE, SYMBOL)
    assert excursion.best_unrealised == pytest.approx(20.0)
    assert excursion.worst_unrealised == pytest.approx(-20.0)


def test_a_restored_tracker_keeps_ratcheting_from_where_it_left_off():
    """A restart must not let a position's true worst point be forgotten."""
    subject = PeakExcursionTracker()
    subject.observe_position(position(1.0, entry=100.0))
    subject.observe_price(VENUE, SYMBOL, 80.0, observed_at_ns=1 * SECOND_NS)

    restored = PeakExcursionTracker()
    restored.restore_from_checkpoint(subject.read_checkpoint_state())
    restored.observe_position(position(1.0, entry=100.0))
    # A print better than the true worst must not erase what the position
    # already went through before the restart.
    excursion = restored.observe_price(VENUE, SYMBOL, 95.0, observed_at_ns=2 * SECOND_NS)
    assert excursion.worst_unrealised == pytest.approx(-20.0), (
        "a restart forgot how far against this position had already gone"
    )


def test_a_tracker_that_never_ran_restores_to_nothing_held():
    """Came back holding nothing, and never ran, are different facts (Rule 8)."""
    subject = PeakExcursionTracker()
    assert subject.restore_from_checkpoint(subject.read_checkpoint_state()) == 0
    assert subject.standing.restored_symbols == 0


def test_a_tracker_comes_back_after_a_restart_through_the_real_checkpoint_store(
    durable_tmp_path,
):
    from parts.portfolio_state.peak_excursion_tracker import CHECKPOINT_COMPONENT
    from runtime.durable_state import CheckpointSchedule, DurableStateStore, restore_and_arm_checkpoint

    store = DurableStateStore(durable_tmp_path)
    before = PeakExcursionTracker()
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "peak-excursion-tracker", CHECKPOINT_COMPONENT, before, {}
    )
    before.observe_position(position(2.0, entry=100.0))
    before.observe_price(VENUE, SYMBOL, 130.0, observed_at_ns=1 * SECOND_NS)
    write(before.checkpointable_observations)

    after = PeakExcursionTracker()
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "peak-excursion-tracker", CHECKPOINT_COMPONENT, after, {}
    )

    excursion = after.read(VENUE, SYMBOL)
    assert excursion.best_unrealised == pytest.approx(60.0), (
        "a restart forgot the excursion an open position had already shown"
    )
    assert after.standing.restored_symbols == 1


def test_a_position_carries_the_leverage_of_the_fill_that_opened_it():
    """The only place the decision that chose it survives (2026-08-26)."""
    subject = FillReconciler(quantity_tolerance=0.0)
    held = subject.observe_fill(fill("f1", BUY, 100.0, 1.0, leverage=10.0))
    assert held.leverage == 10.0


def test_the_leverage_survives_a_restart():
    subject = FillReconciler(quantity_tolerance=0.0)
    subject.observe_fill(fill("f1", BUY, 100.0, 1.0, leverage=10.0))

    restored = FillReconciler(quantity_tolerance=0.0)
    restored.restore_from_checkpoint(subject.read_checkpoint_state())
    assert restored.reconcile(VENUE, SYMBOL).position.leverage == 10.0


def test_a_checkpoint_from_before_positions_carried_leverage_restores_unknown():
    """Not 1x: a position restored as unlevered gets a liquidation price that is
    wrong rather than absent, and only an absent one stops new risk."""
    subject = FillReconciler(quantity_tolerance=0.0)
    subject.restore_from_checkpoint({
        "positions": {
            f"{VENUE}|{SYMBOL}": {
                "venue_id": VENUE, "symbol": SYMBOL, "quantity": 1.0,
                "average_entry_price": 100.0, "realised_pnl": 0.0, "fees_paid": 0.0,
                "opened_at_ns": SECOND, "updated_at_ns": SECOND,
            }
        },
        "seen_fills": [],
    })
    assert subject.reconcile(VENUE, SYMBOL).position.leverage is None


def test_adding_to_a_position_keeps_what_its_margin_was_posted_at():
    subject = FillReconciler(quantity_tolerance=0.0)
    subject.observe_fill(fill("f1", BUY, 100.0, 1.0, leverage=10.0))
    held = subject.observe_fill(fill("f2", BUY, 102.0, 1.0, at=2, leverage=1.0))
    assert held.leverage == 10.0


# ---- liquidation-price-tracker -----------------------------------------------

def test_a_long_is_liquidated_below_its_entry():
    """10x with a 0.5% maintenance rate: the price may fall 9.5% before it dies."""
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0))
    tracker.set_leverage(VENUE, SYMBOL, 10.0)
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)
    result = tracker.compute(VENUE, SYMBOL)
    assert result.liquidation_price == pytest.approx(90.5)


def test_a_short_is_liquidated_above_its_entry():
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(-1.0, entry=100.0))
    tracker.set_leverage(VENUE, SYMBOL, 10.0)
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)
    assert tracker.compute(VENUE, SYMBOL).liquidation_price == pytest.approx(109.5)


def test_more_leverage_dies_closer_to_the_entry():
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0))
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)
    tracker.set_leverage(VENUE, SYMBOL, 2.0)
    gentle = tracker.compute(VENUE, SYMBOL).liquidation_price
    tracker.set_leverage(VENUE, SYMBOL, 50.0)
    steep = tracker.compute(VENUE, SYMBOL).liquidation_price
    assert steep > gentle


# A position outlives the decision that opened it, and `leverage-selector`
# answers only while an intent is being formed. Measured on the live spine at
# 15:01 on 2026-08-26: 12 open positions, 7,041 readings refused for "no
# leverage-choice for this position", `margin-liquidation-watch` stopping new risk
# on each of those symbols, and the sizer refusing most of what it saw -- while
# nothing was anywhere near liquidation.


def test_a_restored_position_is_measured_from_the_leverage_it_carries():
    """No choice has arrived this run, and one still must not be assumed."""
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0, leverage=10.0))
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)

    result = tracker.compute(VENUE, SYMBOL)
    assert result.liquidation_price == pytest.approx(90.5)
    assert tracker.standing.leverage_from_the_position_itself == 1


def test_a_live_choice_outranks_what_the_position_was_opened_at():
    """The selector is answering about now; the position is remembering."""
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0, leverage=2.0))
    tracker.set_leverage(VENUE, SYMBOL, 10.0)
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)

    assert tracker.compute(VENUE, SYMBOL).liquidation_price == pytest.approx(90.5)
    assert tracker.standing.leverage_from_the_position_itself == 0


def test_a_position_carrying_no_leverage_is_still_unmeasurable():
    """Unlevered and unrecorded are different, and only one has a price."""
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0, leverage=None))
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)

    result = tracker.compute(VENUE, SYMBOL)
    assert result.liquidation_price is None
    assert tracker.standing.without_leverage == 1


def test_an_unknown_maintenance_rate_computes_nothing():
    """A liquidation price from an assumed rate looks exactly like a real one."""
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0))
    tracker.set_leverage(VENUE, SYMBOL, 10.0)
    result = tracker.compute(VENUE, SYMBOL)
    assert result.liquidation_price is None
    assert "maintenance margin" in result.reason
    assert tracker.standing.without_margin_rate == 1


def test_distance_to_liquidation_is_measured_against_the_live_price():
    tracker = LiquidationPriceTracker()
    tracker.observe_position(position(1.0, entry=100.0))
    tracker.set_leverage(VENUE, SYMBOL, 10.0)
    tracker.set_maintenance_margin_rate(VENUE, SYMBOL, 0.005)
    tracker.observe_price(VENUE, SYMBOL, 95.0, tracker._now_ns())
    result = tracker.compute(VENUE, SYMBOL)
    assert result.distance_fraction == pytest.approx((95.0 - 90.5) / 95.0)


# ---- fund-lock-ledger --------------------------------------------------------

def test_two_orders_in_one_tick_cannot_spend_the_same_balance():
    """The failure this part exists to prevent."""
    ledger = FundLockLedger()
    ledger.set_account_balance(1000.0)
    first = ledger.lock("order-1", VENUE, SYMBOL, 700.0)
    second = ledger.lock("order-2", VENUE, SYMBOL, 700.0)
    assert first.state == LOCKED
    assert second.state == REFUSED
    assert "700" in second.reason
    assert ledger.free_balance == pytest.approx(300.0)


def test_releasing_frees_the_capital_for_the_next_order():
    ledger = FundLockLedger()
    ledger.set_account_balance(1000.0)
    ledger.lock("order-1", VENUE, SYMBOL, 700.0)
    assert ledger.release("order-1").state == RELEASED
    assert ledger.lock("order-2", VENUE, SYMBOL, 700.0).state == LOCKED


def test_locking_the_same_order_twice_holds_the_capital_once():
    ledger = FundLockLedger()
    ledger.set_account_balance(1000.0)
    ledger.lock("order-1", VENUE, SYMBOL, 400.0)
    ledger.lock("order-1", VENUE, SYMBOL, 400.0)
    assert ledger.locked_total == pytest.approx(400.0)
    assert ledger.standing.double_locks_prevented == 1


def test_releasing_an_unknown_order_is_not_an_error():
    ledger = FundLockLedger()
    ledger.set_account_balance(10.0)
    assert ledger.release("never-locked") is None


def test_a_fills_client_order_id_releases_the_lock_taken_under_its_intent_id():
    """The live bug (2026-08-30): a lock is taken under intent_id, but a fill
    only ever carries order-idempotency-stamper's client_order_id -- a SHA-256
    of that intent_id, an unrelated-looking string. Locking under one and
    releasing under the other matched nothing, ever, and free_balance only
    ever fell."""
    ledger = FundLockLedger()
    ledger.set_account_balance(1000.0)
    ledger.lock("intent-1", VENUE, SYMBOL, 700.0)
    ledger.observe_stamped_order(client_order_id="deadbeef", intent_id="intent-1")
    released = ledger.release("deadbeef")
    assert released is not None
    assert released.state == RELEASED
    assert ledger.free_balance == pytest.approx(1000.0)
    assert ledger.standing.releases_with_no_lock_to_match == 0


def test_an_untranslatable_release_is_counted_not_silent():
    ledger = FundLockLedger()
    ledger.set_account_balance(10.0)
    ledger.release("no-mapping-was-ever-learned-for-this")
    assert ledger.standing.releases_with_no_lock_to_match == 1


# ---- usdt-pnl-accountant -----------------------------------------------------

def closed_trade(realised=100.0, fees=1.0, quote="USDT"):
    from runtime.trading_types import ClosedTrade

    return ClosedTrade(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, quantity=1.0,
        entry_price=100.0, exit_price=200.0, realised_pnl=realised, fees_paid=fees,
        opened_at_ns=SECOND, closed_at_ns=3 * SECOND,
    )


def test_gross_fees_and_funding_stay_separate():
    """A strategy that loses to funding is a different problem from one with no edge."""
    accountant = UsdtPnlAccountant()
    accountant.record_funding(VENUE, SYMBOL, -5.0)
    statement = accountant.state(closed_trade(realised=100.0, fees=1.0))
    assert statement.gross_pnl_usdt == pytest.approx(100.0)
    assert statement.fees_usdt == pytest.approx(1.0)
    assert statement.funding_usdt == pytest.approx(-5.0)
    assert statement.net_pnl_usdt == pytest.approx(94.0)


def test_return_on_capital_needs_the_capital_that_was_used():
    accountant = UsdtPnlAccountant()
    statement = accountant.state(closed_trade(realised=100.0, fees=0.0))
    assert statement.return_on_capital is None
    assert accountant.standing.without_capital == 1

    accountant.set_capital_allotment(VENUE, SYMBOL, 1000.0)
    with_capital = accountant.state(closed_trade(realised=100.0, fees=0.0))
    assert with_capital.return_on_capital == pytest.approx(0.1)


def test_a_non_usdt_quote_is_converted_and_flagged():
    accountant = UsdtPnlAccountant()
    accountant.set_conversion_rate("USDC", 0.999)
    statement = accountant.state(closed_trade(realised=100.0, fees=0.0), quote_currency="USDC")
    assert statement.net_pnl_usdt == pytest.approx(99.9)
    assert statement.quote_currency == "USDC"
    assert accountant.standing.non_usdt_converted == 1


def test_funding_is_applied_once():
    accountant = UsdtPnlAccountant()
    accountant.record_funding(VENUE, SYMBOL, -5.0)
    first = accountant.state(closed_trade())
    second = accountant.state(closed_trade())
    assert first.funding_usdt == pytest.approx(-5.0)
    assert second.funding_usdt == pytest.approx(0.0)


# ---- the journal itself ------------------------------------------------------

def test_the_chain_links_every_entry_to_the_one_before():
    journal = Journal()
    first = journal.append("a", "part", {"n": 1})
    second = journal.append("b", "part", {"n": 2})
    assert first.previous_digest == GENESIS_DIGEST
    assert second.previous_digest == first.digest
    assert first.sequence == 1 and second.sequence == 2


# ---- trade-lifecycle-recorder ------------------------------------------------

def test_the_stages_of_one_trade_are_journalled_in_order():
    recorder = TradeLifecycleRecorder(Journal())
    for stage in ("entry-candidate", "trade-intent", "bounded-order", "order-request", "fill"):
        assert recorder.record(stage, "trade-1", {"stage": stage}) is not None
    assert recorder.stages_recorded_for("trade-1") == (
        "entry-candidate", "trade-intent", "bounded-order", "order-request", "fill",
    )
    assert recorder.standing.trades_filled == 1


def test_a_stage_going_backwards_is_refused():
    """A journal whose order is wrong teaches a later phase the wrong causal story."""
    recorder = TradeLifecycleRecorder(Journal())
    recorder.record("entry-candidate", "trade-1", {})
    recorder.record("order-request", "trade-1", {})
    assert recorder.record("trade-intent", "trade-1", {}) is None
    assert recorder.standing.out_of_order == 1


def test_partial_fills_may_repeat():
    recorder = TradeLifecycleRecorder(Journal())
    recorder.record("order-request", "trade-1", {})
    assert recorder.record("fill", "trade-1", {"quantity": 1}) is not None
    assert recorder.record("fill", "trade-1", {"quantity": 2}) is not None


def test_a_trade_first_seen_mid_lifecycle_is_still_recorded():
    recorder = TradeLifecycleRecorder(Journal())
    entry = recorder.record("fill", "trade-late", {})
    assert entry is not None
    assert entry.payload["note"] == "lifecycle joined in progress"


# ---- position-recorder -------------------------------------------------------

def test_only_a_real_change_is_journalled():
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    assert recorder.record_position(position(1.0)).kind == OPENED
    assert recorder.record_position(position(1.0)) is None
    assert recorder.record_position(position(2.0)).kind == CHANGED
    assert recorder.record_position(position(0.0)).kind == CLOSED
    assert recorder.standing.unchanged_skipped == 1


def test_a_closed_trade_is_journalled_with_its_excursion():
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    from runtime.trading_types import ClosedTrade

    trade = ClosedTrade(
        VENUE, SYMBOL, LONG, 1.0, 100.0, 110.0, 10.0, 0.1, SECOND, 3 * SECOND, 15.0, -2.0
    )
    entry = recorder.record_closed_trade(trade)
    assert entry.payload["best_unrealised"] == 15.0
    assert entry.payload["holding_seconds"] == pytest.approx(2.0)


def an_excursion(best=15.0, worst=-2.0, best_price=115.0, worst_price=98.0, current=1.0, samples=1):
    from parts.portfolio_state.peak_excursion_tracker import PeakExcursion

    return PeakExcursion(VENUE, SYMBOL, best, worst, best_price, worst_price, current, samples, 0)


def test_an_excursion_is_journalled_only_when_a_peak_moves_meaningfully():
    """The tracker publishes one excursion per price. Journaling each wrote four
    gigabytes in two days, and skipping only exact repeats did not hold either:
    an extreme is set by the market's newest best print, so it moves on nearly
    every trade of a trending symbol (8.59 million entries, 2026-08-24). Only a
    move past excursion_move_fraction of the last journalled price is a new
    record; the per-price path is the tape's."""
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    assert recorder.record_excursion(an_excursion()) is not None
    # The peak creeping by less than the fraction is the flood, not a record.
    assert recorder.record_excursion(an_excursion(best_price=115.1, samples=2)) is None
    assert recorder.record_excursion(an_excursion(best_price=115.2, samples=3)) is None
    # Past the fraction on either extreme is a record again.
    assert recorder.record_excursion(an_excursion(best_price=116.0, samples=4)) is not None
    assert recorder.record_excursion(an_excursion(best_price=116.0, worst_price=97.0, samples=5)) is not None
    assert recorder.standing.excursions == 3
    assert recorder.standing.excursions_unchanged_skipped == 2


def test_a_new_position_starts_its_own_extremes():
    """A reopened symbol whose first extremes happen to match the closed one's
    is still journalled -- held peaks would swallow it."""
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    recorder.record_position(position(1.0))
    assert recorder.record_excursion(an_excursion()) is not None
    recorder.record_position(position(0.0))
    recorder.record_position(position(1.0))
    assert recorder.record_excursion(an_excursion()) is not None


def a_lock_adjustment(did_move=True, new_stop=101.0, outcome="trailed"):
    return SimpleNamespace(
        venue_id=VENUE, symbol=SYMBOL, direction=LONG, entry_price=100.0,
        current_price=102.0, previous_stop=99.0, new_stop=new_stop, outcome=outcome,
        gain_fraction=0.02, locked_fraction=0.01, adjusted_at_ns=SECOND, did_move=did_move,
    )


def an_initial_exit(should_be_sent=True):
    """exit-order-chainer's shape on the same stop-adjustment wire."""
    return SimpleNamespace(
        venue_id=VENUE, symbol=SYMBOL, exit_side="sell", quantity=1.0,
        stop_price=95.0, target_price=110.0, should_be_sent=should_be_sent,
    )


def test_a_lock_that_moved_is_journalled():
    """The live bug (2026-08-30): stop-adjustment was on no recorder's consumes
    at all, so the board's trailing column had no journalled record of a lock
    ever forming and could only ever show "not built"."""
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    entry = recorder.record_stop_adjustment(a_lock_adjustment(did_move=True))
    assert entry is not None
    assert entry.payload["new_stop"] == 101.0
    assert recorder.standing.stop_adjustments == 1


def test_a_lock_that_did_not_move_is_not_journalled():
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    assert recorder.record_stop_adjustment(a_lock_adjustment(did_move=False)) is None
    assert recorder.standing.stop_adjustments_held_skipped == 1


def test_exit_order_chainers_shape_on_the_same_wire_is_not_journalled_here():
    """stop-adjustment carries two shapes; only the lock's belongs in this
    journal (parts/paper_live_trading/stop_order_manager.py's own
    read_adjustment documents why a wrong read here is dangerous)."""
    recorder = PositionRecorder(Journal(), excursion_move_fraction=0.002)
    assert recorder.record_stop_adjustment(an_initial_exit()) is None
    assert recorder.standing.stop_adjustments == 0
    assert recorder.standing.stop_adjustments_held_skipped == 0


# ---- learning-recorder -------------------------------------------------------

def test_a_forecast_backdated_after_its_subject_resolved_is_refused():
    """A claim edited after the fact is not a forecast, and would corrupt every score."""
    now = 1000 * SECOND
    recorder = LearningRecorder(Journal(), now_ns=lambda: now)
    assert recorder.record("forecast-accuracy", {"p": 0.9}, subject_resolved_at_ns=now - SECOND) is None
    assert recorder.standing.backdated_refused == 1


def test_a_forecast_made_before_its_subject_resolves_is_kept():
    now = 1000 * SECOND
    recorder = LearningRecorder(Journal(), now_ns=lambda: now)
    entry = recorder.record("forecast-accuracy", {"p": 0.9}, subject_resolved_at_ns=now + SECOND)
    assert entry is not None and entry.payload["is_forward_looking"] is True


def test_a_backward_looking_finding_needs_no_deadline():
    now = 1000 * SECOND
    recorder = LearningRecorder(Journal(), now_ns=lambda: now)
    entry = recorder.record("research-finding", {"source": "a paper"}, subject_resolved_at_ns=now - SECOND)
    assert entry is not None and entry.payload["is_forward_looking"] is False


# ---- control-recorder --------------------------------------------------------

def modification_record(after="new content"):
    """The shape `self-modification-journal` actually publishes."""
    from dataclasses import asdict

    from runtime.autonomy_types import ModificationRecord

    return asdict(
        ModificationRecord(
            record_id="mod-1",
            part_id="position-sizer",
            change="rewrote the sizer",
            before="old content",
            after=after,
            authorised_by="autonomy-policy-engine",
            envelope_level="supervised",
            took_effect_at_ns=None,
            recorded_at_ns=SECOND,
        )
    )


def test_a_self_modification_is_digested_so_it_can_be_checked():
    """An account of self-modification that cannot be checked is an account of intentions."""
    recorder = ControlRecorder(Journal())
    entry = recorder.record("modification-record", modification_record())
    assert recorder.verify_modification(entry, "new content") is True
    assert recorder.verify_modification(entry, "something else") is False


def test_a_modification_with_no_content_is_recorded_and_counted():
    recorder = ControlRecorder(Journal())
    entry = recorder.record("modification-record", modification_record(after=None))
    assert entry.payload["content_digest"] is None
    assert recorder.standing.modifications_without_content == 1
    assert recorder.verify_modification(entry, "anything") is False


def test_a_modification_keeps_every_field_the_producer_carried():
    """The rollback path needs `before`, and a recorder that re-lists fields drops it."""
    entry = ControlRecorder(Journal()).record("modification-record", modification_record())
    assert entry.payload["before"] == "old content"
    assert entry.payload["authorised_by"] == "autonomy-policy-engine"
    assert entry.payload["envelope_level"] == "supervised"


def test_a_failed_switch_is_journalled_too():
    from dataclasses import asdict

    from parts.resource_governor.gate_actuator import SwitchRecord

    recorder = ControlRecorder(Journal())
    entry = recorder.record(
        "switch-record", asdict(SwitchRecord("a-part", "off", "failed", "no cgroup", 1, None))
    )
    assert entry.payload["outcome"] == "failed"


def test_each_kind_is_counted_as_its_own_kind():
    """The defect this replaced: 5,930 recorded, and every per-kind count zero.

    Measured on the live spine on 2026-08-28 -- the journal held 3,691 policy
    rulings and 140 gate flips in its last 2 MiB alone, and the board read
    `switches 0, policy_decisions 0, modifications 0, knowledge_snapshots 0`,
    because only the generic path ever ran and it counted only the total.
    """
    from dataclasses import asdict

    from parts.resource_governor.gate_actuator import SwitchRecord
    from runtime.autonomy_types import PolicyDecision

    recorder = ControlRecorder(Journal())
    recorder.record("switch-record", asdict(SwitchRecord("a-part", "off", "flipped", "hog", 1, 2)))
    recorder.record(
        "policy-decision",
        asdict(PolicyDecision("position-sizer", "place-order", True, "supervised", "inside", SECOND)),
    )
    recorder.record("modification-record", modification_record())
    recorder.record("knowledge-snapshot", {"version": "v3", "counts": {}})

    assert recorder.standing.recorded == 4
    assert recorder.standing.switches == 1
    assert recorder.standing.policy_decisions == 1
    assert recorder.standing.modifications == 1
    assert recorder.standing.snapshots == 1


def test_a_kind_this_recorder_does_not_keep_is_refused_and_counted():
    recorder = ControlRecorder(Journal())
    assert recorder.record("market-data", {"price": 1.0}) is None
    assert recorder.standing.unknown_kinds == 1
    assert recorder.standing.recorded == 0


# ---- journal-integrity-checker ----------------------------------------------

def test_an_intact_journal_verifies():
    journal = Journal()
    for index in range(5):
        journal.append("kind", "part", {"n": index})
    assert JournalIntegrityChecker().check(journal.entries) == ()


def test_a_deleted_entry_shows_as_a_sequence_gap():
    journal = Journal()
    for index in range(5):
        journal.append("kind", "part", {"n": index})
    without_the_third = journal.entries[:2] + journal.entries[3:]
    gaps = JournalIntegrityChecker().check(without_the_third)
    assert SEQUENCE_GAP in {gap.reason for gap in gaps}


def test_an_edited_entry_shows_as_a_digest_mismatch():
    """The chain is what makes an edit visible at all."""
    import dataclasses

    journal = Journal()
    for index in range(3):
        journal.append("kind", "part", {"n": index})
    tampered = list(journal.entries)
    tampered[1] = dataclasses.replace(tampered[1], payload={"n": 999})
    reasons = {gap.reason for gap in JournalIntegrityChecker().check(tampered)}
    assert DIGEST_MISMATCH in reasons


def test_a_relinked_entry_shows_as_a_chain_break():
    import dataclasses

    journal = Journal()
    for index in range(3):
        journal.append("kind", "part", {"n": index})
    tampered = list(journal.entries)
    tampered[2] = dataclasses.replace(tampered[2], previous_digest=GENESIS_DIGEST)
    reasons = {gap.reason for gap in JournalIntegrityChecker().check(tampered)}
    assert CHAIN_BREAK in reasons


def test_an_intact_journal_is_trustworthy_and_a_broken_one_is_not():
    journal = Journal()
    for index in range(3):
        journal.append("kind", "part", {"n": index})
    checker = JournalIntegrityChecker()
    assert checker.is_trustworthy(journal.entries) is True
    # Entries removed from the front, so the journal no longer starts at 1.
    assert checker.is_trustworthy(journal.entries[1:]) is False


def test_an_empty_journal_is_trustworthy_because_it_claims_nothing():
    """Vacuous, and deliberately so: nothing recorded is not evidence of tampering."""
    assert JournalIntegrityChecker().is_trustworthy(()) is True


# ---- funding-settlement-recorder --------------------------------------------

def funding_recorder():
    recorder = FundingSettlementRecorder(Journal())
    recorder.observe_position(position(2.0, entry=100.0))
    return recorder


def test_a_long_pays_funding_when_the_rate_is_positive():
    settlement = funding_recorder().record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND)
    assert settlement.amount_quote == pytest.approx(-10.0)


def test_a_long_receives_funding_when_the_rate_is_negative():
    """The case a second sign branch got wrong before this test caught it."""
    settlement = funding_recorder().record_funding(VENUE, SYMBOL, -0.0001, 50000.0, SECOND)
    assert settlement.amount_quote == pytest.approx(10.0)


def test_a_short_takes_the_opposite_side_of_both():
    recorder = FundingSettlementRecorder(Journal())
    recorder.observe_position(position(-2.0, entry=100.0))
    assert recorder.record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND).amount_quote == pytest.approx(10.0)
    assert recorder.record_funding(VENUE, SYMBOL, -0.0001, 50000.0, 2 * SECOND).amount_quote == pytest.approx(-10.0)


def test_the_same_settlement_is_never_booked_twice():
    """Three times a day for as long as a position is held: doubling it is invisible."""
    recorder = funding_recorder()
    assert recorder.record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND) is not None
    assert recorder.record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND) is None
    assert recorder.standing.duplicates_ignored == 1
    assert recorder.standing.settlements_booked == 1


def test_funding_is_not_charged_where_nothing_was_held():
    recorder = FundingSettlementRecorder(Journal())
    assert recorder.record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND) is None
    assert recorder.standing.without_position == 1


def test_net_funding_is_received_less_paid():
    recorder = funding_recorder()
    recorder.record_funding(VENUE, SYMBOL, 0.0001, 50000.0, SECOND)
    recorder.record_funding(VENUE, SYMBOL, -0.0002, 50000.0, 2 * SECOND)
    assert recorder.net_funding == pytest.approx(10.0)


def test_a_restarted_recorder_continues_the_chain_it_already_wrote(durable_tmp_path):
    """One file, one chain, across as many restarts as the process makes.

    Before this, `Journal` started from the genesis digest whenever a recorder
    started and appended to the file the last one wrote -- so a day of running
    produced one chain per process start. Inside a run an edit is detected; a
    whole run cut out of the file left nothing behind that said it had been
    there, which is the difference between a ledger and a pile of them.
    """
    from runtime.journal import read_journal_tail

    path = durable_tmp_path / "journal.jsonl"

    def append_to(line: str) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    first_run = Journal(append_line=append_to)
    first_run.append("fill", "trade-lifecycle-recorder", {"trade_id": "one"})
    first_run.append("fill", "trade-lifecycle-recorder", {"trade_id": "one"})

    # The recorder dies and is started again against the same file.
    second_run = Journal(append_line=append_to, continues_from=read_journal_tail(path))
    carried_on = second_run.append("fill", "trade-lifecycle-recorder", {"trade_id": "two"})

    assert carried_on.sequence == 3, "the sequence restarted, so two entries share a number"
    assert carried_on.previous_digest == first_run.last_digest
    assert carried_on.previous_digest != GENESIS_DIGEST

    written = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [entry["sequence"] for entry in written] == [1, 2, 3]
    assert sum(1 for entry in written if entry["previous_digest"] == GENESIS_DIGEST) == 1, (
        "more than one entry starts a chain, so the file holds more than one ledger"
    )


def test_a_journal_starting_a_fresh_file_starts_at_genesis(durable_tmp_path):
    """Nothing to continue is not the same as something unreadable."""
    from runtime.journal import read_journal_tail

    assert read_journal_tail(durable_tmp_path / "never-written.jsonl") is None

    empty = durable_tmp_path / "empty.jsonl"
    empty.write_text("")
    assert read_journal_tail(empty) is None

    damaged = durable_tmp_path / "damaged.jsonl"
    damaged.write_text('{"sequence": 1, "digest": "a"}\nnot json at all\n')
    assert read_journal_tail(damaged) is None, (
        "a tail that could not be read must not become a digest the chain claims to follow"
    )


# ---- a position already open must survive a restart ---------------------------
#
# fill-reconciler is where a fill becomes a position, so nothing downstream of a
# fill has an input until it is running -- its own docstring says so. It held its
# positions in memory alone until 2026-08-26, and a position is learned only from
# a *fill*, which for one already open never arrives again. So every restart began
# blind to everything already held.
#
# Measured on the live spine at 11:47 on 2026-08-26, with 20 positions open and
# restored by the two portfolio parts that did checkpoint:
#
#     fill-reconciler           received nothing, published no position
#     peak-excursion-tracker    positions_tracked 0, prices_without_cost_basis
#                               162,960 -- the excursion on the board was frozen,
#                               and 16 of 20 rows contradicted their own live P&L
#                               (a "worst" of -4.91 on a position sitting at -27.27)
#     stop-order-manager        0 of 20 positions had a stop resting
#     exposure-limiter          positions_seen 0, total_exposure 0
#     margin-liquidation-watch  positions_watched 0


def test_a_held_position_comes_back_after_a_restart(durable_tmp_path):
    """Everything downstream of a fill depends on this one book being remembered."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.portfolio_state.fill_reconciler import CHECKPOINT_COMPONENT

    store = DurableStateStore(durable_tmp_path)
    settings = {"quantity_tolerance": 1e-9}
    before = FillReconciler(quantity_tolerance=1e-9)
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, before, settings
    )
    before.observe_fill(fill("f-1", BUY, 100.0, 2.0, at=5, fee=0.1))
    write(before.standing.fills_applied)

    after = FillReconciler(quantity_tolerance=1e-9)
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, after, settings
    )

    assert after.standing.restored_symbols == 1, "a restart forgot an open position"
    restored = after.reconcile_all()
    assert len(restored) == 1
    held = restored[0].position
    assert held.quantity == 2.0
    assert held.average_entry_price == 100.0
    assert held.fees_paid == 0.1
    assert held.opened_at_ns == before.reconcile_all()[0].position.opened_at_ns


def test_a_restored_position_is_republished_so_downstream_learns_of_it(durable_tmp_path):
    """The restore is only useful if the rest of the system is told.

    `tick` publishes every held position on every tick, so a restored book reaches
    the excursion tracker, the stop manager, the exposure limiter and the margin
    watch on the first tick after start -- with no extra wiring. This pins that,
    because a restore that stayed private would fix nothing anybody can see.
    """
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.portfolio_state.fill_reconciler import (
        CHECKPOINT_COMPONENT,
        run_fill_reconciler,
    )
    import parts.portfolio_state.fill_reconciler as module

    store = DurableStateStore(durable_tmp_path)
    settings = {"quantity_tolerance": 1e-9}
    before = FillReconciler(quantity_tolerance=1e-9)
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, before, settings
    )
    before.observe_fill(fill("f-1", BUY, 100.0, 2.0, at=5))
    write(before.standing.fills_applied)

    after = FillReconciler(quantity_tolerance=1e-9)
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, after, settings
    )

    published = []
    body = {}

    def capture_run_part(*, do_one_tick, **_rest):
        body["tick"] = do_one_tick
        return 0

    original = module.run_part
    module.run_part = capture_run_part
    try:
        run_fill_reconciler(
            reconciler=after,
            control_socket=None,
            # No fills at all: the position is only there because it was restored.
            read_fills_and_reports=lambda: ((), ()),
            publish_positions=published.extend,
            health_interval_seconds=1.0,
            emit_health=lambda _health: None,
        )
    finally:
        module.run_part = original

    body["tick"]()

    assert len(published) == 1, (
        "a restored position was never published, so nothing downstream could see it"
    )
    assert published[0].quantity == 2.0


def test_a_fill_already_applied_is_not_applied_twice_across_a_restart(durable_tmp_path):
    """Without the seen-fill ids, a replayed fill would double the position."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.portfolio_state.fill_reconciler import CHECKPOINT_COMPONENT

    store = DurableStateStore(durable_tmp_path)
    settings = {"quantity_tolerance": 1e-9}
    before = FillReconciler(quantity_tolerance=1e-9)
    write = restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, before, settings
    )
    before.observe_fill(fill("f-1", BUY, 100.0, 2.0, at=5))
    write(before.standing.fills_applied)

    after = FillReconciler(quantity_tolerance=1e-9)
    restore_and_arm_checkpoint(
        store, CheckpointSchedule(1), "fill-reconciler", CHECKPOINT_COMPONENT, after, settings
    )
    after.observe_fill(fill("f-1", BUY, 100.0, 2.0, at=5))   # the same fill again

    assert after.reconcile_all()[0].position.quantity == 2.0, (
        "a fill replayed across a restart doubled the position"
    )
    assert after.standing.duplicates_ignored == 1


def test_a_reconciler_that_has_never_checkpointed_says_so(durable_tmp_path):
    """Came back holding nothing, and never ran, are different facts (Rule 8)."""
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from parts.portfolio_state.fill_reconciler import CHECKPOINT_COMPONENT

    cold = FillReconciler(quantity_tolerance=1e-9)
    restore_and_arm_checkpoint(
        DurableStateStore(durable_tmp_path), CheckpointSchedule(1),
        "fill-reconciler", CHECKPOINT_COMPONENT, cold, {"quantity_tolerance": 1e-9},
    )

    assert cold.standing.checkpoint_verdict, "a cold start recorded no verdict"
    assert cold.standing.restored_symbols == 0


# The residue that held 13 round trips open -- against the numbers it actually
# left behind. Read off the live checkpoint on 2026-08-28
# (position-close-detector.positions.json), so these are fills the system
# really took rather than quantities invented to make a bound look necessary.


def test_a_close_a_hair_short_of_the_book_still_ends_the_round_trip():
    """The shape of `binance-usdm|BTCUSDT`: 0.02 in, a fraction less out.

    Before the bound, a remainder of 2.99262E-18 held that position open for
    three days against a recorded cost of 1,577 USDT. What is left here is
    1E-6 -- a thousandth of one quantity step, and still unsellable.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    assert detector.observe_fill(fill("in", BUY, 78848.37, 0.02)) is None
    trade = detector.observe_fill(fill("out", SELL, 79510.5, 0.019999, at=2))
    assert trade is not None
    assert trade.quantity == pytest.approx(0.02)
    assert detector.standing.residues_absorbed_into_the_close == 1
    assert detector.standing.open_symbols == 0


def test_a_close_leaving_more_than_a_step_is_still_only_a_partial_close():
    """The bound must not end a round trip that is still genuinely held."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("in", BUY, 78848.37, 0.02))
    assert detector.observe_fill(fill("out", SELL, 79510.5, 0.017, at=2)) is None
    assert detector.standing.partial_closes == 1
    assert detector.standing.residues_absorbed_into_the_close == 0
    assert detector.standing.open_symbols == 1


def test_a_dust_lot_left_by_an_older_build_does_not_hold_the_next_trip_open():
    """How the 13 actually formed: an overshoot became a lot, then outlived a close.

    `binance-usdm|BTCUSDT` carried `entered_quantity` of
    0.01999999999999999999262 -- twenty-three significant digits, which no single
    float fill can state. It is a sum: a real 0.02 alongside a 2.99262E-18 lot an
    earlier reversal had opened. The real close took the real quantity and the
    dust lot stayed. With the overshoot refused at the source, there is no dust
    lot to outlive anything.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("open", BUY, 78816.5, 0.5))
    detector.observe_fill(fill("through-flat", SELL, 78816.5, 0.5000004, at=2))
    assert detector.standing.open_symbols == 0
    detector.observe_fill(fill("next-in", BUY, 78848.37, 0.02, at=3))
    trade = detector.observe_fill(fill("next-out", SELL, 79510.5, 0.02, at=4))
    assert trade is not None
    assert trade.quantity == pytest.approx(0.02)
    assert detector.standing.open_symbols == 0


def test_a_reversal_overshooting_by_less_than_a_step_opens_nothing():
    """`binance-usdm|BTCUSDC` was opened by an overshoot of 4E-18 and never closed.

    A position of 4E-18 BTC cannot be sold by any order the sizer can snap, so
    it is not a position -- it is the same remainder arriving from the far side.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("in", BUY, 78816.5, 0.5))
    trade = detector.observe_fill(fill("out", SELL, 78816.5, 0.5000004, at=2))
    assert trade is not None
    assert detector.standing.reversal_overshoots_too_small_to_open == 1
    assert detector.standing.reversals == 0
    assert detector.standing.open_symbols == 0


def test_a_reversal_larger_than_a_step_still_opens_the_other_side():
    """The bound must not swallow a real reversal, which is what it would cost."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("in", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("out", SELL, 110.0, 3.0, at=2))
    assert trade is not None
    assert detector.standing.reversals == 1
    assert detector.standing.open_symbols == 1


def test_a_residue_restored_from_an_older_checkpoint_closes_and_is_published():
    """The 13 ghosts. Reconstructed from what was recorded, not from a price now.

    The exit is backed out of the entry, the quantity and the realised profit --
    the three fields the checkpoint already carried -- so the round trip that
    reaches the scorers is the one that actually happened.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    restored = detector.restore_from_checkpoint(
        {
            "books": {
                "binance-usdm|BTCUSDT": [
                    {"quantity": "2.99262E-18", "price": 78848.37479931863,
                     "opened_at_ns": 1787667948675880566, "fee": 0.0}
                ]
            },
            "direction": {"binance-usdm|BTCUSDT": LONG},
            "realised": {"binance-usdm|BTCUSDT": -1.7284959863728204},
            "fees": {"binance-usdm|BTCUSDT": 1.7337135727925046},
            "entered_quantity": {"binance-usdm|BTCUSDT": "0.02"},
            "entry_cost": {"binance-usdm|BTCUSDT": 1576.9674959863726},
            "opened_at": {"binance-usdm|BTCUSDT": 1787667948675880566},
            "excursion": {"binance-usdm|BTCUSDT": [0.6308022904596289, -1.6955977095403933]},
        }
    )
    assert restored == 0
    assert detector.standing.residues_closed_at_restore == 1

    recovered = detector.take_trades_recovered_at_restore()
    assert len(recovered) == 1
    trade = recovered[0]
    assert trade.symbol == "BTCUSDT"
    assert trade.quantity == pytest.approx(0.02)
    assert trade.entry_price == pytest.approx(78848.37479931863)
    # The stated entry and exit reproduce the realised profit that was recorded.
    assert (trade.exit_price - trade.entry_price) * trade.quantity == pytest.approx(
        -1.7284959863728204
    )
    assert trade.best_unrealised == pytest.approx(0.6308022904596289)
    # Drained once: a second tick must not publish the same round trip again.
    assert detector.take_trades_recovered_at_restore() == ()


def test_a_real_position_restored_from_a_checkpoint_is_not_closed_as_a_residue():
    """`binance-usdm|STORJUSDT` held 21,810.25 units. It must come back open."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    restored = detector.restore_from_checkpoint(
        {
            "books": {
                "binance-usdm|STORJUSDT": [
                    {"quantity": "21810.25", "price": 0.0458,
                     "opened_at_ns": 1787739774000000000, "fee": 0.4}
                ]
            },
            "direction": {"binance-usdm|STORJUSDT": LONG},
            "entered_quantity": {"binance-usdm|STORJUSDT": "21810.25"},
            "entry_cost": {"binance-usdm|STORJUSDT": 999.1},
        }
    )
    assert restored == 1
    assert detector.standing.residues_closed_at_restore == 0
    assert detector.take_trades_recovered_at_restore() == ()


def test_a_cost_basis_releases_its_side_when_only_a_residue_is_left():
    """The same remainder kept `cost-basis` publishing a side that was closed."""
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    tracker.observe_fill(fill("in", BUY, 78848.37, 0.02))
    basis = tracker.observe_fill(fill("out", SELL, 79510.5, 0.019999, at=2))
    assert basis.direction == FLAT
    assert basis.quantity == 0
    assert tracker.standing.residues_released_with_the_side == 1


def test_the_leverage_a_position_was_opened_at_is_carried_on_its_checkpoint():
    """What a position ties up is its notional over its leverage, so both travel.

    Weighted by notional across the entering fills rather than taken from the
    last one: a position built at 10x and then added to at 2x commits neither.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("a", BUY, 100.0, 10.0, leverage=10.0))
    detector.observe_fill(fill("b", BUY, 100.0, 10.0, at=2, leverage=2.0))
    state = detector.read_checkpoint_state()
    key = "binance-usdm|BTCUSDT"
    # 1,000 of notional at 10x commits 100; 1,000 at 2x commits 500.
    assert state["notional_at_leverage"][key] == pytest.approx(600.0)
    assert state["entry_cost"][key] == pytest.approx(2000.0)


def test_a_checkpoint_written_before_leverage_was_recorded_restores_as_unknown():
    """Unlevered and unknown are different claims; the second must stay unknown."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.restore_from_checkpoint(
        {
            "books": {"binance-usdm|BTCUSDT": [
                {"quantity": "1.0", "price": 100.0, "opened_at_ns": 1, "fee": 0.0}
            ]},
            "direction": {"binance-usdm|BTCUSDT": LONG},
            "entered_quantity": {"binance-usdm|BTCUSDT": "1.0"},
            "entry_cost": {"binance-usdm|BTCUSDT": 100.0},
        }
    )
    assert detector.read_checkpoint_state()["notional_at_leverage"] == {}


def test_a_cost_basis_restored_as_a_residue_is_released_without_waiting_for_a_fill():
    """Nineteen books came back this way on 2026-08-28 and no fill was ever coming."""
    tracker = CostBasisTracker(QUANTITY_INCREMENT)
    restored = tracker.restore_from_checkpoint(
        {
            "books": {
                "bybit-linear|WIFUSDT": [
                    {"quantity": "1.21E-12", "price": 0.2087, "opened_at_ns": 1, "fee": 0.0}
                ],
                "binance-usdm|STORJUSDT": [
                    {"quantity": "21810.25", "price": 0.0458, "opened_at_ns": 1, "fee": 0.4}
                ],
            },
            "direction": {"bybit-linear|WIFUSDT": LONG, "binance-usdm|STORJUSDT": LONG},
        }
    )
    assert restored == 1
    assert tracker.standing.residues_released_with_the_side == 1
    assert tracker.read("bybit-linear", "WIFUSDT").direction == FLAT
    assert tracker.read("binance-usdm", "STORJUSDT").direction == LONG


def test_a_sell_with_nothing_held_does_not_open_a_short():
    """Phase A buys options and buys shares. It never sells to open.

    `stop-order-manager` rests a stop for the whole position; the position closes
    by another path; the stale stop then fires against a flat book. Read as a new
    position that sell is a **short** -- the one position this system was built
    never to hold -- and it showed on the board as an option sold that nobody
    sold.

    Measured on the live spine 2026-09-07: eleven short positions, every one
    opened immediately after a `position-closed`. RELIANCE 1320 PE went flat and
    reopened at -10,474.62 units, exactly the resting stop's own quantity.
    """
    detector = PositionCloseDetector(QUANTITY_INCREMENT)
    detector.observe_fill(fill("f1", BUY, 24.0, 100.0, at=1))
    closed = detector.observe_fill(fill("f2", SELL, 25.0, 100.0, at=2))
    assert closed is not None, "the round trip should have closed"

    # The stale stop, arriving after the position is already flat.
    stale = detector.observe_fill(fill("f3", SELL, 23.95, 100.0, at=3))

    assert stale is None
    assert detector.standing.refused_a_sell_that_would_open_a_short == 1
    assert detector.standing.quantity_refused_as_an_unmatched_exit == pytest.approx(100.0)


def test_a_buy_with_nothing_held_still_opens_normally():
    """The guard is about selling to open and must not touch buying to open."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT)

    assert detector.observe_fill(fill("f1", BUY, 24.0, 100.0, at=1)) is None
    assert detector.standing.refused_a_sell_that_would_open_a_short == 0


def test_a_segment_that_really_sells_to_open_can_say_so():
    """The rule is an argument rather than an assumption, so a segment that does
    sell to open has somewhere to state it (RL-061)."""
    detector = PositionCloseDetector(QUANTITY_INCREMENT, may_open_a_short=True)

    detector.observe_fill(fill("f1", SELL, 24.0, 100.0, at=1))

    assert detector.standing.refused_a_sell_that_would_open_a_short == 0
