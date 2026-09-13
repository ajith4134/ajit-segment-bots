"""The trading tiles read the books the bots trade against.

Until 2026-09-13 "Trading" and "Open positions" read `trading/orders.jsonl` and
`trading/positions.json`, a crypto-era location nothing has written since the
pivot, and said "no order has ever been placed" on a project with 2,768 paper
fills. The checkpoints written here are the shape `paper-account-keeper` and
`position-close-detector` write -- copied from the live files' own keys.
"""

from __future__ import annotations

import json
import time

import pytest

from runtime.probes import trading_probes
from runtime.probes.trading_probes import (
    FAILING,
    NOT_BUILT,
    NOT_MEASURED,
    OK,
    probe_open_positions,
    probe_trading_state,
)

HOUR_NS = 3600 * 1_000_000_000


@pytest.fixture
def books(durable_tmp_path, monkeypatch):
    root = durable_tmp_path / "positions"
    root.mkdir()
    journal = durable_tmp_path / "journal.position-recorder.sqlite"
    monkeypatch.setattr(trading_probes, "_position_state_root", lambda: root)
    monkeypatch.setattr(
        trading_probes, "_built_segments_and_money_modes",
        lambda: [("index-options", "paper"), ("stock-options", "paper")],
    )
    monkeypatch.setattr(trading_probes, "CLOSED_TRADE_JOURNAL", journal)
    return root, journal


def an_account(root, segment, fills):
    (root / f"paper-account-keeper.paper-account-{segment}.json").write_text(json.dumps({
        "component": f"paper-account-{segment}", "part_id": "paper-account-keeper",
        "saved_at_ns": time.time_ns(), "schema_version": 1, "settings": {},
        "state": {"cash": 7_500_000.0, "fills_applied": fills, "positions": {}, "starting": 7_500_000.0},
    }))


def a_position_change(journal, hours_ago):
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "position-changed", "part_id": "position-recorder",
            "payload": {"symbol": "NIFTY 24750 CE 08 SEP 26", "quantity": 75.0,
                        "updated_at_ns": time.time_ns() - int(hours_ago * HOUR_NS)},
        }) + "\n")


def a_session(monkeypatch, answer):
    import runtime.market_session_answer as market_session_answer

    monkeypatch.setattr(market_session_answer, "read_the_calendars_live_answer", lambda: (answer, "calendar"))


def test_fills_in_the_paper_accounts_are_trading_not_absent(books, monkeypatch):
    root, journal = books
    an_account(root, "index-options", 155)
    an_account(root, "stock-options", 2613)
    a_position_change(journal, hours_ago=0.1)
    a_session(monkeypatch, "in session")
    result = probe_trading_state()
    assert result.state == OK
    assert "2,768 fills in paper" in result.value


def test_no_fill_in_any_segment_is_not_trading(books, monkeypatch):
    root, _ = books
    an_account(root, "index-options", 0)
    a_session(monkeypatch, "in session")
    assert probe_trading_state().state == NOT_BUILT


def test_quiet_for_hours_while_nse_is_open_is_stopped(books, monkeypatch):
    root, journal = books
    an_account(root, "stock-options", 2613)
    a_position_change(journal, hours_ago=119)
    a_session(monkeypatch, "in session")
    result = probe_trading_state()
    assert result.state == FAILING
    assert "STOPPED" in result.value


def test_quiet_since_the_close_is_not_stopped(books, monkeypatch):
    root, journal = books
    an_account(root, "stock-options", 2613)
    a_position_change(journal, hours_ago=119)
    a_session(monkeypatch, "out of session")
    assert probe_trading_state().state == OK


def test_quiet_with_no_session_answer_is_unmeasured(books, monkeypatch):
    root, journal = books
    an_account(root, "stock-options", 2613)
    a_position_change(journal, hours_ago=119)
    a_session(monkeypatch, None)
    assert probe_trading_state().state == NOT_MEASURED


def test_a_live_money_segment_is_named_on_the_tile(books, monkeypatch):
    root, journal = books
    monkeypatch.setattr(
        trading_probes, "_built_segments_and_money_modes",
        lambda: [("index-options", "live"), ("stock-options", "paper")],
    )
    an_account(root, "index-options", 3)
    a_position_change(journal, hours_ago=0.1)
    a_session(monkeypatch, "in session")
    assert "LIVE MONEY" in probe_trading_state().value


def a_lot_book(root, books_by_key):
    (root / "position-close-detector.positions.json").write_text(json.dumps({
        "component": "positions", "part_id": "position-close-detector",
        "saved_at_ns": time.time_ns(), "schema_version": 1, "settings": {},
        "state": {"books": books_by_key},
    }))


def test_open_lots_are_open_positions(books):
    root, _ = books
    a_lot_book(root, {
        "upstox|AXISBANK 1260 CE 29 SEP 26": [
            {"fee": 113.87, "opened_at_ns": 1, "price": 24.73, "quantity": "1281.1495441560483"},
            {"fee": 24.58, "opened_at_ns": 2, "price": 24.35, "quantity": "90.044183532404"},
        ],
        "upstox|NIFTY 24750 CE 08 SEP 26": [],
    })
    result = probe_open_positions()
    assert result.state == OK
    assert result.value.startswith("1 open (AXISBANK 1260 CE 29 SEP 26 +1,371")


def test_a_book_whose_lots_net_to_zero_is_flat(books):
    root, _ = books
    a_lot_book(root, {"upstox|SBIN 1010 PE 29 SEP 26": [
        {"fee": 1.0, "opened_at_ns": 1, "price": 17.3, "quantity": "75"},
        {"fee": 1.0, "opened_at_ns": 2, "price": 18.1, "quantity": "-75"},
    ]})
    assert probe_open_positions().value == "flat -- no position open"


def test_no_lot_book_is_not_built(books):
    assert probe_open_positions().state == NOT_BUILT


def test_an_unreadable_lot_book_is_unmeasured(books):
    root, _ = books
    (root / "position-close-detector.positions.json").write_text("{not json")
    assert probe_open_positions().state == NOT_MEASURED
