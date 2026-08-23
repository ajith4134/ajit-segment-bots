"""The trade board: what it shows when there is nothing, and when there is a lie.

Rule 8 says the failing states must be reachable in the design, so they are what
these test. A board that cannot render red has not been tested against a real
failure, and a board that shows a system as quiet when its record has been edited
is worse than no board at all -- it reassures at exactly the moment attention was
required.
"""

import importlib.util
import json
import pathlib
import sys

import pytest

from runtime.journal import GENESIS_DIGEST, Journal

PROJECT = pathlib.Path(__file__).resolve().parents[2]
BUILDER = PROJECT / "dashboard" / "build_trade_board.py"


@pytest.fixture(scope="module")
def board():
    """The generator, imported by path -- `dashboard` is scripts, not a package.

    Registered in sys.modules before it is executed: `dataclass` looks its own
    module up there while it builds a class, and a module that is not registered
    fails with an AttributeError that says nothing about the cause.
    """
    specification = importlib.util.spec_from_file_location("build_trade_board", BUILDER)
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(specification.name, None)


def write_journal(path: pathlib.Path, trades: int = 1, fills_per_trade: int = 1) -> list[dict]:
    """A real journal, written by the real Journal, so the digests are real."""
    lines: list[str] = []
    journal = Journal(append_line=lines.append)
    for number in range(trades):
        trade_id = f"binance-usdm:SYMBOL{number}"
        journal.append("trade-intent", "trade-lifecycle-recorder", {"trade_id": trade_id})
        for _ in range(fills_per_trade):
            journal.append(
                "fill",
                "trade-lifecycle-recorder",
                {
                    "trade_id": trade_id,
                    "venue_id": "binance-usdm",
                    "symbol": f"SYMBOL{number}",
                    "side": "buy",
                    "quantity": 0.012,
                    "price": 77_518.1,
                    "fee": 0.186,
                },
            )
    path.write_text("\n".join(lines) + "\n")
    return [json.loads(line) for line in lines]


def test_an_empty_journal_reads_as_nothing_yet_and_never_as_healthy(board, durable_tmp_path):
    path = durable_tmp_path / "journal.jsonl"
    entries, unreadable = board.read_journal_entries(path)
    assert entries == [] and unreadable is None

    opened = board.probe_opened([], path)
    assert opened.state == board.WAITING
    assert opened.state != board.OK
    assert str(path) in opened.proof


def test_an_edited_entry_makes_the_record_fail(board, durable_tmp_path):
    """The one thing a ledger claims. If it cannot be shown false, it says nothing."""
    path = durable_tmp_path / "journal.jsonl"
    write_journal(path, trades=1, fills_per_trade=1)

    lines = path.read_text().splitlines()
    tampered = json.loads(lines[-1])
    tampered["payload"]["quantity"] = 99.0
    lines[-1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    entries, unreadable = board.read_journal_entries(path)
    result = board.probe_journal_chain(entries, unreadable, path)
    assert result.state == board.FAILING
    assert "does not hash" in result.proof


def test_a_line_that_will_not_parse_fails_rather_than_being_skipped(board, durable_tmp_path):
    """A ledger with a hole must not render as a ledger with fewer trades."""
    path = durable_tmp_path / "journal.jsonl"
    write_journal(path, trades=1)
    path.write_text(path.read_text() + "{ not json\n")

    entries, unreadable = board.read_journal_entries(path)
    assert unreadable is not None
    result = board.probe_journal_chain(entries, unreadable, path)
    assert result.state == board.FAILING


def test_a_file_holding_one_chain_per_run_is_reported_as_not_built(board, durable_tmp_path):
    """Two runs appended to one file is two ledgers, and the board says so."""
    path = durable_tmp_path / "journal.jsonl"
    write_journal(path, trades=1)
    write_journal(durable_tmp_path / "second.jsonl", trades=1)
    path.write_text(path.read_text() + (durable_tmp_path / "second.jsonl").read_text())

    entries, _unreadable = board.read_journal_entries(path)
    chains, broken = board.verify_journal_chain(entries)
    assert broken is None, "two genuine chains in one file is not an edit"
    assert chains == 2

    result = board.probe_chain_continuity(entries)
    assert result.state == board.NOT_BUILT
    assert "genesis digest" in result.proof


def test_a_trade_recorded_before_the_first_live_run_is_marked_as_a_test_s(board, durable_tmp_path):
    """The integration test runs the same fourteen parts; its fills are not trades."""
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=2)

    # The live run began between the two trades.
    boundary = entries[2]["recorded_at_ns"]
    trades = board.collect_trades(entries, live_from_ns=boundary)
    assert len(trades) == 2
    assert trades[0].is_from_a_live_run is False
    assert trades[1].is_from_a_live_run is True

    opened = board.probe_opened(trades, path)
    assert opened.state == board.OK
    assert "1 on the live run" in opened.value


def test_with_no_live_run_at_all_every_trade_is_a_test_s(board, durable_tmp_path):
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=2)

    trades = board.collect_trades(entries, live_from_ns=None)
    assert all(trade.is_from_a_live_run is False for trade in trades)
    opened = board.probe_opened(trades, path)
    assert opened.state == board.WAITING
    assert "filled by tests" in opened.value


def test_a_trade_carries_the_numbers_the_journal_recorded(board, durable_tmp_path):
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1, fills_per_trade=2)

    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.fills == 2
    assert trade.quantity == pytest.approx(0.024)
    assert trade.average_price == pytest.approx(77_518.1)
    assert trade.fees == pytest.approx(0.372)
    assert trade.stages == ["trade-intent", "fill", "fill"]
    assert trade.is_open is True


def test_nothing_running_is_not_built_rather_than_a_failure(board):
    """A spine that was never started has not failed; it is not on."""
    assert board.probe_trading_half({}).state == board.NOT_BUILT


def test_a_half_started_trading_chain_fails_and_names_what_is_missing(board):
    running = {part_id: 1 for part_id in board.TRADING_HALF[:-1]}
    result = board.probe_trading_half(running)
    assert result.state == board.FAILING
    assert board.TRADING_HALF[-1] in result.proof


def test_a_silent_tape_fails_and_a_written_one_does_not(board):
    assert board.probe_feed(None).state == board.UNMEASURED
    assert board.probe_feed(board.TAPE_SILENCE_SECONDS + 1).state == board.FAILING
    assert board.probe_feed(1.0).state == board.OK


def test_closing_is_not_built_and_the_board_measures_how_far_each_part_got(board):
    """Measured, not asserted. The first version of this claimed six parts were
    unwritten; all six had code, and what they lacked was `start_part`."""
    result = board.probe_closed(running={})
    assert result.state == board.NOT_BUILT
    for part_id, _produces in board.CLOSING_CHAIN:
        assert part_id in result.proof
    assert "no start_part" in result.proof or "startable" in result.proof

    every_part_up = {part_id: 1 for part_id, _ in board.CLOSING_CHAIN}
    assert board.probe_closed(every_part_up).state == board.OK


def test_how_far_a_part_is_built_tells_running_from_startable_from_absent(board):
    assert board.how_far_a_part_is_built("paper-fill-simulator", {"paper-fill-simulator": 1}) == (
        "running"
    )
    assert board.how_far_a_part_is_built("paper-fill-simulator", {}) == "startable"
    # A part with real code and no start_part yet: the launcher has nothing to
    # fork, which is a different state from unwritten and must read as one.
    assert board.how_far_a_part_is_built(
        "bull-position-invalidation-watcher", {}
    ) == "no start_part"
    assert board.how_far_a_part_is_built("a-part-nobody-wrote", {}) == "no module"


def test_learning_progress_is_read_from_the_model_s_own_checkpoint(board, durable_tmp_path):
    """The number on the board is the number the model restores from.

    A second count kept for the board would be free to disagree with the one the
    bot actually acts on -- and the tile would be reporting something no decision
    was made from.
    """
    result = board.probe_learning()
    assert result.state in (board.UNMEASURED, board.WAITING, board.OK)
    assert "bull-conviction-model" in result.proof


def test_a_bot_that_has_never_checkpointed_reads_as_unmeasured_not_as_untrained(board):
    """Rule 8: no file is a different fact from a file saying zero.

    A model that has not run since checkpointing existed and one that has run and
    learned nothing are different states, and neither of them is healthy.
    """
    result = board.probe_learning()
    checkpoint = board.pathlib.Path(
        str(board.load_settings_document(
            board.settings_directory() / "runtime.toml", "runtime"
        ).read_value("learned_state_root"))
    ).expanduser() / "bull-conviction-model.conviction.json"
    if checkpoint.exists():
        assert result.state in (board.WAITING, board.OK)
    else:
        assert result.state == board.UNMEASURED
        assert "has not checkpointed" in result.value


def test_the_table_carries_the_numbers_an_open_position_is_judged_by(board, durable_tmp_path):
    """Entry, the price now with its age, the peak, the worst and the profit."""
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1)
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    trade.prices = board.PriceWindow(
        last_price=78_000.0,
        read_at_ns=int(__import__("time").time() * 1e9),
        highest=79_000.0,
        lowest=77_000.0,
        trades_seen=4_919,
    )

    # entry 77,518.10 on 0.012, fee 0.186: the peak is the high, the worst the low.
    assert trade.peak_profit == pytest.approx(0.012 * (79_000.0 - 77_518.1) - 0.186)
    assert trade.worst_loss == pytest.approx(0.012 * (77_000.0 - 77_518.1) - 0.186)
    assert trade.profit_now == pytest.approx(0.012 * (78_000.0 - 77_518.1) - 0.186)

    rendered = board.render_trades([trade], path)
    assert "78,000.00" in rendered and "4,919 trades" in rendered
    assert "s old" in rendered, "a price with no age is a number the reader must trust"
    assert "not built" in rendered, "the columns no part fills yet must say so"
    assert "test run" in rendered

    empty = board.render_trades([], path)
    assert "No position is open" in empty
    assert str(path) in empty


def test_a_short_is_worth_the_distance_the_price_fell(board, durable_tmp_path):
    """Signed by the side, or every short reads as a loss when it is winning."""
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1)
    for entry in entries:
        entry["payload"]["side"] = "sell"
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    trade.prices = board.PriceWindow(
        last_price=77_000.0, read_at_ns=1, highest=79_000.0, lowest=77_000.0, trades_seen=10
    )
    assert trade.is_long is False
    assert trade.profit_now == pytest.approx(0.012 * (77_518.1 - 77_000.0) - 0.186)
    assert trade.peak_profit > 0, "the price fell, which is a short in profit"
    assert trade.worst_loss < trade.peak_profit


def test_a_stale_price_says_so_in_the_cell(board):
    """The page is generated then published; the age is what keeps it honest."""
    fresh = board.PriceWindow(
        last_price=1.0,
        read_at_ns=int(__import__("time").time() * 1e9),
        highest=1.0,
        lowest=1.0,
        trades_seen=1,
    )
    assert "stale" not in board.price_cell(fresh)

    old = board.PriceWindow(
        last_price=1.0,
        read_at_ns=int((__import__("time").time() - board.TAPE_SILENCE_SECONDS * 10) * 1e9),
        highest=1.0,
        lowest=1.0,
        trades_seen=1,
    )
    assert "stale" in board.price_cell(old)
    assert board.price_cell(None).count("no tape") == 1


def test_a_decision_that_never_filled_is_counted_rather_than_listed(board, durable_tmp_path):
    """The table is about money at risk; 128 unfilled intents would bury it."""
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1)
    unfilled = [entry for entry in entries if entry["kind"] != "fill"]
    trades = board.collect_trades(unfilled, live_from_ns=None)
    assert trades and board.trades_worth_listing(trades) == []
    assert "never filled" in board.compose_listing_note(trades)


def test_the_generated_page_is_self_contained_and_theme_aware(board):
    """It is published as an artifact, where an external request is blocked.

    The template is filled with stand-ins rather than by running every probe:
    what is under test is the page, and building it for real walks a tape with
    millions of records on it.
    """
    page = board.PAGE.format(
        verdict="nothing yet",
        measured_at="2026-08-22 18:00:00 UTC",
        probe_count=9,
        journal_path="/tmp/journal.jsonl",
        tiles="",
        listed_note="",
        trades="",
        closed_note="",
        closed_trades="",
        live_from="2026-08-22 17:37:10",
    )
    assert "<title>" in page
    assert "prefers-color-scheme" in page
    assert 'data-theme="dark"' in page
    assert "http://" not in page and "https://" not in page
    assert GENESIS_DIGEST not in page, "a digest on the page is noise, not evidence"


def test_two_trades_on_one_symbol_are_two_trades(board, durable_tmp_path):
    """A trade id is not unique over time, and grouping on it alone merges them.

    Before the stamper gives an order its identity the recorder correlates stages
    by venue and symbol, so every trade ever made on one symbol shares an id. Left
    merged, a candidate noticed this minute joins a fill from a test hours ago and
    the board reports a live trade that never happened -- which it did, once, on
    the run that found this.
    """
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1, fills_per_trade=1)
    later = write_journal(durable_tmp_path / "later.jsonl", trades=1, fills_per_trade=1)
    for entry in later:
        entry["recorded_at_ns"] = entry["recorded_at_ns"] + 1_000_000_000

    trades = board.collect_trades(entries + later, live_from_ns=later[0]["recorded_at_ns"])
    assert len(trades) == 2, "the two trades on one symbol were merged into one"
    assert trades[0].is_from_a_live_run is False
    assert trades[1].is_from_a_live_run is True
    assert all(trade.fills == 1 for trade in trades)


def test_a_partial_fill_does_not_split_a_trade_in_two(board, durable_tmp_path):
    """A fill may arrive more than once for one order; that is what partial means."""
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=1, fills_per_trade=3)
    trades = board.collect_trades(entries, live_from_ns=None)
    assert len(trades) == 1
    assert trades[0].fills == 3


def test_a_candidate_alone_is_not_a_trade_worth_listing(board, durable_tmp_path):
    """2 609 setups noticed with no order behind them would bury the real rows."""
    noticed = {
        "sequence": 1,
        "kind": "entry-candidate",
        "part_id": "trade-lifecycle-recorder",
        "payload": {"trade_id": "bybit-linear:BEATUSDT", "symbol": "BEATUSDT"},
        "previous_digest": GENESIS_DIGEST,
        "digest": "unchecked here",
        "recorded_at_ns": 1,
    }
    trades = board.collect_trades([noticed], live_from_ns=None)
    assert len(trades) == 1
    assert board.trades_worth_listing(trades) == []


# ---- closed trades, and what an open position has in it -----------------------

def a_closed_trade_entry(**overrides):
    """One `closed-trade` entry as position-recorder writes it."""
    payload = dict(
        venue_id="binance-usdm", symbol="BTCUSDT", direction="long", quantity=0.01,
        entry_price=77419.2, exit_price=77480.0, realised_pnl=0.608, fees_paid=0.62,
        holding_seconds=41.0, best_unrealised=0.7, worst_unrealised=-0.2,
        opened_at_ns=1_787_000_000_000_000_000, closed_at_ns=1_787_000_041_000_000_000,
    )
    payload.update(overrides)
    return {"kind": "closed-trade", "payload": payload,
            "recorded_at_ns": payload["closed_at_ns"]}


def test_a_closed_trade_is_read_from_the_ledger_not_recomputed(board):
    """The close detector resolved the lots; the board reports what it decided.

    Recomputing it here would be a second implementation of the same arithmetic,
    free to disagree with the one the system actually acted on.
    """
    closed = board.collect_closed_trades([a_closed_trade_entry()])
    assert len(closed) == 1
    trade = closed[0]
    assert trade.direction == "long"
    assert trade.exit_price == 77480.0
    assert trade.capital_in_quote == pytest.approx(774.192)
    assert trade.net_pnl == pytest.approx(0.608 - 0.62)


def test_an_empty_closed_table_says_nothing_has_closed_rather_than_nothing_is_built(board):
    """Rule 8: absence of a close is its own state, and it is not a missing part."""
    assert board.collect_closed_trades([]) == []
    note = board.compose_closed_note([])
    assert "Nothing has closed yet" in note
    assert "unbuilt" in note
    assert "NOTHING YET" in board.render_closed_trades([])


def test_the_closed_table_says_what_the_round_trips_made_after_fees(board):
    entries = [
        a_closed_trade_entry(realised_pnl=2.0, fees_paid=0.5),
        a_closed_trade_entry(realised_pnl=-1.0, fees_paid=0.5,
                             closed_at_ns=1_787_000_100_000_000_000),
    ]
    closed = board.collect_closed_trades(entries)
    note = board.compose_closed_note(closed)
    assert "2 round trip(s)" in note
    assert "1 of them profitable" in note
    rendered = board.render_closed_trades(closed)
    assert "capital in (usdt)" in rendered
    assert "held for" in rendered


def test_an_exit_fill_reduces_the_position_rather_than_adding_to_it(board):
    """A trade's fills are not all entries once positions can close.

    An exit arrives as a fill on the opposite side, and adding it would report a
    closed position as twice the size it ever was -- and as still open.
    """
    entries = [
        {"kind": "entry-candidate", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 1},
        {"kind": "trade-intent", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 2},
        {"kind": "bounded-order", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 3},
        {"kind": "order-request", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 4},
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 0.01, "price": 77419.2, "fee": 0.31},
         "recorded_at_ns": 5},
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "sell",
                                     "quantity": 0.01, "price": 77480.0, "fee": 0.31},
         "recorded_at_ns": 6},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.fills == 2
    assert trade.quantity == pytest.approx(0.0)
    assert trade.is_open is False, "a position sold back is closed, and the fills say so"
    assert trade.exit_price == pytest.approx(77480.0)


def test_an_open_position_says_what_capital_went_into_it(board):
    """Operator, 2026-08-23: the USDT in each open trade, on the table."""
    entries = [
        {"kind": "entry-candidate", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 1},
        {"kind": "trade-intent", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 2},
        {"kind": "bounded-order", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 3},
        {"kind": "order-request", "payload": {"trade_id": "t1", "symbol": "BTCUSDT"},
         "recorded_at_ns": 4},
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 0.01, "price": 77419.2, "fee": 0.31},
         "recorded_at_ns": 5},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.is_open is True
    assert trade.capital_in_quote == pytest.approx(774.192)
    assert "capital in (usdt)" in board.render_trades([trade], pathlib.Path("/tmp/journal.jsonl"))


# ---- the wait that used to be invisible --------------------------------------

def test_the_exit_plan_tile_reports_the_closest_symbol_not_the_total(board, monkeypatch):
    """The gate is per symbol and per side, so the total is not the wait.

    Thirty symbols with four settled claims each is a total of a hundred and
    twenty and an exit plan for nobody. A tile showing the total would read as
    nearly-there while the wait had barely started (Rule 8).
    """
    monkeypatch.setattr(board, "read_learned_checkpoint", lambda name: (
        {
            "saved_at_ns": 1_787_460_000_000_000_000,
            "state": {"adverse": {f"binance-usdm|SYM{index}|long": [0.001] * 4 for index in range(30)}},
        },
        "/tmp/checkpoint.json",
    ))
    result = board.probe_exit_plans()
    assert result.state == board.WAITING
    assert "4 of" in result.value
    assert "the total across all of them is not the wait" in result.proof


def test_the_exit_plan_tile_turns_green_only_when_a_symbol_can_actually_be_planned(
    board, monkeypatch
):
    needed = int(board.load_settings_document(
        board.settings_directory() / "runtime.toml", "runtime"
    ).read_value("signal_excursion_minimum_claims"))
    monkeypatch.setattr(board, "read_learned_checkpoint", lambda name: (
        {
            "saved_at_ns": 1_787_460_000_000_000_000,
            "state": {"adverse": {"binance-usdm|BTCUSDT|long": [0.001] * needed}},
        },
        "/tmp/checkpoint.json",
    ))
    result = board.probe_exit_plans()
    assert result.state == board.OK
    assert "binance-usdm|BTCUSDT|long" in result.proof


def test_a_profiler_that_has_never_run_reads_as_unmeasured(board, monkeypatch):
    monkeypatch.setattr(board, "read_learned_checkpoint", lambda name: (None, "/tmp/missing.json"))
    result = board.probe_exit_plans()
    assert result.state == board.UNMEASURED
    assert "has not checkpointed" in result.value


def test_no_claim_having_come_right_is_its_own_state(board, monkeypatch):
    """Only claims that came right are measured, so an empty profile is expected.

    It must not read as a broken part: how far price runs when a call was wrong is
    unbounded, and a stop placed beyond it would make every loss the worst one.
    """
    monkeypatch.setattr(board, "read_learned_checkpoint", lambda name: (
        {"saved_at_ns": 1_787_460_000_000_000_000, "state": {"adverse": {}}},
        "/tmp/checkpoint.json",
    ))
    result = board.probe_exit_plans()
    assert result.state == board.WAITING
    assert "no claim has come right yet" in result.value
