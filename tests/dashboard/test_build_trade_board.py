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
    # Verified per file, because a chain is a property of one writer: two
    # recorders appending to one path interleave into no chain at all, which is
    # what broke this tile on 2026-08-23.
    result = board.probe_journal_chain({path: (entries, unreadable)}, unreadable, path)
    assert result.state == board.FAILING
    assert "does not hash" in result.proof
    assert path.name in result.proof, "a broken chain must name the file it is in"


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
    # Found rather than named, because parts gain start_part as they are built
    # and a named example stopped being one on 2026-08-23.
    from runtime.part_launcher import PART_ENTRY_POINT, PartHasNoModule, resolve_part_module
    from runtime.wiring_plan import load_blueprint
    import importlib

    def has_real_code_but_no_start_part(feature_id: str) -> bool:
        # A DECLARED-only part (no module at all, e.g. one just added to the
        # blueprint ahead of its implementation) is a different state from
        # this search's target and must be skipped, not let a
        # PartHasNoModule from resolve_part_module end the search early --
        # found 2026-09-01 when expiry-day-zero-to-hero-detector landed
        # DECLARED and this generator, which never caught the exception,
        # crashed on the first one it met instead of skipping past it.
        try:
            module = importlib.import_module(resolve_part_module(feature_id))
        except PartHasNoModule:
            return False
        return not callable(getattr(module, PART_ENTRY_POINT, None))

    unlaunchable = next(
        (feature["id"] for feature in load_blueprint()["features"]
         if has_real_code_but_no_start_part(feature["id"])),
        None,
    )
    if unlaunchable is not None:
        assert board.how_far_a_part_is_built(unlaunchable, {}) == "no start_part"
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


def test_capital_in_quote_is_the_margin_posted_not_the_bare_notional(board):
    """The live bug (2026-08-30): showing bare notional read as a trade
    exceeding its own configured maximum_capital_per_trade whenever it was
    levered at all, when the capital actually committed was correctly inside
    it."""
    entries = [
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 0.1, "price": 1000.0, "fee": 0.1,
                                     "leverage": 10.0},
         "recorded_at_ns": 1},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.notional == pytest.approx(100.0)
    assert trade.capital_in_quote == pytest.approx(10.0)


def test_no_leverage_named_reads_as_unlevered(board):
    entries = [
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 1.0, "price": 100.0, "fee": 0.0},
         "recorded_at_ns": 1},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.capital_in_quote == pytest.approx(100.0)


def test_a_position_with_no_lock_yet_says_so_honestly(board):
    entries = [
        {"kind": "fill", "payload": {"trade_id": "t1", "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 1.0, "price": 100.0, "fee": 0.0},
         "recorded_at_ns": 1},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.trailing_new_stop is None
    cell = board.trailing_cell(trade, running={})
    assert "not built" in cell or "none recorded" in cell


def test_a_moved_lock_shows_the_real_stop_not_a_placeholder(board):
    """The live bug (2026-08-30): stop-adjustment was never journalled by
    anything, so this column could only ever render "not built", regardless
    of whether profit-lock had actually trailed a stop."""
    entries = [
        {"kind": "fill", "payload": {"trade_id": "t1", "venue_id": "binance-usdm",
                                     "symbol": "BTCUSDT", "side": "buy",
                                     "quantity": 1.0, "price": 100.0, "fee": 0.0},
         "recorded_at_ns": 1},
        {"kind": "stop-adjustment",
         "payload": {"venue_id": "binance-usdm", "symbol": "BTCUSDT", "new_stop": 101.5,
                     "locked_fraction": 0.4, "outcome": "trailed"},
         "recorded_at_ns": 2},
    ]
    trade = board.collect_trades(entries, live_from_ns=None)[0]
    assert trade.trailing_new_stop == pytest.approx(101.5)
    cell = board.trailing_cell(trade, running={})
    assert "101.5" in cell
    assert "40.0%" in cell


def test_only_the_latest_lock_for_a_symbol_is_kept(board):
    trail = board.LatestTrail()
    trail.observe({"kind": "stop-adjustment",
                   "payload": {"venue_id": "v", "symbol": "s", "new_stop": 1.0}})
    trail.observe({"kind": "stop-adjustment",
                   "payload": {"venue_id": "v", "symbol": "s", "new_stop": 2.0}})
    assert trail.for_position("v", "s")["new_stop"] == 2.0


def test_the_other_shape_on_the_same_wire_is_ignored_by_the_scan(board):
    """exit-order-chainer's initial exits carry no new_stop; this scan is only
    ever fed real journal payload dicts (not the two dataclasses directly), so
    it is naturally indifferent to which shape produced a given entry -- but a
    payload missing venue_id/symbol must not raise or attach anything."""
    trail = board.LatestTrail()
    trail.observe({"kind": "stop-adjustment", "payload": {"exit_side": "sell"}})
    assert trail.for_position(None, None) is None


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


# ---- the gap between what the bot thought and what it got ----------------------

def test_decision_freshness_measures_the_decision_price_against_the_fill(board):
    """The measurement that was invisible, and the one that mattered most.

    On the live run of 2026-08-23 a trade was decided at an ENAUSDT price of
    0.17019 -- the real market fifty-six minutes earlier -- and filled at 0.18043.
    Both numbers were already on the ledger; nothing read them against each other.
    """
    entries = [
        {"kind": "bounded-order", "recorded_at_ns": 1,
         "payload": {"venue_id": "binance-usdm", "symbol": "ENAUSDT", "entry_price": 0.17019,
                     "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 2,
         "payload": {"venue_id": "binance-usdm", "symbol": "ENAUSDT", "price": 0.18043,
                     "quantity": 1.0, "side": "buy", "trade_id": "t-1"}},
    ]
    result = board.probe_decision_freshness(entries)
    assert result.state == board.FAILING
    assert "ENAUSDT" in result.proof
    assert "0.17019" in result.proof and "0.18043" in result.proof


def test_a_decision_that_filled_where_it_expected_reads_ok(board):
    entries = [
        {"kind": "bounded-order", "recorded_at_ns": 1,
         "payload": {"venue_id": "binance-usdm", "symbol": "BTCUSDT", "entry_price": 77_500.0,
                     "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 2,
         "payload": {"venue_id": "binance-usdm", "symbol": "BTCUSDT", "price": 77_512.0,
                     "quantity": 0.01, "side": "buy", "trade_id": "t-1"}},
    ]
    result = board.probe_decision_freshness(entries)
    assert result.state == board.OK


def test_an_exit_fill_is_not_measured_against_the_entry_it_closes(board):
    """A closing fill lands wherever the trade went. That is the trade's result.

    Measured against the entry, a winning trade reads as a decision made at a
    price 4% from the market -- which is what this tile calls badly stale. The
    exit and the entry share a trade id and differ in side, and that is what
    separates them: the fill this tile is about is the one that opened the
    position, at the price the decision named.
    """
    entries = [
        {"kind": "bounded-order", "recorded_at_ns": 1,
         "payload": {"venue_id": "binance-usdm", "symbol": "SOXLUSDT", "entry_price": 100.0,
                     "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 2,
         "payload": {"venue_id": "binance-usdm", "symbol": "SOXLUSDT", "price": 100.02,
                     "quantity": 1.0, "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 3,
         "payload": {"venue_id": "binance-usdm", "symbol": "SOXLUSDT", "price": 104.0,
                     "quantity": 1.0, "side": "sell", "trade_id": "t-1"}},
    ]
    result = board.probe_decision_freshness(entries)
    assert result.state == board.OK, result.proof
    assert "0.02%" in result.proof


def test_only_the_first_fill_of_an_order_measures_the_decision(board):
    """Later partials are the order working through the book, which is slippage."""
    entries = [
        {"kind": "bounded-order", "recorded_at_ns": 1,
         "payload": {"venue_id": "binance-usdm", "symbol": "XRPUSDC", "entry_price": 1.4689,
                     "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 2,
         "payload": {"venue_id": "binance-usdm", "symbol": "XRPUSDC", "price": 1.4691,
                     "quantity": 1.0, "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 3,
         "payload": {"venue_id": "binance-usdm", "symbol": "XRPUSDC", "price": 1.4858,
                     "quantity": 1.0, "side": "buy", "trade_id": "t-1"}},
    ]
    result = board.probe_decision_freshness(entries)
    assert result.state == board.OK, result.proof
    assert "1 fill(s)" in result.proof or "1 most recent" in result.proof


def test_a_fill_for_a_trade_no_order_was_recorded_for_is_not_measured(board):
    """A fill whose order is older than the journal window has nothing to compare
    against, and pairing it with another trade's price on the same symbol is how
    this tile came to report a trade's profit as a stale decision."""
    entries = [
        {"kind": "bounded-order", "recorded_at_ns": 1,
         "payload": {"venue_id": "binance-usdm", "symbol": "BTCUSDT", "entry_price": 77_500.0,
                     "side": "buy", "trade_id": "t-1"}},
        {"kind": "fill", "recorded_at_ns": 2,
         "payload": {"venue_id": "binance-usdm", "symbol": "BTCUSDT", "price": 90_000.0,
                     "quantity": 0.01, "side": "buy", "trade_id": "a-different-trade"}},
    ]
    result = board.probe_decision_freshness(entries)
    assert result.state == board.WAITING
    assert "nothing has filled yet" in result.value


def test_nothing_filled_reads_as_waiting_not_as_healthy(board):
    """Rule 8: an unmeasured gap is its own state, never a passing one."""
    result = board.probe_decision_freshness([])
    assert result.state == board.WAITING
    assert "nothing has filled yet" in result.value


# ---- decisions that never reached the book ------------------------------------

def _decision(symbol, action="open", side="long", at=1):
    return {"kind": "trade-intent", "recorded_at_ns": at,
            "payload": {"venue_id": "binance-usdm", "symbol": symbol, "side": side,
                        "action": action}}


def _order(symbol, side="long", action="open", at=2):
    return {"kind": "bounded-order", "recorded_at_ns": at,
            "payload": {"venue_id": "binance-usdm", "symbol": symbol, "entry_price": 100.0,
                        "intent_id": f"binance-usdm|{symbol}|{side}|{action}"}}


def test_a_decision_that_reached_the_book_is_not_counted_as_refused(board):
    scan = board.RefusedDecisionScan()
    for entry in (_decision("BTCUSDT"), _order("BTCUSDT")):
        scan.observe(entry)

    result = scan.result()
    assert result.state == board.OK
    assert "1 of 1" in result.proof


def test_standing_aside_is_a_decision_not_to_trade_and_never_a_refusal(board):
    """The arbiter republishes its view every tick, and most ticks it stands aside.

    Counting those as decisions the system failed to act on would read as a bot
    refusing thousands of trades a minute while it was in fact declining to take
    them, which is the opposite fact.
    """
    scan = board.RefusedDecisionScan()
    scan.observe(_decision("BTCUSDT", action="stand-aside"))

    result = scan.result()
    assert result.state == board.WAITING
    assert "no actionable decision" in result.value


def test_one_decision_republished_every_tick_counts_once(board):
    """Identity is the decision, not the message: venue, symbol, side, action."""
    scan = board.RefusedDecisionScan()
    for tick in range(50):
        scan.observe(_decision("BTCUSDT", at=tick))
    scan.observe(_order("BTCUSDT"))

    assert "1 of 1" in scan.result().proof


def test_every_decision_refused_is_the_failure_state(board):
    """The shape a bound set too tight makes: the bot still decides and never trades.

    A board that showed this as an absence of trades would be indistinguishable
    from a quiet market, which is the reassurance Rule 8 exists to refuse.
    """
    scan = board.RefusedDecisionScan()
    for symbol in ("BTCUSDT", "ETHUSDT", "ENAUSDT"):
        scan.observe(_decision(symbol))

    result = scan.result()
    assert result.state == board.FAILING
    assert "0 of 3" in result.proof
    assert "none" in result.value


def test_some_refused_is_reported_without_inventing_a_threshold(board):
    """Refusals are ordinary -- capital, risk limits, an unlisted instrument. The
    number is shown; only none-at-all is a verdict."""
    scan = board.RefusedDecisionScan()
    for symbol in ("BTCUSDT", "ETHUSDT", "ENAUSDT", "SOLUSDT"):
        scan.observe(_decision(symbol))
    scan.observe(_order("BTCUSDT"))

    result = scan.result()
    assert result.state == board.OK
    assert "1 of 4" in result.proof


def test_nothing_decided_yet_is_not_healthy(board):
    result = board.RefusedDecisionScan().result()
    assert result.state == board.WAITING


# ---- Parts alive: the first consumer of staleness and input loss ---------------

def _heartbeat_table(path, collected_at_ns, beats):
    import json

    path.write_text(json.dumps({
        "schema_version": 1, "part_id": "heartbeat-collector",
        "collected_at_ns": collected_at_ns,
        "reporting": sum(1 for b in beats if b["state"] == "reporting"),
        "late": 0, "silent": sum(1 for b in beats if b["state"] == "silent"),
        "never_reported": 0, "heartbeats": beats,
    }))


@pytest.fixture
def heartbeat_settings(board, monkeypatch, tmp_path):
    table = tmp_path / "heartbeat-table.json"

    class Document:
        def read_value(self, name):
            return {"heartbeat_table_path": str(table), "heartbeat_silent_after_seconds": 10.0}[name]

    monkeypatch.setattr(board, "load_settings_document", lambda *_a, **_k: Document())
    return table


def test_parts_alive_is_not_measured_without_a_table(board, heartbeat_settings):
    result = board.probe_parts_alive(now_ns=1)
    assert result.state == board.UNMEASURED
    assert "heartbeat-collector has not run" in result.proof


def test_input_loss_fails_the_tile_and_names_the_part(board, heartbeat_settings):
    """The frozen-price defect of 2026-08-23, as the tile would have shown it."""
    _heartbeat_table(heartbeat_settings, 10_000_000_000, [
        {"part_id": "bull-feature-builder", "state": "reporting", "age_seconds": 0.3,
         "rate_ratio": 1.0, "staleness_seconds": 0.2, "input_loss": [["market-data", 3400]],
         "input_loss_since_previous": [["market-data", 540]]},
        {"part_id": "position-sizer", "state": "reporting", "age_seconds": 0.5,
         "rate_ratio": 1.0, "staleness_seconds": 0.4, "input_loss": []},
    ])
    result = board.probe_parts_alive(now_ns=11_000_000_000)
    assert result.state == board.FAILING
    assert "bull-feature-builder" in result.proof and "540" in result.proof


def test_loss_at_startup_that_stopped_is_named_not_painted_red(board, heartbeat_settings):
    _heartbeat_table(heartbeat_settings, 10_000_000_000, [
        {"part_id": "trade-capital-bounds-gate", "state": "reporting", "age_seconds": 0.3,
         "rate_ratio": 1.0, "staleness_seconds": 0.2,
         "input_loss": [["capital-settings-verdict", 212]], "input_loss_since_previous": []},
    ])
    result = board.probe_parts_alive(now_ns=11_000_000_000)
    assert result.state == board.OK
    assert "trade-capital-bounds-gate" in result.proof and "212" in result.proof
    assert "not since" in result.proof


def test_a_silent_part_fails_the_tile(board, heartbeat_settings):
    _heartbeat_table(heartbeat_settings, 10_000_000_000, [
        {"part_id": "a", "state": "silent", "age_seconds": 45.0, "rate_ratio": 1.0,
         "staleness_seconds": 45.0, "input_loss": []},
    ])
    result = board.probe_parts_alive(now_ns=11_000_000_000)
    assert result.state == board.FAILING and "a" in result.proof


def test_a_dead_collector_is_failing_not_its_last_good_table(board, heartbeat_settings):
    _heartbeat_table(heartbeat_settings, 10_000_000_000, [
        {"part_id": "a", "state": "reporting", "age_seconds": 0.3, "rate_ratio": 1.0,
         "staleness_seconds": 0.2, "input_loss": []},
    ])
    result = board.probe_parts_alive(now_ns=10_000_000_000 + 30_000_000_000)
    assert result.state == board.FAILING and "collector itself has stopped" in result.proof


def test_everything_reporting_with_no_loss_reads_ok(board, heartbeat_settings):
    _heartbeat_table(heartbeat_settings, 10_000_000_000, [
        {"part_id": "a", "state": "reporting", "age_seconds": 0.3, "rate_ratio": 1.0,
         "staleness_seconds": 0.2, "input_loss": []},
        {"part_id": "b", "state": "reporting", "age_seconds": 0.9, "rate_ratio": 1.0,
         "staleness_seconds": 0.8, "input_loss": []},
    ])
    result = board.probe_parts_alive(now_ns=11_000_000_000)
    assert result.state == board.OK and result.value == "2 of 2 reporting"


# ---- refusals a part counted, and somebody finally reads ----------------------

def test_stale_price_refusals_are_read_off_the_table(board, tmp_path, monkeypatch):
    """The tile that closes the loop on 2026-08-23.

    Each part counts why it refused. Until 2026-08-24 nothing read those counters,
    so a part refusing every decision for a stale price and a part with nothing to
    decide produced the same board.
    """
    document = {
        "collected_at_ns": 1_000,
        "heartbeats": [
            {"part_id": "instrument-selector", "state": "reporting",
             "standing": {"refused_for_a_stale_price": 41.0, "chosen": 12.0}},
            {"part_id": "signal-outcome-labeller", "state": "reporting",
             "standing": {"refused_for_a_stale_price": 7.0}},
            {"part_id": "tick-size-resolver", "state": "reporting", "standing": {}},
        ],
    }
    result = board.probe_stale_price_refusals(document)
    assert result.state == board.OK
    assert "48" in result.value
    assert "instrument-selector" in result.proof and "41" in result.proof


def test_a_part_refusing_everything_for_staleness_is_failing(board):
    """Refusing is right. Refusing everything means the bound is unreachable, and a
    bot that cannot act is not a bot with nothing to do."""
    document = {
        "collected_at_ns": 1_000,
        "heartbeats": [
            {"part_id": "instrument-selector", "state": "reporting",
             "standing": {"refused_for_a_stale_price": 500.0, "chosen": 0.0}},
        ],
    }
    result = board.probe_stale_price_refusals(document)
    assert result.state == board.FAILING
    assert "nothing was chosen" in result.proof


def test_no_part_reporting_a_standing_is_not_measured(board):
    """Rule 8: a counter nothing reported is not a count of zero."""
    document = {
        "collected_at_ns": 1_000,
        "heartbeats": [{"part_id": "tick-size-resolver", "state": "reporting"}],
    }
    result = board.probe_stale_price_refusals(document)
    assert result.state == board.UNMEASURED


def test_no_heartbeat_table_at_all_is_not_measured(board):
    assert board.probe_stale_price_refusals(None).state == board.UNMEASURED
