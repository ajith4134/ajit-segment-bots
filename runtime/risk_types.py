"""The risk vocabulary: limits, bounds and the rule that combines them.

Six parts produce `risk-limit` independently -- drawdown, exposure, events, a
halt, margin proximity, stop frequency -- and every one of them can only ever
*reduce* what may be risked. That is the whole design: a limiter cannot grant
permission, only withhold it, so adding a seventh reason to be careful can never
make the system less careful.

**The smallest limit wins, and it says who set it.** A number alone would be
useless the moment two limiters disagree: an operator seeing "you may risk 0"
needs to know whether that is a drawdown breaker, a halt, or a margin call, and
those demand completely different responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# A limit of exactly this means no new risk may be taken at all. Named rather
# than compared against a bare zero, because "the limiter said stop" and "the
# limiter has not reported yet" must never be the same value.
NO_RISK_ALLOWED = 0.0


@dataclass(frozen=True)
class RiskLimit:
    """The most that may be risked, and which limiter said so.

    `fraction_of_allotment` rather than an absolute amount: a limiter reasons
    about proportion -- drawdown, exposure, correlation -- and the allotment it
    applies to is the capital desk's business. Multiplying too early would put a
    stale balance inside every limiter.
    """

    limiter: str
    fraction_of_allotment: float
    reason: str
    is_binding: bool
    decided_at_ns: int

    @property
    def forbids_new_risk(self) -> bool:
        return self.fraction_of_allotment <= NO_RISK_ALLOWED


def tightest_limit(limits, now_ns=time.time_ns) -> RiskLimit:
    """The binding limit across every limiter that reported.

    An empty set is not permission. A system with no limiter reporting has not
    been cleared to trade -- it has failed to ask -- so the answer is zero and
    says which it is.
    """
    reported = [limit for limit in limits if limit is not None]
    if not reported:
        return RiskLimit(
            limiter="none",
            fraction_of_allotment=NO_RISK_ALLOWED,
            reason="no limiter has reported; silence is not permission",
            is_binding=True,
            decided_at_ns=now_ns(),
        )
    tightest = min(reported, key=lambda limit: (limit.fraction_of_allotment, limit.limiter))
    return RiskLimit(
        limiter=tightest.limiter,
        fraction_of_allotment=tightest.fraction_of_allotment,
        reason=tightest.reason,
        is_binding=True,
        decided_at_ns=now_ns(),
    )


@dataclass(frozen=True)
class TradeCapitalBounds:
    """The smallest and largest capital one trade may use, from settings (RL-054).

    A minimum exists because a trade too small to matter still pays fees and
    still occupies attention; a maximum exists because no single trade may put
    the segment's allocation at risk. Both are the operator's, and estimation may
    never exceed them (RL-061).
    """

    segment: str
    minimum_capital: float
    maximum_capital: float
    currency: str

    def __post_init__(self) -> None:
        if self.minimum_capital < 0 or self.maximum_capital < 0:
            raise ValueError("capital bounds cannot be negative")
        if self.minimum_capital > self.maximum_capital:
            raise ValueError(
                f"minimum capital {self.minimum_capital} is above maximum {self.maximum_capital}; "
                f"no order could satisfy both"
            )


# What `capital-settings-verdict` says, in the three states it has. Named rather
# than compared against a bare string, and living here rather than in the part
# that judges, because the part that acts on a verdict must not import the part
# that formed it (T-4) -- and a reader that guessed at the shape instead would
# get a wrong answer that looks exactly like a refusal.
CONSISTENT = "consistent"
INCONSISTENT = "inconsistent"
INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class SettingsFault:
    """One pair of settings that cannot both be obeyed."""

    settings: tuple[str, ...]
    values: tuple[float, ...]
    explanation: str


@dataclass(frozen=True)
class CapitalSettingsVerdict:
    """Whether the settings can be obeyed, and every reason they cannot.

    `permits_trading` is the whole point of the type: `trade-capital-bounds-gate`
    refuses every order while it is False, so this is the switch between a system
    that can place an order and one that cannot.
    """

    segment: str
    verdict: str
    faults: tuple[SettingsFault, ...]
    reason: str
    judged_at_ns: int

    @property
    def permits_trading(self) -> bool:
        return self.verdict == CONSISTENT


@dataclass(frozen=True)
class CapitalAllotment:
    """One segment's slice of the main account, and what bounds it (RL-051)."""

    segment: str
    allotted: float
    currency: str
    leverage_ceiling: float
    bounds: TradeCapitalBounds
    read_at_ns: int
