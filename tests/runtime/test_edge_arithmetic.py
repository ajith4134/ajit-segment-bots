"""The floor a trade has to clear is arithmetic from its own plan, not a literal."""

import pytest

from runtime.edge_arithmetic import (
    ConvictionFloor,
    FloorUncomputable,
    break_even_probability,
    round_trip_cost_in_risk_units,
)

# The live settings on 2026-08-23: the higher of the two venues' taker fees, and
# the least reward-to-risk a bull exit plan is accepted with.
FEE_RATE = 0.00055
LEAST_REWARD_TO_RISK = 1.5


def test_break_even_is_where_expected_value_is_zero():
    for reward_to_risk, cost in ((1.0, 0.0), (1.5, 0.073), (3.0, 0.5), (0.5, 0.1)):
        p = break_even_probability(reward_to_risk, cost)
        expected_value = p * reward_to_risk - (1.0 - p) - cost
        assert expected_value == pytest.approx(0.0, abs=1e-12)


def test_a_coin_flip_at_even_money_breaks_even_before_fees():
    assert break_even_probability(1.0) == pytest.approx(0.5)


def test_fees_raise_the_floor_and_a_tight_stop_raises_it_most():
    wide = round_trip_cost_in_risk_units(FEE_RATE, 0.015)   # 1.5% stop
    tight = round_trip_cost_in_risk_units(FEE_RATE, 0.001)  # 0.1% stop
    assert wide == pytest.approx(0.0733, abs=1e-3)
    assert tight == pytest.approx(1.1, abs=1e-3), "two fees cost more than the whole stop"
    assert break_even_probability(1.5, tight) > break_even_probability(1.5, wide) > break_even_probability(1.5)


def test_the_live_numbers_the_literal_was_refusing():
    """0.55 stood in three settings; the plans behind the refused intents paid
    1.5 to 1 with a 1.5% stop, whose break-even is 42.9%. The model's measured
    output of 0.44-0.50 clears that and never cleared 0.55."""
    floor = ConvictionFloor(fee_rate=FEE_RATE, margin=0.0, fallback_reward_to_risk=LEAST_REWARD_TO_RISK)
    value, reason = floor.for_plan(1.5, 0.015)
    assert value == pytest.approx(0.4293, abs=1e-3)
    assert "break-even 42.9%" in reason
    assert 0.44 > value and 0.55 > value


def test_before_a_plan_exists_the_floor_is_the_least_any_plan_could_demand():
    floor = ConvictionFloor(fee_rate=FEE_RATE, margin=0.0, fallback_reward_to_risk=LEAST_REWARD_TO_RISK)
    value, reason = floor.before_any_plan()
    assert value == pytest.approx(0.4)
    assert "plan's own floor applies once it exists" in reason
    assert floor.for_plan(None, None) == floor.before_any_plan()


def test_the_margin_is_added_and_the_floor_never_exceeds_one():
    floor = ConvictionFloor(fee_rate=FEE_RATE, margin=0.1, fallback_reward_to_risk=LEAST_REWARD_TO_RISK)
    assert floor.before_any_plan()[0] == pytest.approx(0.5)
    capped = ConvictionFloor(fee_rate=FEE_RATE, margin=0.9, fallback_reward_to_risk=LEAST_REWARD_TO_RISK)
    assert capped.for_plan(1.5, 0.001)[0] == 1.0


@pytest.mark.parametrize("bad", [
    dict(fee_rate=-0.1, margin=0.0, fallback_reward_to_risk=1.5),
    dict(fee_rate=0.0, margin=-0.1, fallback_reward_to_risk=1.5),
    dict(fee_rate=0.0, margin=1.0, fallback_reward_to_risk=1.5),
    dict(fee_rate=0.0, margin=0.0, fallback_reward_to_risk=0.0),
])
def test_a_floor_that_cannot_mean_anything_is_refused(bad):
    with pytest.raises(FloorUncomputable):
        ConvictionFloor(**bad)


def test_a_plan_that_risks_nothing_or_pays_nothing_has_no_floor():
    with pytest.raises(FloorUncomputable):
        round_trip_cost_in_risk_units(FEE_RATE, 0.0)
    with pytest.raises(FloorUncomputable):
        break_even_probability(0.0)
