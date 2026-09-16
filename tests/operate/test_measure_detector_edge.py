"""The detector-edge measurement scores real trades and never scores what it fitted on.

Real data only (RL-063): one past session, 2026-09-04, NIFTY and its eight contracts
from Upstox's history and expired-instruments routes, cached after the first run.
"""

from __future__ import annotations

import datetime

import pytest

from operate.historical_prints import NoBrokerToken, upstox_access_token
from operate.measure_detector_edge import DetectorEdgeRun, walk_forward
from operate.past_session_prints import past_session_instruments

DAY = "2026-09-04"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def session_result():
    try:
        upstox_access_token()
    except NoBrokerToken as missing:
        pytest.skip(str(missing))
    instruments = past_session_instruments(DAY, ("NIFTY",), 8, 1)
    run = DetectorEdgeRun()
    return instruments, run, run.run_session(DAY, instruments)


def test_a_real_session_raises_candidates_and_scores_trades(session_result):
    instruments, run, scored = session_result
    assert sum(run.fired.values()) > 0, dict(run.refused)
    assert scored, f"fired {dict(run.fired)} but scored nothing: {dict(run.refused)}"


def test_every_trade_buys_a_contract_held_that_session_and_is_held_forward(session_result):
    instruments, _run, scored = session_result
    held = {i.trading_symbol for i in instruments if i.option_type is not None}
    for trade in scored:
        assert trade.bought in held
        assert trade.held_seconds > 0
        assert trade.entry_price > 0 and trade.quantity > 0


def test_costs_are_charged_on_every_trade(session_result):
    _instruments, _run, scored = session_result
    for trade in scored:
        gross = (trade.exit_price - trade.entry_price) / trade.entry_price
        assert trade.fees > 0 and trade.spread_cost > 0
        assert trade.net_return < gross


def test_no_trade_is_entered_on_a_close_that_had_not_printed(session_result):
    """A bar's close exists only when the bar ends. Every entry price must come from a
    bar that had ended by the moment of entry."""
    instruments, run, scored = session_result
    interval = run.chain.kline_windows.interval_ns
    starts = {start for i in instruments for start, _ in i.prints}
    assert scored
    for trade in scored:
        assert trade.entry_price_known_at_ns <= trade.entered_at_ns
        assert trade.entry_price_known_at_ns - interval in starts


def test_the_walk_forward_never_scores_a_session_it_chose_on(session_result):
    """Two real sessions, the second scored by what the first chose."""
    import dataclasses

    _instruments, run, first = session_result
    earlier_day = "2026-09-03"
    earlier = run.run_session(earlier_day, past_session_instruments(earlier_day, ("NIFTY",), 8, 1))
    rows = [dataclasses.asdict(trade) for trade in (*earlier, *first)]
    assert {row["session"] for row in rows} == {earlier_day, DAY}
    result = walk_forward(rows, train_fraction=0.5, minimum_trades=1, confidence_level=0.95)
    assert result["train_sessions"] == [earlier_day]
    assert result["test_sessions"] == [DAY]
    for entry in result["kept"].values():
        assert entry["train"]["sessions"] == 1
