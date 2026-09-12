"""The probability a trade must be right with to be worth taking, from its own plan.

A conviction floor was a literal until 2026-08-23: 0.55 in three settings, set
before the model had seen an outcome, with the note "just above the coin flip".
The model's measured output on the live run sits between 0.44 and 0.50, so the
literal refused every trade -- 325 of 327 intents stood aside -- and RL-061 says
a number in decision code is estimated from data or named with provenance, not
chosen to feel right.

The floor a trade actually has to clear is arithmetic, not judgement. A plan says
where the stop is and where the target is; fees say what the round trip costs.
With reward-to-risk R and round-trip cost c expressed in units of the risk, the
expected value of acting on a calibrated probability p is

    p * R - (1 - p) * 1 - c

and it is zero at p* = (1 + c) / (1 + R). Below p* the trade loses on average
even if the probability is exactly right; above it the trade is worth something,
and how much is the sizer's business, not a gate's.

What this module refuses to be: a place for a margin. The margin over break-even
is an operator's setting with its own provenance, passed in and added here so
every gate applies the same arithmetic and the same margin.

**The cost and the risk must be measured on the same instrument.** `c` is the
round trip expressed in units of the risk, and the risk is a fraction of the
price of whatever symbol the plan was written on. Charging one global options
cost against every plan is what pinned the floor at 1.0 for a whole session on
2026-09-08: `volatility-gap-detector -> ICICIBANK` produces a stop measured as a
fraction of ICICIBANK's price, and it was charged
`per_side_trading_cost_fraction`, a rate derived for an NSE option premium. A
stock's range over a minute and an option's are two different sizes of number,
so the ratio between them was meaningless and the floor it produced was
uncrossable by construction. `for_plan` therefore takes the round trip actually
measured for that symbol -- `liquidity-grade.round_trip_cost_fraction`, this
project's own reading of that symbol's book -- and falls back to the setting only
where nothing has graded it. measurements/2026-09-08-conviction-floor-name-mismatch/
carries the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass


class FloorUncomputable(ValueError):
    """The plan does not carry what a break-even needs."""


def round_trip_cost_in_risk_units(fee_rate: float, risk_fraction: float) -> float:
    """Two taker fees, measured against how far the stop is from the entry.

    A 0.055% fee against a stop 1.5% away costs 0.073 of the risk; the same fee
    against a stop 0.1% away costs 1.1 of it -- more than the whole stop -- which
    is why a tight stop needs a far higher probability to be worth anything.
    """
    if fee_rate < 0.0:
        raise FloorUncomputable(f"a negative fee rate ({fee_rate}) is not a cost")
    return cost_in_risk_units(2.0 * fee_rate, risk_fraction)


def cost_in_risk_units(round_trip_cost_fraction: float, risk_fraction: float) -> float:
    """A whole round trip, already measured, against how far the stop is.

    Both are fractions of the same symbol's price, which is the only way the
    ratio means anything -- see the module docstring for what charging one
    instrument's cost against another's risk did.
    """
    if round_trip_cost_fraction < 0.0:
        raise FloorUncomputable(
            f"a negative round trip ({round_trip_cost_fraction}) is not a cost"
        )
    if risk_fraction <= 0.0:
        raise FloorUncomputable(
            f"a stop {risk_fraction} of the price away risks nothing, so no cost can be "
            f"measured against it"
        )
    return round_trip_cost_fraction / risk_fraction


def break_even_probability(reward_to_risk: float, round_trip_cost: float = 0.0) -> float:
    """p* at which acting has zero expected value: (1 + c) / (1 + R)."""
    if reward_to_risk <= 0.0:
        raise FloorUncomputable(
            f"a reward-to-risk of {reward_to_risk} pays nothing, so no probability makes "
            f"the trade worth taking"
        )
    if round_trip_cost < 0.0:
        raise FloorUncomputable(f"a negative round-trip cost ({round_trip_cost}) is not a cost")
    return (1.0 + round_trip_cost) / (1.0 + reward_to_risk)


@dataclass(frozen=True)
class ConvictionFloor:
    """The floor a gate applies: break-even for this plan plus the operator's margin.

    fallback_reward_to_risk is the least reward-to-risk any plan will be accepted
    at, for a gate that runs before a plan exists (the entry timer decides the
    moment while the exit plan is still being built). Its floor is the lowest
    any plan could later demand, fee-free; the composer and arbiter apply the
    exact one once the plan is in hand.
    """

    fee_rate: float
    margin: float
    fallback_reward_to_risk: float

    def __post_init__(self) -> None:
        if self.fee_rate < 0.0:
            raise FloorUncomputable(f"a negative fee rate ({self.fee_rate}) is not a cost")
        if not 0.0 <= self.margin < 1.0:
            raise FloorUncomputable(
                f"a margin of {self.margin} over break-even is outside [0, 1): below zero acts "
                f"on trades that lose on average, and at one nothing could ever clear it"
            )
        if self.fallback_reward_to_risk <= 0.0:
            raise FloorUncomputable(
                f"a fallback reward-to-risk of {self.fallback_reward_to_risk} pays nothing"
            )

    def for_plan(
        self,
        reward_to_risk: float | None,
        risk_fraction: float | None,
        round_trip_cost_fraction: float | None = None,
    ) -> tuple[float, str]:
        """The floor and the sentence that says where it came from.

        `round_trip_cost_fraction` is what a round trip in **this plan's own
        symbol** costs, as a fraction of that symbol's price -- the measured
        reading from its own book plus the broker's charge stack. Absent it, the
        fallback is twice `fee_rate`, which is one rate for every instrument and
        is right only where the plan happens to be written on the instrument that
        rate was derived for.
        """
        if reward_to_risk is None or risk_fraction is None:
            return self.before_any_plan()
        if round_trip_cost_fraction is None:
            cost = round_trip_cost_in_risk_units(self.fee_rate, risk_fraction)
            source = (
                f"the {2.0 * self.fee_rate:.3%} round trip every plan is charged where its own "
                f"symbol has not been graded"
            )
        else:
            cost = cost_in_risk_units(round_trip_cost_fraction, risk_fraction)
            source = f"this symbol's own measured {round_trip_cost_fraction:.3%} round trip"
        floor = min(1.0, break_even_probability(reward_to_risk, cost) + self.margin)
        return floor, (
            f"break-even {break_even_probability(reward_to_risk, cost):.1%} at {reward_to_risk:.2f} "
            f"reward to risk with {source} costing {cost:.3f} of the risk, plus a margin of "
            f"{self.margin:.1%}"
        )

    def before_any_plan(self) -> tuple[float, str]:
        floor = min(1.0, break_even_probability(self.fallback_reward_to_risk) + self.margin)
        return floor, (
            f"fee-free break-even {break_even_probability(self.fallback_reward_to_risk):.1%} at the "
            f"{self.fallback_reward_to_risk:.2f} reward to risk every plan must at least pay, "
            f"plus a margin of {self.margin:.1%}; the plan's own floor applies once it exists"
        )


__all__ = [
    "ConvictionFloor",
    "FloorUncomputable",
    "break_even_probability",
    "cost_in_risk_units",
    "round_trip_cost_in_risk_units",
]
