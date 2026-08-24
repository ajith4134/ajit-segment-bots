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


def fill(fill_id, side, price, quantity, at=1, fee=0.0):
    return Fill(
        fill_id=fill_id, venue_id=VENUE, symbol=SYMBOL, side=side,
        price=price, quantity=quantity, fee=fee, filled_at_ns=at * SECOND,
    )


def position(quantity, entry=100.0, at=1):
    return Position(
        venue_id=VENUE, symbol=SYMBOL, quantity=quantity, average_entry_price=entry,
        realised_pnl=0.0, fees_paid=0.0, opened_at_ns=at * SECOND, updated_at_ns=at * SECOND,
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- cost-basis-tracker ------------------------------------------------------

def test_the_average_is_weighted_by_quantity_not_by_fill_count():
    """1 at 100 and 3 at 200 averages 175, not 150."""
    tracker = CostBasisTracker()
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    basis = tracker.observe_fill(fill("f2", BUY, 200.0, 3.0))
    assert basis.quantity == 4.0
    assert basis.average_price == pytest.approx(175.0)


def test_a_repeated_fill_id_does_not_move_the_basis():
    tracker = CostBasisTracker()
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    basis = tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    assert basis.quantity == 1.0
    assert tracker.standing.duplicates_ignored == 1


def test_a_partial_close_leaves_the_oldest_lots_consumed_first():
    tracker = CostBasisTracker()
    tracker.observe_fill(fill("f1", BUY, 100.0, 1.0))
    tracker.observe_fill(fill("f2", BUY, 200.0, 1.0))
    basis = tracker.observe_fill(fill("f3", SELL, 300.0, 1.0))
    # The 100 lot went; what remains was bought at 200.
    assert basis.quantity == 1.0
    assert basis.average_price == pytest.approx(200.0)


def test_a_fill_through_flat_opens_the_other_side_at_its_own_price():
    """A long of 1 hit by a sell of 3 is a short of 2 at the sell price."""
    tracker = CostBasisTracker()
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
    detector = PositionCloseDetector()
    detector.observe_fill(fill("f1", BUY, 100.0, 2.0))
    assert detector.observe_fill(fill("f2", SELL, 150.0, 1.0)) is None
    assert detector.standing.partial_closes == 1


def test_reaching_flat_emits_the_round_trip():
    detector = PositionCloseDetector()
    detector.observe_fill(fill("f1", BUY, 100.0, 2.0, at=1))
    detector.observe_fill(fill("f2", SELL, 150.0, 1.0, at=2))
    trade = detector.observe_fill(fill("f3", SELL, 150.0, 1.0, at=3))
    assert trade is not None
    assert trade.direction == LONG
    assert trade.realised_pnl == pytest.approx(100.0)
    assert trade.holding_seconds == pytest.approx(2.0)


def test_closing_fills_match_the_oldest_lots_first():
    """Bought at 100 then 200, sold once at 150: the 100 lot is what closed."""
    detector = PositionCloseDetector()
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    detector.observe_fill(fill("f2", BUY, 200.0, 1.0))
    assert detector.observe_fill(fill("f3", SELL, 150.0, 1.0)) is None
    trade = detector.observe_fill(fill("f4", SELL, 150.0, 1.0))
    # +50 on the first lot, -50 on the second: flat overall.
    assert trade.realised_pnl == pytest.approx(0.0)


def test_a_reversal_closes_one_trade_and_opens_another():
    detector = PositionCloseDetector()
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 120.0, 3.0))
    assert trade.realised_pnl == pytest.approx(20.0)
    assert detector.standing.reversals == 1
    # The new short of 2 is open, and closing it emits its own trade.
    second = detector.observe_fill(fill("f3", BUY, 110.0, 2.0))
    assert second.direction == SHORT
    assert second.realised_pnl == pytest.approx(20.0)


def test_a_closed_trade_carries_its_excursion():
    detector = PositionCloseDetector()
    detector.observe_excursion(VENUE, SYMBOL, best=75.0, worst=-10.0)
    detector.observe_fill(fill("f1", BUY, 100.0, 1.0))
    trade = detector.observe_fill(fill("f2", SELL, 110.0, 1.0))
    assert trade.best_unrealised == 75.0 and trade.worst_unrealised == -10.0


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
    recorder = PositionRecorder(Journal())
    assert recorder.record_position(position(1.0)).kind == OPENED
    assert recorder.record_position(position(1.0)) is None
    assert recorder.record_position(position(2.0)).kind == CHANGED
    assert recorder.record_position(position(0.0)).kind == CLOSED
    assert recorder.standing.unchanged_skipped == 1


def test_a_closed_trade_is_journalled_with_its_excursion():
    recorder = PositionRecorder(Journal())
    from runtime.trading_types import ClosedTrade

    trade = ClosedTrade(
        VENUE, SYMBOL, LONG, 1.0, 100.0, 110.0, 10.0, 0.1, SECOND, 3 * SECOND, 15.0, -2.0
    )
    entry = recorder.record_closed_trade(trade)
    assert entry.payload["best_unrealised"] == 15.0
    assert entry.payload["holding_seconds"] == pytest.approx(2.0)


def an_excursion(best=15.0, worst=-2.0, current=1.0, samples=1):
    from parts.portfolio_state.peak_excursion_tracker import PeakExcursion

    return PeakExcursion(VENUE, SYMBOL, best, worst, 115.0, 98.0, current, samples, 0)


def test_an_excursion_is_journalled_only_when_a_peak_moves():
    """The tracker publishes one excursion per price; journaling each wrote four
    gigabytes of identical extremes in two days (2026-08-24). The extremes are
    the record; the per-price path is the tape's."""
    recorder = PositionRecorder(Journal())
    assert recorder.record_excursion(an_excursion()) is not None
    assert recorder.record_excursion(an_excursion(current=2.0, samples=2)) is None
    assert recorder.record_excursion(an_excursion(current=3.0, samples=3)) is None
    assert recorder.record_excursion(an_excursion(best=20.0, samples=4)) is not None
    assert recorder.record_excursion(an_excursion(best=20.0, worst=-5.0, samples=5)) is not None
    assert recorder.standing.excursions == 3
    assert recorder.standing.excursions_unchanged_skipped == 2


def test_a_new_position_starts_its_own_extremes():
    """A reopened symbol whose first extremes happen to match the closed one's
    is still journalled -- held peaks would swallow it."""
    recorder = PositionRecorder(Journal())
    recorder.record_position(position(1.0))
    assert recorder.record_excursion(an_excursion()) is not None
    recorder.record_position(position(0.0))
    recorder.record_position(position(1.0))
    assert recorder.record_excursion(an_excursion()) is not None


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

def test_a_self_modification_is_digested_so_it_can_be_checked():
    """An account of self-modification that cannot be checked is an account of intentions."""
    recorder = ControlRecorder(Journal())
    entry = recorder.record_modification("parts/x.py", "rewrote the sizer", "new content", "part-author")
    assert recorder.verify_modification(entry, "new content") is True
    assert recorder.verify_modification(entry, "something else") is False


def test_a_modification_with_no_content_is_recorded_and_counted():
    recorder = ControlRecorder(Journal())
    entry = recorder.record_modification("parts/x.py", "changed something", None, "part-author")
    assert entry.payload["content_digest"] is None
    assert recorder.standing.modifications_without_content == 1
    assert recorder.verify_modification(entry, "anything") is False


def test_a_failed_switch_is_journalled_too():
    from parts.resource_governor.gate_actuator import SwitchRecord

    recorder = ControlRecorder(Journal())
    entry = recorder.record_switch(SwitchRecord("a-part", "off", "failed", "no cgroup", 1, None))
    assert entry.payload["outcome"] == "failed"


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
    assert carried_on.previous_digest == first_run.entries[-1].digest
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
