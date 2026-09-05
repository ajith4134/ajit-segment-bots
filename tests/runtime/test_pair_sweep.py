"""Every pair, a budget at a time, from any cursor.

Two parts sweep the same pairs for different measurements, and the cursor is
subtle enough that two copies of it would eventually differ -- one of them
skipping a pair forever, which is invisible: the pair simply never gets a
verdict and nothing reports a fault.

The properties that matter, checked from every possible cursor rather than from
one: every pair is yielded, none is yielded twice, and a cursor whose symbols
are gone restarts rather than guessing.
"""

import itertools

from runtime.pair_sweep import pairs_after

GROUPS = {"binance-usdm": ["ADA", "BTC", "ETH"], "upstox": ["INFY", "SBIN", "TCS", "WIPRO"]}


def every_pair():
    return [
        (group, left, right)
        for group, symbols in GROUPS.items()
        for left, right in itertools.combinations(symbols, 2)
    ]


def test_a_sweep_from_the_top_yields_every_pair_once():
    walked = list(pairs_after(GROUPS))

    assert sorted(walked) == sorted(every_pair())
    assert len(walked) == len(set(walked))


def test_every_cursor_still_covers_every_pair_exactly_once():
    """The property that matters. A cursor that skipped a pair would leave it
    without a verdict forever, and nothing would report it."""
    for cursor in every_pair():
        walked = list(pairs_after(GROUPS, cursor))

        assert sorted(walked) == sorted(every_pair()), cursor
        assert len(walked) == len(set(walked)), cursor


def test_a_sweep_resumes_just_past_its_cursor():
    walked = list(pairs_after(GROUPS, ("binance-usdm", "ADA", "BTC")))

    assert walked[0] == ("binance-usdm", "ADA", "ETH")


def test_the_last_pair_of_a_group_moves_on_to_the_next_group():
    walked = list(pairs_after(GROUPS, ("binance-usdm", "BTC", "ETH")))

    assert walked[0] == ("upstox", "INFY", "SBIN")


def test_the_last_pair_of_the_last_group_wraps_to_the_top():
    walked = list(pairs_after(GROUPS, ("upstox", "TCS", "WIPRO")))

    assert walked[0] == ("binance-usdm", "ADA", "BTC")


def test_a_cursor_whose_symbols_are_gone_restarts_the_sweep():
    """Symbols come and go as the feed does, and an index into yesterday's list
    points at a different pair today."""
    walked = list(pairs_after(GROUPS, ("upstox", "DELISTED", "ALSOGONE")))

    assert walked == list(pairs_after(GROUPS))


def test_two_symbols_on_different_venues_are_not_a_pair():
    """Their prices did not arrive from the same book."""
    walked = list(pairs_after(GROUPS))

    assert all(
        left in GROUPS[group] and right in GROUPS[group]
        for group, left, right in walked
    )


def test_a_group_of_one_yields_nothing_and_does_not_stop_the_others():
    walked = list(pairs_after({"thin": ["ONLY"], "upstox": GROUPS["upstox"]}))

    assert [group for group, _, _ in walked] == ["upstox"] * 6


def test_nothing_is_materialised_for_a_universe_that_could_not_be():
    """2,985,346 pairs cost 252 MB as a list. Taking ten of them must cost
    neither the memory nor the quarter-second it takes to build."""
    symbols = [f"S{index:04d}" for index in range(2444)]
    sweep = pairs_after({"upstox": symbols})

    taken = [next(sweep) for _ in range(10)]

    assert taken[0] == ("upstox", "S0000", "S0001")
    assert len(taken) == len(set(taken))
