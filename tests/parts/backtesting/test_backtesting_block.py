"""The backtesting block: a machine for producing encouraging numbers, restrained.

Left alone a backtest finds an edge in a random walk. These tests are about the
restraints: gaps that are not filled in, splits that are chronological with an
embargo, fills capped by the volume that traded, ambiguity resolved adversely, costs
fitted rather than typed, and a promotion gate that refuses on six separate grounds.
"""

import importlib

import pytest

from parts.backtesting.backtest_scorer import (
    BacktestScorer, NO_TRADES as SCORER_NO_TRADES, SCORED, TOO_FEW_TRADES as SCORER_THIN,
)
from parts.backtesting.execution_cost_model import (
    ExecutionCostModel, FITTED, NOT_FITTED, NO_FEE,
)
from parts.backtesting.fill_volume_capper import (
    CAPPED, FILLED, FillVolumeCapper, NO_BAR, NO_VOLUME,
)
from parts.backtesting.historical_bar_store import (
    Bar, BUILT, HAS_GAPS, HistoricalBarStore, NOTHING_RECORDED,
)
from parts.backtesting.instruction_promotion_gate import (
    FAILED_A_FOLD, InstructionPromotionGate, LOST_TO_MULTIPLE_TESTING,
    NOT_BELIEVABLE as GATE_NOT_BELIEVABLE, NOT_REFUTED, NO_FALSIFICATION_CRITERION,
    PROMOTED, SAMPLE_TOO_SMALL, WAS_REFUTED,
)
from parts.backtesting.instruction_replayer import (
    BoundedView, InstructionReplayer, NO_BARS, NO_COST_MODEL,
    NO_TRADES as REPLAY_NO_TRADES, REPLAYED,
)
from parts.backtesting.intra_bar_fill_sequencer import (
    ADVERSE_FIRST, AMBIGUOUS, IntraBarFillSequencer, KNOWN_FROM_TICKS, NEITHER_INSIDE,
    ONLY_ONE_LEVEL_INSIDE,
)
from parts.backtesting.live_vs_replay_reconciler import (
    COSTS_ARE_UNDERSTATED, IMPACT_IS_UNDERSTATED, LATENCY_IS_UNMODELLED,
    LiveVsReplayReconciler, RECONCILED, THE_REPLAY_MISSES_TRADES,
    TOO_FEW_LIVE_TRADES,
)
from parts.backtesting.lookahead_auditor import (
    BELIEVABLE, IMPOSSIBLY_SMOOTH, LookaheadAuditor, NOT_BELIEVABLE,
)
from parts.backtesting.walk_forward_splitter import (
    SPLIT, TOO_SHORT, WalkForwardSplitter, WINDOW_HAS_GAPS,
)
from runtime.backtest_types import (
    AT_THE_NEXT_OPEN, BacktestResult, BacktestRun, BacktestTrade, BacktestVerdict,
    FITTED_ON_THE_TEST_PERIOD, HistoricalWindow, NO_COSTS, USED_THE_FUTURE,
    WalkForwardSplit,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "historical-bar-store": "parts.backtesting.historical_bar_store",
    "walk-forward-splitter": "parts.backtesting.walk_forward_splitter",
    "execution-cost-model": "parts.backtesting.execution_cost_model",
    "instruction-replayer": "parts.backtesting.instruction_replayer",
    "lookahead-auditor": "parts.backtesting.lookahead_auditor",
    "backtest-scorer": "parts.backtesting.backtest_scorer",
    "instruction-promotion-gate": "parts.backtesting.instruction_promotion_gate",
    "live-vs-replay-reconciler": "parts.backtesting.live_vs_replay_reconciler",
    "intra-bar-fill-sequencer": "parts.backtesting.intra_bar_fill_sequencer",
    "fill-volume-capper": "parts.backtesting.fill_volume_capper",
}

MINUTE_NS = 60_000_000_000
DAY_NS = 86_400_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


def a_bar(at_ns, open_price=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0):
    return Bar(
        at_ns=at_ns, open_price=open_price, high_price=high, low_price=low,
        close_price=close, volume=volume, trades=10,
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- historical-bar-store ---------------------------------------------------

def a_store(interval=60.0):
    return HistoricalBarStore(interval_seconds=interval, now_ns=Clock())


def _fill_store(store, count=10, skip=()):
    for index in range(count):
        if index in skip:
            continue
        store.store("binance-usdm", "BTCUSDT", a_bar(index * MINUTE_NS))


def test_a_gap_is_reported_and_never_filled_in():
    """A smooth series through a period the feed missed is a backtest of a fiction."""
    subject = a_store()
    _fill_store(subject, count=10, skip=(4, 5))
    outcome = subject.window("binance-usdm", "BTCUSDT", 0, 9 * MINUTE_NS)
    assert outcome.state == HAS_GAPS
    assert outcome.window.missing_bars == 2
    assert outcome.window.was_interpolated is False
    assert outcome.window.can_be_backtested is False


def test_a_complete_window_says_so():
    subject = a_store()
    _fill_store(subject, count=10)
    outcome = subject.window("binance-usdm", "BTCUSDT", 0, 9 * MINUTE_NS)
    assert outcome.state == BUILT
    assert outcome.window.can_be_backtested


def test_an_off_interval_bar_is_refused():
    subject = a_store(interval=60.0)
    assert subject.store("binance-usdm", "BTCUSDT", a_bar(37)) is False
    assert subject.standing.bars_rejected_off_interval == 1


def test_an_incoherent_bar_is_refused():
    subject = a_store()
    assert subject.store(
        "binance-usdm", "BTCUSDT", a_bar(0, open_price=100.0, high=99.0, low=101.0)
    ) is False


def test_nothing_recorded_is_not_the_market_being_flat():
    outcome = a_store().window("binance-usdm", "NEWUSDT", 0, MINUTE_NS)
    assert outcome.state == NOTHING_RECORDED
    assert "not the same as the market being flat" in outcome.reason


def test_the_store_never_interpolates():
    described = importlib.import_module(
        BLOCK_PARTS["historical-bar-store"]
    ).describe_bar_store(a_store())
    assert described["interpolates_gaps"] is False
    assert described["fetches_history_from_a_venue_endpoint"] is False


# ---- walk-forward-splitter --------------------------------------------------

def a_splitter(train=10.0, test=5.0, embargo=1.0, step=5.0):
    return WalkForwardSplitter(
        train_seconds=train * 86_400.0, test_seconds=test * 86_400.0,
        embargo_seconds=embargo * 86_400.0, step_seconds=step * 86_400.0,
        now_ns=Clock(),
    )


def a_window(days=60, missing=0, interpolated=False):
    return HistoricalWindow(
        window_id="w-1", venue_id="binance-usdm", symbol="BTCUSDT",
        interval_seconds=60.0, bars=(), first_at_ns=0, last_at_ns=days * DAY_NS,
        expected_bars=days * 1440, missing_bars=missing, gap_spans=(),
        was_interpolated=interpolated, built_at_ns=0,
    )


def test_every_split_is_chronological_with_an_embargo():
    subject = a_splitter()
    outcome = subject.split(a_window(days=60))
    assert outcome.state == SPLIT
    for split in outcome.splits:
        assert split.is_chronological
        assert split.has_an_embargo


def test_a_window_with_holes_is_refused():
    assert a_splitter().split(a_window(missing=5)).state == WINDOW_HAS_GAPS


def test_a_window_too_short_for_one_fold_is_refused():
    assert a_splitter(train=30.0, test=30.0).split(a_window(days=10)).state == TOO_SHORT


def test_folds_do_not_overlap_train_into_test():
    subject = a_splitter()
    splits = subject.split(a_window(days=60)).splits
    for split in splits:
        assert split.train_to_ns < split.test_from_ns


def test_the_splitter_produces_no_random_splits():
    described = importlib.import_module(
        BLOCK_PARTS["walk-forward-splitter"]
    ).describe_splitting(a_splitter())
    assert described["produces_random_splits"] is False
    assert described["selects_folds"] is False


# ---- execution-cost-model ---------------------------------------------------

def a_cost_model(window=50, quantile=0.8, minimum=3):
    model = ExecutionCostModel(
        window=window, quantile=quantile, minimum_observations=minimum,
        prior_half_spread_fraction=0.0005, prior_impact_coefficient=0.001,
        now_ns=Clock(),
    )
    model.observe_fee_schedule("binance-usdm", 0.0004)
    return model


def test_an_unfitted_symbol_says_so_rather_than_defaulting_quietly():
    """A plausible default cost is what makes an untested instrument look tradeable."""
    subject = a_cost_model()
    outcome = subject.estimate("binance-usdm", "NEWUSDT", 10_000.0)
    assert outcome.state == NOT_FITTED
    assert outcome.estimate.is_fitted is False


def test_costs_are_fitted_per_symbol():
    subject = a_cost_model(minimum=2)
    subject.observe_daily_volume("binance-usdm", "BTCUSDT", 1e9)
    subject.observe_daily_volume("binance-usdm", "TINYUSDT", 1e5)
    for _ in range(5):
        subject.observe_measured_costs("binance-usdm", "BTCUSDT", 0.00001, 0.00001, 0.0001)
        subject.observe_measured_costs("binance-usdm", "TINYUSDT", 0.005, 0.01, 0.01)
    liquid = subject.estimate("binance-usdm", "BTCUSDT", 10_000.0)
    thin = subject.estimate("binance-usdm", "TINYUSDT", 10_000.0)
    assert thin.estimate.fraction_of_notional > liquid.estimate.fraction_of_notional
    assert liquid.state == FITTED


def test_impact_per_unit_grows_with_the_square_root_of_size():
    """Modelling it as linear makes small trades look expensive and large ones cheap."""
    subject = a_cost_model(minimum=2)
    subject.observe_daily_volume("binance-usdm", "BTCUSDT", 1_000_000.0)
    for _ in range(5):
        subject.observe_measured_costs("binance-usdm", "BTCUSDT", 0.0001, 0.001, 0.01)
    small = subject.estimate("binance-usdm", "BTCUSDT", 10_000.0).estimate
    large = subject.estimate("binance-usdm", "BTCUSDT", 40_000.0).estimate
    small_fraction = small.expected_impact / small.notional
    large_fraction = large.expected_impact / large.notional
    # Four times the size, twice the impact per unit -- not four times.
    assert large_fraction == pytest.approx(2.0 * small_fraction)


def test_a_venue_with_no_fee_schedule_cannot_be_priced():
    subject = ExecutionCostModel(
        window=10, quantile=0.8, minimum_observations=2,
        prior_half_spread_fraction=0.0005, prior_impact_coefficient=0.001,
        now_ns=Clock(),
    )
    assert subject.estimate("unknown-venue", "BTCUSDT", 1000.0).state == NO_FEE


def test_the_model_uses_no_flat_cost():
    assert importlib.import_module(
        BLOCK_PARTS["execution-cost-model"]
    ).describe_cost_model(a_cost_model())["uses_a_flat_cost_per_trade"] is False


# ---- fill-volume-capper -----------------------------------------------------

def a_capper(cap=0.1):
    return FillVolumeCapper(participation_cap=cap, now_ns=Clock())


def test_a_fill_cannot_exceed_a_fraction_of_the_volume_that_traded():
    subject = a_capper(cap=0.1)
    subject.observe_bar("binance-usdm", "TINYUSDT", 0, volume=100.0)
    outcome = subject.cap("binance-usdm", "TINYUSDT", 0, intended=50.0)
    assert outcome.state == CAPPED
    assert outcome.size.fillable == pytest.approx(10.0)


def test_a_bar_with_no_volume_fills_nothing():
    subject = a_capper()
    subject.observe_bar("binance-usdm", "TINYUSDT", 0, volume=0.0)
    outcome = subject.cap("binance-usdm", "TINYUSDT", 0, intended=1.0)
    assert outcome.state == NO_VOLUME
    assert outcome.size.fillable == 0.0
    assert "trading against nobody" in outcome.size.reason


def test_a_small_order_in_a_liquid_bar_fills_in_full():
    subject = a_capper(cap=0.1)
    subject.observe_bar("binance-usdm", "BTCUSDT", 0, volume=10_000.0)
    assert subject.cap("binance-usdm", "BTCUSDT", 0, 1.0).state == FILLED


def test_scaling_in_is_capped_bar_by_bar():
    subject = a_capper(cap=0.1)
    for index in range(3):
        subject.observe_bar("binance-usdm", "TINYUSDT", index * MINUTE_NS, volume=100.0)
    outcomes = subject.cap_a_sequence(
        "binance-usdm", "TINYUSDT",
        [(index * MINUTE_NS, 50.0) for index in range(3)],
    )
    total = sum(outcome.size.fillable for outcome in outcomes)
    assert total == pytest.approx(30.0)


def test_a_full_participation_cap_is_refused():
    with pytest.raises(ValueError):
        FillVolumeCapper(participation_cap=1.0)


def test_no_bar_means_no_fill():
    assert a_capper().cap("binance-usdm", "BTCUSDT", 0, 1.0).state == NO_BAR


# ---- intra-bar-fill-sequencer -----------------------------------------------

def a_sequencer():
    return IntraBarFillSequencer(now_ns=Clock())


def test_ambiguity_is_resolved_adversely():
    """Assuming the favourable order is wrong by an amount that grows with volatility."""
    subject = a_sequencer()
    outcome = subject.sequence(
        "binance-usdm", "BTCUSDT", 0, a_bar(0, high=105.0, low=95.0),
        stop_price=96.0, target_price=104.0, is_long=True,
    )
    assert outcome.state == AMBIGUOUS
    assert outcome.sequence.assumption == ADVERSE_FIRST
    assert outcome.sequence.resolves_ambiguity_pessimistically


def test_the_tape_removes_the_ambiguity():
    subject = a_sequencer()
    subject.observe_ticks("binance-usdm", "BTCUSDT", 0, [100.0, 104.5, 96.0])
    outcome = subject.sequence(
        "binance-usdm", "BTCUSDT", 0, a_bar(0, high=105.0, low=95.0),
        stop_price=96.0, target_price=104.0, is_long=True,
    )
    assert outcome.state == KNOWN_FROM_TICKS
    assert outcome.sequence.order[0] == "target"
    assert outcome.sequence.is_known


def test_one_level_inside_the_bar_is_not_ambiguous():
    subject = a_sequencer()
    outcome = subject.sequence(
        "binance-usdm", "BTCUSDT", 0, a_bar(0, high=101.0, low=99.0),
        stop_price=99.5, target_price=110.0, is_long=True,
    )
    assert outcome.state == ONLY_ONE_LEVEL_INSIDE
    assert outcome.sequence.is_known


def test_neither_level_reached_is_its_own_state():
    subject = a_sequencer()
    outcome = subject.sequence(
        "binance-usdm", "BTCUSDT", 0, a_bar(0, high=101.0, low=99.0),
        stop_price=90.0, target_price=110.0, is_long=True,
    )
    assert outcome.state == NEITHER_INSIDE


def test_how_often_it_had_to_guess_is_reported():
    subject = a_sequencer()
    for index in range(4):
        subject.sequence(
            "binance-usdm", "BTCUSDT", index, a_bar(index, high=105.0, low=95.0),
            96.0, 104.0, True,
        )
    described = importlib.import_module(
        BLOCK_PARTS["intra-bar-fill-sequencer"]
    ).describe_sequencing(subject)
    assert described["ambiguous_fraction"] == 1.0
    assert described["assumes_the_favourable_order"] is False


# ---- instruction-replayer ---------------------------------------------------

def a_replayer(cost=1.0):
    replayer = InstructionReplayer(now_ns=Clock())
    replayer.install_cost_model(lambda venue, symbol, notional: cost)
    return replayer


def a_split(test_from=0, test_to=100 * MINUTE_NS):
    return WalkForwardSplit(
        split_id="s-1", fold=1, train_from_ns=0, train_to_ns=0,
        test_from_ns=test_from, test_to_ns=test_to, embargo_seconds=60.0,
    )


def a_bar_window(count=10):
    return HistoricalWindow(
        window_id="w-1", venue_id="binance-usdm", symbol="BTCUSDT",
        interval_seconds=60.0,
        bars=tuple(
            a_bar(index * MINUTE_NS, open_price=100.0 + index, high=102.0 + index,
                  low=98.0 + index, close=101.0 + index)
            for index in range(count)
        ),
        first_at_ns=0, last_at_ns=(count - 1) * MINUTE_NS, expected_bars=count,
        missing_bars=0, gap_spans=(), was_interpolated=False, built_at_ns=0,
    )


def test_a_decision_is_handed_a_view_that_cannot_reach_forward():
    subject = a_replayer()
    window = a_bar_window(5)
    view = subject.view_up_to(window.bars, 2)
    assert isinstance(view, BoundedView)
    assert len(view) == 3
    assert view.latest.at_ns == 2 * MINUTE_NS


def test_a_signal_fills_no_earlier_than_the_next_bar():
    subject = a_replayer()
    window = a_bar_window(6)
    fired = {"done": False}

    def decide(view):
        if fired["done"] or len(view) < 2:
            return None
        fired["done"] = True
        return {"side": "long", "stop": 0.0, "target": 1e9}

    outcome = subject.replay("i-1", a_split(), window, decide, quantity=1.0)
    assert outcome.run.trades
    trade = outcome.run.trades[0]
    assert trade.entry_at_ns > trade.decided_with_data_up_to_ns
    assert trade.used_the_future is False
    assert trade.fill_assumption == AT_THE_NEXT_OPEN


def test_a_rule_that_never_fires_is_a_result():
    subject = a_replayer()
    outcome = subject.replay("i-1", a_split(), a_bar_window(5), lambda view: None, 1.0)
    assert outcome.state == REPLAY_NO_TRADES
    assert "has no edge to measure" in outcome.reason


def test_a_run_without_a_cost_model_is_refused():
    subject = InstructionReplayer(now_ns=Clock())
    outcome = subject.replay("i-1", a_split(), a_bar_window(5), lambda view: None, 1.0)
    assert outcome.state == NO_COST_MODEL


def test_a_split_with_too_few_bars_is_refused():
    subject = a_replayer()
    outcome = subject.replay(
        "i-1", a_split(test_from=0, test_to=0), a_bar_window(5), lambda view: None, 1.0
    )
    assert outcome.state == NO_BARS


def test_fills_are_capped_by_the_volume_model():
    subject = a_replayer()
    subject.install_volume_cap(lambda venue, symbol, at_ns, intended: intended * 0.5)
    window = a_bar_window(6)
    fired = {"done": False}

    def decide(view):
        if fired["done"] or len(view) < 2:
            return None
        fired["done"] = True
        return {"side": "long", "stop": 0.0, "target": 1e9}

    outcome = subject.replay("i-1", a_split(), window, decide, quantity=10.0)
    assert outcome.run.trades[0].quantity == pytest.approx(5.0)
    assert subject.standing.fills_capped_by_volume == 1


# ---- lookahead-auditor ------------------------------------------------------

def an_auditor(win_rate=0.95, minimum=10):
    return LookaheadAuditor(
        impossible_win_rate=win_rate, minimum_trades_for_smoothness=minimum,
        now_ns=Clock(),
    )


def a_trade(net=1.0, entry_at=MINUTE_NS, decided_up_to=0, entry=100.0, exit_price=101.0,
            costs=0.1):
    return BacktestTrade(
        entry_at_ns=entry_at, exit_at_ns=entry_at + MINUTE_NS, side="long",
        entry_price=entry, exit_price=exit_price, quantity=1.0, gross=net + costs,
        costs=costs, net=net, fill_assumption=AT_THE_NEXT_OPEN,
        decided_with_data_up_to_ns=decided_up_to,
    )


def a_run(trades=None, costs_applied=True, out_of_sample=True):
    trades = trades if trades is not None else [a_trade(net=1.0), a_trade(net=-1.0)]
    return BacktestRun(
        run_id="run-1", instruction_id="i-1", split_id="s-1", venue_id="binance-usdm",
        symbol="BTCUSDT", trades=tuple(trades), period_from_ns=0,
        period_to_ns=100 * MINUTE_NS, fill_assumption=AT_THE_NEXT_OPEN,
        costs_applied=costs_applied, was_out_of_sample=out_of_sample, bars_seen=100,
        ran_at_ns=0,
    )


def test_a_decision_stamped_after_its_own_fill_is_fatal():
    subject = an_auditor()
    run = a_run([a_trade(entry_at=MINUTE_NS, decided_up_to=2 * MINUTE_NS)])
    outcome = subject.audit(run)
    assert outcome.state == NOT_BELIEVABLE
    assert USED_THE_FUTURE in outcome.verdict.defects
    assert outcome.verdict.has_a_fatal_defect


def test_a_run_with_no_costs_is_unrelated_to_anything_achievable():
    subject = an_auditor()
    outcome = subject.audit(a_run(costs_applied=False))
    assert NO_COSTS in outcome.verdict.defects


def test_an_in_sample_run_describes_the_period_not_the_rule():
    subject = an_auditor()
    outcome = subject.audit(a_run(out_of_sample=False))
    assert FITTED_ON_THE_TEST_PERIOD in outcome.verdict.defects


def test_an_impossibly_clean_result_is_a_bug():
    subject = an_auditor(win_rate=0.95, minimum=5)
    outcome = subject.audit(a_run([a_trade(net=1.0) for _ in range(10)]))
    assert IMPOSSIBLY_SMOOTH in outcome.verdict.defects


def test_a_fill_outside_its_bar_did_not_come_from_the_tape():
    subject = an_auditor()
    subject.observe_bar("binance-usdm", "BTCUSDT", MINUTE_NS, 99.0, 101.0)
    outcome = subject.audit(a_run([a_trade(entry=120.0)]))
    assert outcome.state == NOT_BELIEVABLE
    assert subject.standing.fills_outside_their_bar >= 1


def test_a_clean_run_is_believable():
    subject = an_auditor()
    outcome = subject.audit(a_run())
    assert outcome.state == BELIEVABLE
    assert outcome.verdict.is_believable


def test_there_is_no_partly_believable_backtest():
    assert importlib.import_module(
        BLOCK_PARTS["lookahead-auditor"]
    ).describe_auditing(an_auditor())["produces_a_partial_score"] is False


# ---- backtest-scorer --------------------------------------------------------

def a_scorer(confidence=2.0):
    return BacktestScorer(confidence_multiple=confidence, now_ns=Clock())


def test_expectancy_decides_rather_than_win_rate():
    """A 70% win rate with losses three times the size loses money."""
    subject = a_scorer()
    trades = [a_trade(net=1.0) for _ in range(7)] + [a_trade(net=-3.0) for _ in range(3)]
    outcome = subject.score(a_run(trades))
    assert outcome.result.win_rate == pytest.approx(0.7)
    assert outcome.result.expectancy < 0


def test_effective_bets_are_used_rather_than_the_trade_count():
    subject = a_scorer()
    run = a_run([a_trade(net=1.0) for _ in range(20)])
    subject.observe_effective_bets(run.run_id, 3.0)
    outcome = subject.score(run)
    assert outcome.result.effective_bets == 3.0


def test_the_required_sample_size_is_computed_and_reported():
    subject = a_scorer(confidence=2.0)
    trades = [a_trade(net=1.0), a_trade(net=-0.9), a_trade(net=1.1), a_trade(net=-0.8)]
    outcome = subject.score(a_run(trades))
    assert outcome.result.required_trades > len(trades)
    assert outcome.state == SCORER_THIN


def test_drawdown_is_peak_to_trough_on_the_path():
    subject = a_scorer()
    assert subject.worst_drawdown([1.0, 3.0, -2.0, 0.0]) == pytest.approx(5.0)


def test_a_run_with_no_trades_is_a_result_not_a_failure():
    outcome = a_scorer().score(a_run([]))
    assert outcome.state == SCORER_NO_TRADES


def test_the_scorer_does_not_headline_the_win_rate():
    assert importlib.import_module(
        BLOCK_PARTS["backtest-scorer"]
    ).describe_scoring(a_scorer())["reports_win_rate_as_the_headline"] is False


# ---- instruction-promotion-gate ---------------------------------------------

def a_gate(bar=0.01, exponent=1.0):
    return InstructionPromotionGate(
        base_expectancy_bar=bar, multiple_testing_exponent=exponent, now_ns=Clock(),
    )


def a_result(run_id="run-1", expectancy=1.0, significant=True, net=10.0):
    return BacktestResult(
        run_id=run_id, instruction_id="i-1", trades=100, net_return=net,
        gross_return=net * 1.2, cost_share=0.2, win_rate=0.55, average_win=2.0,
        average_loss=-1.0, expectancy=expectancy, worst_drawdown=3.0,
        effective_bets=100.0, is_significant=significant, required_trades=50,
        scored_at_ns=0,
    )


def _prepare(gate, folds=2, believable=True, significant=True, expectancy=1.0,
             refuted=None, criterion=True, trials=1):
    for fold in range(folds):
        result = a_result(f"run-{fold}", expectancy=expectancy, significant=significant)
        gate.observe_result(result)
        gate.observe_verdict(
            BacktestVerdict(
                run_id=result.run_id, defects=() if believable else (USED_THE_FUTURE,),
                is_believable=believable, checks_run=(), reason="", audited_at_ns=0,
            )
        )
    if refuted is not None:
        gate.observe_refutation("i-1", refuted)
    if criterion:
        gate.observe_falsification_criterion("i-1", "it stops holding in chop")
    gate.observe_trials("family-1", trials)
    return gate


def test_an_instruction_that_survives_everything_is_promoted():
    subject = _prepare(a_gate(), refuted=True)
    outcome = subject.decide("i-1", "family-1", folds_expected=2)
    assert outcome.state == PROMOTED
    assert outcome.proven.is_ready_to_trade
    assert "survived, not that it works" in outcome.proven.reason


def test_a_defective_run_stops_everything():
    subject = _prepare(a_gate(), believable=False, refuted=True)
    assert subject.decide("i-1", "family-1", 2).state == GATE_NOT_BELIEVABLE


def test_a_rule_must_hold_in_every_fold():
    subject = _prepare(a_gate(), folds=2, expectancy=1.0, refuted=True)
    assert subject.decide("i-1", "family-1", folds_expected=5).state == FAILED_A_FOLD


def test_an_insufficient_sample_is_refused():
    subject = _prepare(a_gate(), significant=False, refuted=True)
    assert subject.decide("i-1", "family-1", 2).state == SAMPLE_TOO_SMALL


def test_nothing_having_tried_to_refute_it_is_a_refusal():
    subject = _prepare(a_gate())
    outcome = subject.decide("i-1", "family-1", 2)
    assert outcome.state == NOT_REFUTED
    assert "Confirmation is available for anything" in outcome.reason


def test_a_refuted_instruction_is_refused():
    subject = _prepare(a_gate(), refuted=False)
    assert subject.decide("i-1", "family-1", 2).state == WAS_REFUTED


def test_an_instruction_with_no_falsification_criterion_could_never_be_retired():
    subject = _prepare(a_gate(), refuted=True, criterion=False)
    assert subject.decide("i-1", "family-1", 2).state == NO_FALSIFICATION_CRITERION


def test_the_bar_rises_with_the_number_of_trials():
    subject = _prepare(a_gate(bar=0.5), refuted=True, expectancy=0.9, trials=10_000)
    outcome = subject.decide("i-1", "family-1", 2)
    assert outcome.state == LOST_TO_MULTIPLE_TESTING
    assert outcome.adjusted_bar > 0.5


# ---- live-vs-replay-reconciler ----------------------------------------------

def a_reconciler(minimum=3, tolerance=0.3, size_threshold=0.5):
    return LiveVsReplayReconciler(
        minimum_live_trades=minimum, consistency_tolerance=tolerance,
        size_correlation_threshold=size_threshold, now_ns=Clock(),
    )


def _live(reconciler, gaps, notionals=None, fast=None, at_ns=0):
    notionals = notionals or [10_000.0] * len(gaps)
    fast = fast or [False] * len(gaps)
    for index, gap in enumerate(gaps):
        reconciler.observe_live_trade(
            "i-1", realised=1.0, expected=1.0 + gap, notional=notionals[index],
            was_fast_market=fast[index], at_ns=at_ns,
        )


def test_a_consistent_overstatement_points_at_the_cost_model():
    subject = a_reconciler(tolerance=0.3)
    subject.observe_backtest(a_result(net=10.0), 0, DAY_NS)
    _live(subject, [0.5, 0.52, 0.48, 0.51])
    outcome = subject.reconcile("i-1")
    assert outcome.state == RECONCILED
    assert outcome.gap.likely_cause == COSTS_ARE_UNDERSTATED
    assert outcome.gap.is_consistent


def test_a_gap_that_grows_with_size_points_at_impact():
    subject = a_reconciler(tolerance=0.01, size_threshold=0.5)
    subject.observe_backtest(a_result(net=10.0), 0, DAY_NS)
    _live(
        subject, [0.1, 0.1, 2.0, 2.2],
        notionals=[1_000.0, 1_000.0, 100_000.0, 100_000.0],
    )
    assert subject.reconcile("i-1").gap.likely_cause == IMPACT_IS_UNDERSTATED


def test_a_gap_concentrated_in_fast_markets_points_at_latency():
    subject = a_reconciler(tolerance=0.01)
    subject.observe_backtest(a_result(net=10.0), 0, DAY_NS)
    _live(
        subject, [0.05, 0.05, 3.0, 3.0],
        fast=[False, False, True, True],
    )
    assert subject.reconcile("i-1").gap.likely_cause == LATENCY_IS_UNMODELLED


def test_live_beating_the_backtest_is_not_good_news():
    subject = a_reconciler()
    subject.observe_backtest(a_result(net=1.0), 0, DAY_NS)
    _live(subject, [-1.0, -1.1, -0.9])
    outcome = subject.reconcile("i-1")
    assert outcome.gap.likely_cause == THE_REPLAY_MISSES_TRADES
    assert subject.standing.times_live_beat_the_backtest == 1
    assert importlib.import_module(
        BLOCK_PARTS["live-vs-replay-reconciler"]
    ).describe_reconciliation(subject)[
        "treats_live_beating_the_backtest_as_good_news"
    ] is False


def test_too_few_live_trades_produces_no_diagnosis():
    subject = a_reconciler(minimum=10)
    subject.observe_backtest(a_result(), 0, DAY_NS)
    _live(subject, [0.5])
    assert subject.reconcile("i-1").state == TOO_FEW_LIVE_TRADES
