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


def test_closing_is_not_built_and_the_board_names_the_parts(board):
    result = board.probe_closed()
    assert result.state == board.NOT_BUILT
    for part_id, _produces in board.CLOSING_CHAIN:
        assert part_id in result.proof


def test_learning_progress_is_unmeasured_and_says_what_would_measure_it(board):
    result = board.probe_learning()
    assert result.state == board.UNMEASURED
    assert "bull-conviction-model" in result.proof


def test_the_page_renders_every_probe_and_every_trade(board, durable_tmp_path):
    path = durable_tmp_path / "journal.jsonl"
    entries = write_journal(path, trades=2)
    trades = board.collect_trades(entries, live_from_ns=None)

    rendered = board.render_trades(trades, path)
    for trade in trades:
        assert trade.trade_id in rendered
    assert "test run" in rendered

    empty = board.render_trades([], path)
    assert "No trade has been recorded" in empty
    assert str(path) in empty


def test_the_generated_page_is_self_contained_and_theme_aware(board):
    """It is published as an artifact, where an external request is blocked."""
    page = board.build_page()
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
