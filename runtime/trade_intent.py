"""What leaves the brain: one intent, and everything that was weighed to reach it.

Substrate, not a part. Eleven brain parts pass these between them and none may
import another (T-4), so the vocabulary lives here.

The whole shape of the brain is in this file's types. Three bots produce
opinions; the brain turns them into **one** intent, and everything else here
exists to make that single decision reviewable afterwards:

- The **conflict ruling** says what was done when the bots disagreed, and why.
- The **counter-argument** and the **premortem** are the case against, recorded
  before the trade rather than after it -- which is the only time it can be
  written honestly.
- The **rationale** is what a person reads when they ask why this trade exists.
- The **reflection note** is what the brain concluded about its own reasoning
  once the outcome was known.

An **intent is not an order.** It says what should be true of the position; the
risk gate can refuse it, the capital desk sizes it, the instrument selector
decides what expresses it and the execution block turns it into orders. Keeping
that in the type is what stops the brain from becoming the trading system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate

LONG = "long"
SHORT = "short"
BUY = "buy"
SELL = "sell"

# What an intent asks for. From `state_vocabulary`, and closed on purpose: a
# brain that could ask for something not on this list would be asking for
# something no part downstream implements.
OPEN = "open"
ADD_TO = "add-to"
REDUCE = "reduce"
CLOSE = "close"
STAND_ASIDE = "stand-aside"

# How the bots stood relative to one another. The distinction matters because
# agreement and a lone opinion are not the same evidence, and neither is a
# disagreement that was ruled on.
UNANIMOUS = "all-bots-agree"
MAJORITY = "a-majority-agree"
SOLE_OPINION = "only-one-bot-had-a-view"
RULED = "the-bots-disagreed-and-the-conflict-was-ruled-on"
NO_OPINION = "no-bot-had-a-view"


@dataclass(frozen=True)
class TradeIntent:
    """What the brain wants to be true of a position, and what it weighed.

    Deliberately not an order. It carries no price, no order type and no venue
    contract -- those are decisions belonging to parts that know things this one
    does not, and an intent that named them would be making them by implication.
    """

    venue_id: str
    symbol: str
    side: str
    action: str
    conviction: Estimate
    horizon_seconds: float
    stop_price: float | None
    agreement: str
    contributing_bots: tuple
    dissenting_bots: tuple
    opinion_weights: dict
    evidence: dict
    reason: str
    formed_at_ns: int

    @property
    def is_long(self) -> bool:
        """Which way this intent points, in either vocabulary.

        The arbiter forms intents in the brain's terms -- long or short -- and parts
        further along speak the venue's, buy or sell. Both are accepted here so that
        a reader never has to know which half of the system its input came from.
        """
        return self.side in (LONG, BUY)

    @property
    def is_short(self) -> bool:
        return self.side in (SHORT, SELL)

    @property
    def decision_id(self) -> str:
        """What makes two republished intents the same decision.

        The arbiter publishes a standing opinion tick after tick -- an opinion
        still held is still published -- so every part downstream sees the same
        decision many times. Identity therefore cannot come from anything that
        moves with the market, and until 2026-08-23 the order id was derived from
        the order's quantity and entry price. Both drift on every tick, so one
        decision became a new order every second: on the live run at 09:01 a
        single ENAUSDT long produced 13 orders and 13 fills, 12,982 USDT of
        notional against a 1,000 per-trade cap.

        Venue, symbol, side and action: the decision itself, with nothing in it
        that the next print can change. A bot that changes its mind changes one of
        these, and a bot that does not is still asking for the same trade.
        """
        return f"{self.venue_id}|{self.symbol}|{self.side}|{self.action}"

    @property
    def is_actionable(self) -> bool:
        return self.action != STAND_ASIDE

    @property
    def was_contested(self) -> bool:
        return bool(self.dissenting_bots)


@dataclass(frozen=True)
class ConflictRuling:
    """What was done about bots that disagreed, and on what grounds.

    Recorded even when the ruling is "do nothing", because a trade not taken
    because two bots contradicted each other is a decision, and a system that
    only records the trades it took cannot learn from the ones it did not.
    """

    venue_id: str
    symbol: str
    ruling: str
    favoured_bot: str | None
    opposed_bots: tuple
    grounds: str
    regime: str
    ruled_at_ns: int

    @property
    def resolved(self) -> bool:
        return self.favoured_bot is not None


@dataclass(frozen=True)
class BotWeight:
    """How much one bot's opinion counts right now, in this regime.

    Per regime because a bot that is right in a trend and wrong in a chop has
    two records, and one weight over both describes neither.
    """

    bot: str
    regime: str
    weight: float
    hit_rate: Estimate
    regret: float
    trades_judged: int
    is_exploring: bool
    reason: str
    weighed_at_ns: int


@dataclass(frozen=True)
class ForecastBias:
    """How far the ensemble forecast should shift the brain's view, if at all."""

    venue_id: str
    symbol: str
    bias: float
    trust: Estimate
    forecast: float
    reason: str
    weighed_at_ns: int

    @property
    def is_trusted(self) -> bool:
        return self.trust.is_fitted and self.bias != 0.0


@dataclass(frozen=True)
class SizeHint:
    """How large this intent should be relative to a normal one, and why.

    A hint rather than a size: the capital desk owns sizing and knows about the
    rest of the book, which this part does not. A brain that returned a notional
    would be sizing without knowing what else is open.
    """

    venue_id: str
    symbol: str
    multiple_of_normal: float
    conviction: Estimate
    agreement: str
    floor: float
    ceiling: float
    reason: str
    hinted_at_ns: int


@dataclass(frozen=True)
class TimedIntent:
    """An intent plus when it may act, and when it stops being valid."""

    intent: TradeIntent
    act_now: bool
    trigger_price: float | None
    valid_until_ns: int | None
    waited_for: str
    reason: str
    timed_at_ns: int

    def has_expired(self, now_ns: int | None = None) -> bool:
        """Whether the reasoning behind this intent has aged out.

        A method rather than a property because it needs the clock, and a
        timing decision that read a clock it was not given would be untestable.
        """
        if self.valid_until_ns is None:
            return False
        return (now_ns if now_ns is not None else time.time_ns()) > self.valid_until_ns


@dataclass(frozen=True)
class DecisionRationale:
    """Why this trade exists, in terms a person can check against the evidence.

    `citations` is what makes it checkable: every claim points at the measurement
    behind it, so a rationale that has drifted from its evidence can be caught
    rather than believed.
    """

    venue_id: str
    symbol: str
    action: str
    headline: str
    body: str
    citations: dict
    unsupported_claims: tuple
    was_written_by_a_model: bool
    reason: str
    written_at_ns: int

    @property
    def is_fully_supported(self) -> bool:
        return not self.unsupported_claims


@dataclass(frozen=True)
class CounterArgument:
    """The strongest case against an intent, made before it is acted on."""

    venue_id: str
    symbol: str
    objections: tuple
    strongest_objection: str | None
    would_reverse_the_decision: bool
    evidence_cited: dict
    was_written_by_a_model: bool
    reason: str
    argued_at_ns: int


WORKING = "working"
UNDERPERFORMING = "underperforming"
UNMEASURED = "not-enough-trades-to-judge"


@dataclass(frozen=True)
class StrategyReview:
    """A narrative judgment on one of the system's own bots or detectors.

    Not a per-symbol call -- `opinion-arbiter` folds it into a bot's weight
    across every symbol, never into one trade-intent directly.
    """

    bot: str
    assessment: str
    confidence: Estimate
    reason: str
    formed_at_ns: int


@dataclass(frozen=True)
class PremortemNote:
    """Assume this trade has failed. What happened? Written before entry.

    Before entry because that is the only time it can be written honestly: after
    a loss the reasons are obvious and wrong, and after a win nobody writes it.
    """

    venue_id: str
    symbol: str
    failure_modes: tuple
    most_likely_failure: str | None
    what_would_show_it_early: tuple
    was_written_by_a_model: bool
    reason: str
    written_at_ns: int


@dataclass(frozen=True)
class ReflectionNote:
    """What the brain concluded about its own reasoning, once the outcome was known.

    Kept separate from the trade record on purpose: whether a trade made money
    and whether the reasoning was sound are different questions, and a system
    that conflates them learns to repeat lucky mistakes.
    """

    venue_id: str
    symbol: str
    reasoning_was_sound: bool | None
    outcome_was_good: bool | None
    lesson: str
    applies_beyond_this_trade: bool
    citations: dict
    was_written_by_a_model: bool
    reason: str
    reflected_at_ns: int

    @property
    def is_a_lucky_win(self) -> bool:
        return self.outcome_was_good is True and self.reasoning_was_sound is False

    @property
    def is_an_unlucky_loss(self) -> bool:
        return self.outcome_was_good is False and self.reasoning_was_sound is True


def no_intent(
    venue_id: str, symbol: str, agreement: str, reason: str, now_ns=time.time_ns
) -> TradeIntent:
    """The decision to do nothing, published rather than dropped.

    A symbol the brain declined to trade must be distinguishable from one it
    never considered, or the record cannot tell restraint from a broken feed
    (Rule 8).
    """
    return TradeIntent(
        venue_id=venue_id,
        symbol=symbol,
        side=LONG,
        action=STAND_ASIDE,
        conviction=Estimate(
            value=0.0, is_fitted=False, observations=0, prior=0.0,
            was_clamped=False, bound_low=None, bound_high=None, reason=reason,
        ),
        horizon_seconds=0.0,
        stop_price=None,
        agreement=agreement,
        contributing_bots=(),
        dissenting_bots=(),
        opinion_weights={},
        evidence={},
        reason=reason,
        formed_at_ns=now_ns(),
    )
