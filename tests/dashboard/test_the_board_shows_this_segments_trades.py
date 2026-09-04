"""A board for one segment must not show another segment's trades as its own.

`read_closed_trades` reads a bounded tail of the position journal and shows the
newest round trips it finds, whatever they are. That was right while there was
one segment. It stopped being right when the goal pivoted: the journal still
holds the crypto era's 778 closed trades, and on 2026-09-04 the live board
served 100 of them -- SANDUSDT on binance-usdm, entry 0.03931 -- on a board
whose capital, universe and open positions are all index-options in INR.

They are real trades. They are not this segment's, and the true state of
index-options is that nothing has closed yet. A board that answers a question
about this segment with another one's rows is the failure Rule 8 exists to
prevent, and it is worse than an empty table because it is convincing.

The venues are an allowlist rather than a list of crypto venues to drop: an
ignore-list admits the first thing nobody thought to exclude, and a second
broker is already planned (goal.md item 6).
"""

from __future__ import annotations

from dashboard.trade_activity import (
    segment_trading_venues,
    trades_belonging_to_this_segment,
)


def a_trade(venue_id, symbol):
    return {"venue_id": venue_id, "symbol": symbol}


def test_the_venues_are_read_from_a_setting_not_hardcoded():
    venues = segment_trading_venues()

    assert venues, "a board that trusts every venue shows every segment's trades"
    assert "upstox" in venues


def test_another_segments_trades_are_not_shown_as_this_ones():
    kept, dropped = trades_belonging_to_this_segment(
        [
            a_trade("binance-usdm", "SANDUSDT"),
            a_trade("upstox", "NIFTY24500CE"),
            a_trade("bybit-linear", "BTCUSDT"),
        ]
    )

    assert [t["symbol"] for t in kept] == ["NIFTY24500CE"]
    assert dropped == 2


def test_nothing_of_this_segments_reads_as_nothing_rather_than_as_someone_elses():
    """The true state of index-options today, and it must be reachable."""
    kept, dropped = trades_belonging_to_this_segment(
        [a_trade("binance-usdm", "SANDUSDT"), a_trade("bybit-linear", "BTCUSDT")]
    )

    assert kept == []
    assert dropped == 2


def test_a_trade_with_no_venue_is_dropped_rather_than_assumed_to_be_ours():
    kept, dropped = trades_belonging_to_this_segment([a_trade(None, "UNKNOWN")])

    assert kept == []
    assert dropped == 1
