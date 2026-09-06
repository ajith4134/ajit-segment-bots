"""Real Indian prices for any past date, and the cache that makes them repeatable.

The tape is three days deep and holds only what the feed was subscribed to.
Upstox serves one-minute bars from January 2022, verified against the live API on
2026-09-06 with this project's own adapter and token:

    RELIANCE  2022-01-10..14   1,875 bars      NIFTY 50 index  2025-09-01..05  1,875 bars
    NIFTY 24000 CE  2026-09-01..05  1,540 bars

**The quota is real and it is not per-second.** After a day of fetching, Upstox
answered HTTP 429 and went on refusing through four backoffs totalling 200
seconds. That is what the cache is for: a past session never changes, so
refetching one spends a quota to be told the same thing again, and a default
source that cannot be re-run is not a default anybody can use.
"""

from __future__ import annotations

import json

import pytest

from operate.historical_prints import (
    HISTORICAL_INTERVAL, HISTORICAL_UNIT, NoBrokerToken,
    cache_path_for, cached_document, historical_candles, prints_from_candles,
    remember_document, upstox_access_token,
)

# One real Upstox historical response, in the shape the endpoint actually
# returns: newest row first, +05:30 stamps, [time, open, high, low, close,
# volume, open_interest] (RL-063 -- the shape is the venue's, not invented).
A_REAL_RESPONSE = {
    "status": "success",
    "data": {
        "candles": [
            ["2025-09-01T09:17:00+05:30", 1350.0, 1351.2, 1349.5, 1350.8, 4210, 0],
            ["2025-09-01T09:16:00+05:30", 1349.4, 1350.6, 1349.0, 1350.1, 3980, 0],
            ["2025-09-01T09:15:00+05:30", 1349.3, 1350.0, 1348.8, 1349.4, 5120, 0],
        ]
    },
}


def test_bars_become_prints_oldest_first_at_their_own_times():
    """The adapter reverses the venue's newest-first rows; a series read in the
    order Upstox sends it runs backwards through time."""
    from runtime.brokers.upstox import UpstoxAdapter

    candles = UpstoxAdapter().read_historical_candles(
        "NSE_EQ|INE002A01018", HISTORICAL_UNIT, HISTORICAL_INTERVAL, A_REAL_RESPONSE,
    )
    prints = prints_from_candles(candles)
    assert [price for _at_ns, price in prints] == [1349.4, 1350.1, 1350.8]
    assert [at_ns for at_ns, _price in prints] == sorted(at_ns for at_ns, _ in prints)


def test_a_print_is_the_bar_close_not_its_high_or_low():
    """The high and low happened somewhere inside the minute and nothing says
    when. Replaying them would place prices at times they did not occur, and a
    stop walked against an invented time is an exit the market never offered."""
    from runtime.brokers.upstox import UpstoxAdapter

    candles = UpstoxAdapter().read_historical_candles(
        "NSE_EQ|INE002A01018", HISTORICAL_UNIT, HISTORICAL_INTERVAL, A_REAL_RESPONSE,
    )
    closes = {candle.close for candle in candles}
    assert {price for _at_ns, price in prints_from_candles(candles)} == closes


# ---- the cache ---------------------------------------------------------------

def test_a_remembered_range_is_read_back_without_fetching(tmp_path, monkeypatch):
    """A past session never changes, so a second read must cost no request. The
    access token is deliberately nonsense: if anything reached the network this
    would fail rather than quietly succeed."""
    import operate.historical_prints as history

    monkeypatch.setattr(history, "HISTORY_CACHE", tmp_path)
    path = history.cache_path_for("NSE_EQ|INE002A01018", "2025-09-01", "2025-09-05")
    history.remember_document(path, A_REAL_RESPONSE)

    candles = historical_candles(
        "NSE_EQ|INE002A01018", "2025-09-01", "2025-09-05", "not-a-real-token",
    )
    assert len(candles) == 3
    assert candles[0].close == 1349.4


def test_the_cache_is_keyed_by_instrument_and_range(tmp_path, monkeypatch):
    """One instrument's September is not its October, and NIFTY's is not
    RELIANCE's. A key that collided would replay one contract as another."""
    import operate.historical_prints as history

    monkeypatch.setattr(history, "HISTORY_CACHE", tmp_path)
    first = history.cache_path_for("NSE_EQ|A", "2025-09-01", "2025-09-05")
    same_instrument_other_range = history.cache_path_for("NSE_EQ|A", "2025-10-01", "2025-10-05")
    other_instrument = history.cache_path_for("NSE_FO|B", "2025-09-01", "2025-09-05")
    assert len({first, same_instrument_other_range, other_instrument}) == 3
    # The instrument key carries a pipe and a space, so it is the directory and
    # never part of a filename.
    assert first.parent.name == "NSE_EQ|A"


def test_a_half_written_cache_file_is_never_read_as_data(tmp_path, monkeypatch):
    """Written beside and renamed into place: a replay killed mid-write must not
    come back to a truncated JSON document that parses as fewer bars."""
    import operate.historical_prints as history

    monkeypatch.setattr(history, "HISTORY_CACHE", tmp_path)
    path = history.cache_path_for("NSE_EQ|A", "2025-09-01", "2025-09-05")
    history.remember_document(path, A_REAL_RESPONSE)
    assert not list(path.parent.glob("*.writing"))
    assert json.loads(path.read_text()) == A_REAL_RESPONSE


def test_an_unreadable_cache_file_is_a_miss_not_a_crash(tmp_path, monkeypatch):
    import operate.historical_prints as history

    monkeypatch.setattr(history, "HISTORY_CACHE", tmp_path)
    path = history.cache_path_for("NSE_EQ|A", "2025-09-01", "2025-09-05")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert cached_document(path) is None


def test_no_token_says_so_rather_than_fetching_anonymously(tmp_path):
    """There is no anonymous route to this data, and a caller that got an empty
    series instead of an error would read a missing credential as a quiet
    market."""
    with pytest.raises(NoBrokerToken):
        upstox_access_token(tokens_root=tmp_path)
