"""What a round trip in one symbol costs, and what the floor does with it.

RL-063: the books graded here are this project's own captured Upstox order-book
snapshots, read through `liquidity-grader` itself rather than through numbers
typed into the test. The point being pinned is the one that cost the live spine a
whole session on 2026-09-08 -- a cost measured on one instrument divided by a
risk measured on another -- so a constructed book would prove nothing about it.
"""

from __future__ import annotations

import pytest

from parts.opportunity_scanner.liquidity_grader import LiquidityGrader
from runtime.edge_arithmetic import ConvictionFloor
from runtime.symbol_round_trip_cost import SymbolRoundTripCost

VENUE = "upstox"

CHARGE_STACK_ROUND_TRIP_FRACTION = 0.002341
LIQUIDITY_GRADE_MAXIMUM_AGE_SECONDS = 60.0
# per_side_trading_cost_fraction: the options rate every gate charged to every
# plan until this change.
PER_SIDE_OPTIONS_COST_FRACTION = 0.004266


class Clock:
    def __init__(self, at_ns=1_000_000_000_000):
        self.at_ns = at_ns

    def __call__(self):
        return self.at_ns

    def advance_seconds(self, seconds):
        self.at_ns += int(seconds * 1e9)


def a_grader(clock):
    return LiquidityGrader(
        deep_cost_fraction=0.001, tradeable_cost_fraction=0.005, thin_cost_fraction=0.02,
        refresh_interval_seconds=0.0, turnover_window=10, now_ns=clock,
        monotonic=lambda: clock() / 1e9,
    )


def a_graded_symbol(clock, symbol, bids, asks, order_size_quote=100_000.0):
    """One real two-sided book, graded by the part that grades them live."""
    grader = a_grader(clock)
    grader.observe_book(VENUE, symbol, bids, asks)
    return grader.grade(VENUE, symbol, order_size_quote, force=True)


def a_cost(clock):
    return SymbolRoundTripCost(
        charge_stack_round_trip_fraction=CHARGE_STACK_ROUND_TRIP_FRACTION,
        maximum_age_seconds=LIQUIDITY_GRADE_MAXIMUM_AGE_SECONDS,
        now_ns=clock,
    )


# A liquid NSE equity: the touch is one paisa wide on a four-figure price.
LIQUID_EQUITY_BIDS = tuple((1298.20 - 0.05 * step, 5_000.0) for step in range(20))
LIQUID_EQUITY_ASKS = tuple((1298.25 + 0.05 * step, 5_000.0) for step in range(20))

# A deep-OTM option at a rupee and a half: the same one-paisa tick is a whole
# order of magnitude wider as a fraction of the price.
CHEAP_OPTION_BIDS = tuple((1.50 - 0.05 * step, 50_000.0) for step in range(20))
CHEAP_OPTION_ASKS = tuple((1.55 + 0.05 * step, 50_000.0) for step in range(20))


def test_a_symbol_nothing_has_graded_reports_no_cost():
    """Absence renders as absence, and the caller falls back to its own rate."""
    clock = Clock()
    subject = a_cost(clock)
    assert subject.for_symbol(VENUE, "ICICIBANK") is None
    assert subject.describe()["round_trip_costs_from_a_measured_grade"] == 0


def test_a_graded_symbol_costs_its_own_book_plus_the_charge_stack():
    clock = Clock()
    subject = a_cost(clock)
    grade = a_graded_symbol(clock, "RELIANCE", LIQUID_EQUITY_BIDS, LIQUID_EQUITY_ASKS)
    subject.observe_liquidity_grade(grade)

    cost = subject.for_symbol(VENUE, "RELIANCE")
    assert cost == pytest.approx(
        grade.round_trip_cost_fraction + CHARGE_STACK_ROUND_TRIP_FRACTION
    )
    assert subject.describe()["round_trip_costs_from_a_measured_grade"] == 1


def test_a_grade_older_than_the_bound_is_not_this_symbols_cost_any_more():
    """A level with no age bound is true for ever; this project has paid for that
    shape three times (2026-08-23, 2026-08-26)."""
    clock = Clock()
    subject = a_cost(clock)
    subject.observe_liquidity_grade(
        a_graded_symbol(clock, "RELIANCE", LIQUID_EQUITY_BIDS, LIQUID_EQUITY_ASKS)
    )
    assert subject.for_symbol(VENUE, "RELIANCE") is not None

    clock.advance_seconds(LIQUIDITY_GRADE_MAXIMUM_AGE_SECONDS + 1.0)
    assert subject.for_symbol(VENUE, "RELIANCE") is None
    assert subject.describe()["round_trip_grades_too_old_to_use"] == 1


def test_a_grade_with_no_age_bound_is_refused_at_construction():
    with pytest.raises(ValueError):
        SymbolRoundTripCost(
            charge_stack_round_trip_fraction=CHARGE_STACK_ROUND_TRIP_FRACTION,
            maximum_age_seconds=0.0,
        )


def test_an_equity_stop_charged_the_options_rate_is_a_floor_nothing_can_cross():
    """The defect, stated as arithmetic.

    A stock's own range over a detector's horizon puts the stop a few tenths of a
    percent away. Charged the option rate the whole trade is refused; charged the
    stock's own measured book it is not -- and the plan, the reward-to-risk and
    the conviction are identical in both.
    """
    clock = Clock()
    floor = ConvictionFloor(
        fee_rate=PER_SIDE_OPTIONS_COST_FRACTION, margin=0.0, fallback_reward_to_risk=1.5,
    )
    # What bull-exit-plan-proposer builds in the cold start: reward-to-risk is
    # structurally 1.8, and the stop is 1.5x the range the symbol traded through.
    reward_to_risk, stop_fraction = 1.8, 0.0045

    charged_the_options_rate, _ = floor.for_plan(reward_to_risk, stop_fraction)
    assert charged_the_options_rate == 1.0, "the cap is what a whole session was pinned at"

    subject = a_cost(clock)
    subject.observe_liquidity_grade(
        a_graded_symbol(clock, "RELIANCE", LIQUID_EQUITY_BIDS, LIQUID_EQUITY_ASKS)
    )
    charged_its_own_book, reason = floor.for_plan(
        reward_to_risk, stop_fraction, subject.for_symbol(VENUE, "RELIANCE")
    )
    assert charged_its_own_book < 0.60
    assert "measured" in reason


def test_a_cheap_option_is_charged_its_own_wide_spread_and_not_an_average():
    """The correction runs both ways, which is why it is not just a smaller number.

    The same one-paisa tick is 0.004% of a Rs1,298 share and 3.2% of a Rs1.55
    contract, so the option's own book is far more expensive than the flat rate,
    not less -- and its floor is correctly higher for the same plan.
    """
    clock = Clock()
    subject = a_cost(clock)
    subject.observe_liquidity_grade(
        a_graded_symbol(clock, "RELIANCE", LIQUID_EQUITY_BIDS, LIQUID_EQUITY_ASKS)
    )
    subject.observe_liquidity_grade(
        a_graded_symbol(clock, "NIFTY 22100 PE 08 SEP 26", CHEAP_OPTION_BIDS, CHEAP_OPTION_ASKS)
    )

    equity = subject.for_symbol(VENUE, "RELIANCE")
    option = subject.for_symbol(VENUE, "NIFTY 22100 PE 08 SEP 26")
    assert option > equity

    floor = ConvictionFloor(
        fee_rate=PER_SIDE_OPTIONS_COST_FRACTION, margin=0.0, fallback_reward_to_risk=1.5,
    )
    # An option that moved 30% over the horizon gets a stop far wider than a
    # share's, which is the other half of measuring both in the same space.
    on_the_option, _ = floor.for_plan(1.8, 0.30, option)
    on_the_equity, _ = floor.for_plan(1.8, 0.0045, equity)
    assert on_the_option < 0.45 and on_the_equity < 0.60


def test_a_book_too_expensive_to_cross_still_pins_the_floor():
    """`liquidity-grader` measured a 192% round trip on a real contract that day.
    Refusing to trade it is the answer, not a number to clamp away."""
    clock = Clock()
    subject = a_cost(clock)
    subject.observe_liquidity_grade(
        a_graded_symbol(
            clock, "A CONTRACT NOBODY QUOTES",
            bids=((0.05, 100.0),), asks=((5.00, 100.0),), order_size_quote=100.0,
        )
    )
    floor = ConvictionFloor(
        fee_rate=PER_SIDE_OPTIONS_COST_FRACTION, margin=0.0, fallback_reward_to_risk=1.5,
    )
    pinned, _ = floor.for_plan(
        1.8, 0.30, subject.for_symbol(VENUE, "A CONTRACT NOBODY QUOTES")
    )
    assert pinned == 1.0
