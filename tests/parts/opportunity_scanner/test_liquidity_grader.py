import pytest

from parts.opportunity_scanner.liquidity_grader import (
    TRADEABLE, UNGRADEABLE, LiquidityGrader,
)


def make_grader(**overrides):
    defaults = dict(
        refresh_interval_seconds=0.0,
        deep_cost_fraction=0.001,
        tradeable_cost_fraction=0.01,
        thin_cost_fraction=0.05,
        turnover_window=10,
    )
    defaults.update(overrides)
    return LiquidityGrader(**defaults)


def test_grade_walks_a_clean_two_sided_book():
    grader = make_grader()
    grader.observe_book(
        "upstox", "NIFTY24SEP25000CE",
        bids=((99.9, 500.0), (99.8, 500.0)),
        asks=((100.1, 500.0), (100.2, 500.0)),
    )
    result = grader.grade("upstox", "NIFTY24SEP25000CE", order_size_quote=1000.0)
    assert result.grade == TRADEABLE
    assert result.is_tradeable


def test_grade_is_ungradeable_when_the_ask_side_is_entirely_padding():
    """Real bug, 2026-09-02: a thin option's real book can have every level
    on one side padded to price=0, quantity=0 (a broker pads unused depth
    slots). observe_book's own filter drops every one of them, leaving asks
    empty -- which grade()'s existing two-sided-book check already treats
    as ungradeable, rather than a fabricated best_ask of 0 reaching the
    walk-cost math (where it used to divide by that zero touch price)."""
    grader = make_grader()
    grader.observe_book(
        "upstox", "BANKNIFTY24SEP60000CE",
        bids=((200.0, 10.0), (0.0, 0.0)),
        asks=((0.0, 0.0), (0.0, 0.0)),
    )
    result = grader.grade(
        "upstox", "BANKNIFTY24SEP60000CE", order_size_quote=1000.0,
    )
    assert result.grade == UNGRADEABLE
    assert result.spread_fraction is None


def test_grade_ignores_padding_levels_and_grades_the_real_book():
    """Real bug, 2026-09-02: a padding level's price of 0 sorts below any
    real ask, so the old unfiltered sort put it at asks[0] -- a false
    'best_ask is 0' price that poisoned the spread and crashed the walk
    cost's own division. observe_book now drops it before sorting, so
    grading proceeds against the real 99.9/100.1 book exactly as if the
    padding had never been sent."""
    grader = make_grader()
    grader.observe_book(
        "upstox", "BANKNIFTY24SEP52000PE",
        bids=((99.9, 500.0), (0.0, 0.0)),
        asks=((100.1, 500.0), (0.0, 0.0)),
    )
    result = grader.grade(
        "upstox", "BANKNIFTY24SEP52000PE", order_size_quote=1000.0,
    )
    assert result.grade == TRADEABLE
    assert result.spread_fraction == pytest.approx((100.1 - 99.9) / 100.0)
    assert result.depth_at_size_fraction is not None
