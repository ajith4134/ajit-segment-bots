"""The cash-equity shortlist ranker: a pure function, tested on constructed
signal readings the way the retired crypto reader's own
`select_capturable_symbols` was -- the thing under test is the blend's
arithmetic and its refusals, not any one day's real prices.
"""

import pytest

from runtime.cash_equity_shortlist import (
    CashEquityCandidate,
    ShortlistWeights,
    ShortlistWeightsInvalid,
    rank_cash_equity_candidates,
)

EQUAL_WEIGHTS = ShortlistWeights(
    momentum_weight=1 / 6, volume_weight=1 / 6, week_52_weight=1 / 6,
    gap_weight=1 / 6, vwap_weight=1 / 6, atr_weight=1 / 6,
)


def candidate(symbol, **kwargs) -> CashEquityCandidate:
    return CashEquityCandidate(symbol=symbol, **kwargs)


def test_weights_must_sum_to_one():
    with pytest.raises(ShortlistWeightsInvalid):
        ShortlistWeights(
            momentum_weight=0.5, volume_weight=0.5, week_52_weight=0.5,
            gap_weight=0.0, vwap_weight=0.0, atr_weight=0.0,
        )


def test_shortlist_size_below_one_is_refused():
    with pytest.raises(ValueError):
        rank_cash_equity_candidates([], shortlist_size=0, weights=EQUAL_WEIGHTS, liquidity_pool_size=10)


def test_liquidity_pool_smaller_than_shortlist_is_refused():
    with pytest.raises(ValueError):
        rank_cash_equity_candidates(
            [candidate("A")], shortlist_size=5, weights=EQUAL_WEIGHTS, liquidity_pool_size=2,
        )


def test_the_biggest_mover_is_shortlisted_over_a_flat_name():
    mover = candidate(
        "MOVER", last_price=110.0, session_open=100.0, previous_close=100.0,
        week_52_high=120.0, week_52_low=80.0, average_daily_volume=1_000_000.0,
        volume_so_far=1_000_000.0, average_true_range=5.0, session_vwap=105.0,
        liquidity_spread_fraction=0.001,
    )
    flat = candidate(
        "FLAT", last_price=100.0, session_open=100.0, previous_close=100.0,
        week_52_high=120.0, week_52_low=80.0, average_daily_volume=1_000_000.0,
        volume_so_far=100_000.0, average_true_range=5.0, session_vwap=100.0,
        liquidity_spread_fraction=0.001,
    )
    shortlist = rank_cash_equity_candidates(
        [mover, flat], shortlist_size=1, weights=EQUAL_WEIGHTS, liquidity_pool_size=2,
    )
    assert shortlist == ("MOVER",)


def test_a_thin_spread_cannot_outrank_a_liquid_pool_by_moving_more():
    thin_but_moving = candidate(
        "THIN", last_price=200.0, session_open=100.0, previous_close=100.0,
        week_52_high=200.0, week_52_low=50.0, average_daily_volume=1_000_000.0,
        volume_so_far=5_000_000.0, average_true_range=5.0, session_vwap=150.0,
        liquidity_spread_fraction=0.05,
    )
    liquid_names = [
        candidate(
            f"LIQ{i}", last_price=100.0, session_open=100.0, previous_close=100.0,
            week_52_high=110.0, week_52_low=90.0, average_daily_volume=1_000_000.0,
            volume_so_far=100_000.0, average_true_range=5.0, session_vwap=100.0,
            liquidity_spread_fraction=0.0005,
        )
        for i in range(3)
    ]
    shortlist = rank_cash_equity_candidates(
        [thin_but_moving, *liquid_names], shortlist_size=1,
        weights=EQUAL_WEIGHTS, liquidity_pool_size=3,
    )
    assert "THIN" not in shortlist


def test_a_symbol_with_no_readings_ranks_last_rather_than_crashing():
    unmeasured = candidate("BLANK", liquidity_spread_fraction=0.001)
    measured = candidate(
        "MEASURED", last_price=110.0, session_open=100.0, previous_close=100.0,
        week_52_high=120.0, week_52_low=80.0, average_daily_volume=1_000_000.0,
        volume_so_far=1_000_000.0, average_true_range=5.0, session_vwap=105.0,
        liquidity_spread_fraction=0.001,
    )
    shortlist = rank_cash_equity_candidates(
        [unmeasured, measured], shortlist_size=1, weights=EQUAL_WEIGHTS, liquidity_pool_size=2,
    )
    assert shortlist == ("MEASURED",)


def test_shortlist_size_caps_the_result():
    candidates = [
        candidate(
            f"S{i}", last_price=100.0 + i, session_open=100.0, previous_close=100.0,
            week_52_high=150.0, week_52_low=50.0, average_daily_volume=1_000_000.0,
            volume_so_far=200_000.0 * i if i else 1.0, average_true_range=5.0,
            session_vwap=100.0, liquidity_spread_fraction=0.001,
        )
        for i in range(10)
    ]
    shortlist = rank_cash_equity_candidates(
        candidates, shortlist_size=3, weights=EQUAL_WEIGHTS, liquidity_pool_size=10,
    )
    assert len(shortlist) == 3


def test_result_is_deterministic_on_a_tie():
    tied = [
        candidate(
            name, last_price=100.0, session_open=100.0, previous_close=100.0,
            week_52_high=110.0, week_52_low=90.0, average_daily_volume=1_000_000.0,
            volume_so_far=100_000.0, average_true_range=5.0, session_vwap=100.0,
            liquidity_spread_fraction=0.001,
        )
        for name in ("ZED", "ALPHA", "MID")
    ]
    first = rank_cash_equity_candidates(tied, shortlist_size=3, weights=EQUAL_WEIGHTS, liquidity_pool_size=3)
    second = rank_cash_equity_candidates(
        list(reversed(tied)), shortlist_size=3, weights=EQUAL_WEIGHTS, liquidity_pool_size=3,
    )
    assert first == second == ("ALPHA", "MID", "ZED")


def test_a_cold_ranking_picks_the_most_traded_and_not_the_alphabet():
    """The first pass of the day, before anything is subscribed.

    Nothing has a spread reading (spreads come from `liquidity-grade`, which only
    exists for a subscribed symbol) and nothing has a live price (prices come from
    the feed, which subscribes what this function returns). Every blended signal
    is therefore None for every candidate and they all score identically -- so the
    tie-break alone decides the shortlist.

    Until 2026-09-07 that tie-break was `entry.symbol`, and the live spine was
    subscribed to 3PLAND, 63MOONS, AADHARHFC, ABGSEC and ABSLLIQUID: the
    alphabetical head of the NSE master, including two ETFs. They print 20-700
    times a session against MARUTI's 8,002, so no detector window ever filled and
    cash-equity-intraday formed zero trade intents all day. The loop was closed --
    the only names ever measured were the ones the first cold ranking happened to
    pick.

    Average daily volume comes from `equity-historical-profile`, which is fetched
    from history for every ordinary share whether or not it is subscribed, so it
    is the one liquidity fact that survives having no subscription.
    """
    candidates = [
        CashEquityCandidate(symbol="3PLAND", average_daily_volume=12_000.0),
        CashEquityCandidate(symbol="ABGSEC", average_daily_volume=4_000.0),
        CashEquityCandidate(symbol="ABSLLIQUID", average_daily_volume=900.0),
        CashEquityCandidate(symbol="TRENT", average_daily_volume=3_100_000.0),
        CashEquityCandidate(symbol="ZOMATO", average_daily_volume=42_000_000.0),
    ]

    shortlist = rank_cash_equity_candidates(
        candidates, shortlist_size=2, weights=EQUAL_WEIGHTS, liquidity_pool_size=5,
    )

    assert shortlist == ("ZOMATO", "TRENT")


def test_a_measured_spread_still_outranks_an_unmeasured_one():
    """Volume orders the unmeasured names; it does not promote them past a name
    whose spread this system has actually seen."""
    candidates = [
        CashEquityCandidate(
            symbol="TIGHT", liquidity_spread_fraction=0.0004, average_daily_volume=1_000.0,
        ),
        CashEquityCandidate(symbol="LOUD", average_daily_volume=90_000_000.0),
    ]

    shortlist = rank_cash_equity_candidates(
        candidates, shortlist_size=1, weights=EQUAL_WEIGHTS, liquidity_pool_size=1,
    )

    assert shortlist == ("TIGHT",)
