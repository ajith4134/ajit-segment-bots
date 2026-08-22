"""The vocabulary for taking a finished trade apart.

A closed trade is a single number -- realised PnL -- and that number is the least
informative thing about it. It conflates the setup being right with the entry being
timed, the size being sensible, the exit being taken, the costs being paid and the
market simply moving. Learning from the total teaches nothing, because the same
total is produced by a good decision that got unlucky and a bad decision that got
lucky, and reinforcing both is worse than reinforcing neither.

So this block takes trades apart, and the shapes here are the pieces. Four ideas run
through all of them:

- **Every piece is separable and named.** A trade that lost because the stop sat
  inside the noise is a different lesson from one that lost because the regime
  turned, and both are different from one that lost to slippage. The decomposition
  is the deliverable.
- **A counterfactual is labelled as one.** "The exit at the peak would have made
  more" is not a fact about a decision -- it is a fact about hindsight, and mixing
  them is how a system learns to chase peaks.
- **Significance travels with every outcome.** A single trade is one draw from a
  wide distribution, and treating it as evidence is the fastest way to overfit to
  noise. Anything derived from too few trades says so rather than reporting a number.
- **Nothing here is revised.** These records are what was found when the trade
  closed; a later, better-informed edit destroys exactly the evidence that shows how
  understanding changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Where PnL actually came from. These sum to the realised total by construction --
# a decomposition that does not reconcile is a story, not an attribution.
FROM_DIRECTION = "direction"          # the market moved the way the trade was placed
FROM_TIMING = "timing"                # entering or exiting at a better moment
FROM_SIZE = "size"                    # the position being larger or smaller
FROM_FEES = "fees"                    # what the venue charged
FROM_SLIPPAGE = "slippage"            # the gap between intended and achieved price
FROM_FUNDING = "funding"              # perpetual funding paid or received
FROM_UNEXPLAINED = "unexplained"      # the residual, kept rather than distributed

PNL_COMPONENTS = (
    FROM_DIRECTION, FROM_TIMING, FROM_SIZE, FROM_FEES, FROM_SLIPPAGE, FROM_FUNDING,
    FROM_UNEXPLAINED,
)

# Why a losing trade lost. One cause, chosen because acting on the wrong one makes
# things worse: widening a stop that was correctly placed is how a small loss
# becomes a large one.
THE_SETUP_WAS_WRONG = "the-setup-never-had-an-edge"
THE_ENTRY_WAS_EARLY = "the-entry-was-taken-before-the-move-confirmed"
THE_ENTRY_WAS_LATE = "the-entry-was-taken-after-the-move-was-spent"
THE_STOP_WAS_INSIDE_THE_NOISE = "the-stop-sat-where-normal-movement-would-reach-it"
THE_STOP_WAS_TOO_WIDE = "the-stop-let-a-small-loss-become-a-large-one"
THE_REGIME_TURNED = "the-market-changed-character-mid-trade"
THE_EXIT_WAS_LATE = "a-profitable-position-was-given-back"
COSTS_ATE_IT = "the-move-was-real-but-smaller-than-what-it-cost-to-capture"
IT_WAS_JUST_VARIANCE = "a-good-decision-that-lost"

LOSS_CAUSES = (
    THE_SETUP_WAS_WRONG, THE_ENTRY_WAS_EARLY, THE_ENTRY_WAS_LATE,
    THE_STOP_WAS_INSIDE_THE_NOISE, THE_STOP_WAS_TOO_WIDE, THE_REGIME_TURNED,
    THE_EXIT_WAS_LATE, COSTS_ATE_IT, IT_WAS_JUST_VARIANCE,
)


@dataclass(frozen=True)
class PnlAttribution:
    """Where the money came from, in pieces that add back to the total.

    The residual is kept as its own component rather than smeared across the others.
    A large unexplained share is the most useful signal here: it means the model of
    where PnL comes from is missing something, and hiding it inside "direction"
    makes an incomplete decomposition look complete.
    """

    trade_id: str
    venue_id: str
    symbol: str
    realised_pnl: float
    components: dict
    quote_currency: str
    reconciles: bool
    residual: float
    attributed_at_ns: int

    @property
    def cost_share(self) -> float:
        """How much of the gross move went to the venue rather than the account."""
        costs = abs(self.components.get(FROM_FEES, 0.0)) + abs(
            self.components.get(FROM_SLIPPAGE, 0.0)
        ) + abs(self.components.get(FROM_FUNDING, 0.0))
        gross = abs(self.components.get(FROM_DIRECTION, 0.0))
        return costs / gross if gross > 0 else 0.0


@dataclass(frozen=True)
class LossCause:
    """The one thing that most explains a loss, with what else was considered."""

    trade_id: str
    cause: str
    confidence: float
    is_fitted: bool
    runners_up: tuple
    evidence: dict
    was_avoidable: bool
    reason: str
    classified_at_ns: int

    @property
    def is_worth_changing_something_for(self) -> bool:
        """Variance is not a defect, and treating it as one produces churn."""
        return self.was_avoidable and self.cause != IT_WAS_JUST_VARIANCE


@dataclass(frozen=True)
class EntryQuality:
    """How good the entry price was against what was reachable around that moment.

    Measured against the window the decision could actually have acted in, not
    against the day's best price -- which no decision could have reached and which
    makes every entry look bad.
    """

    trade_id: str
    achieved_price: float
    best_reachable: float | None
    worst_reachable: float | None
    percentile: float | None
    seconds_of_window: float
    was_chasing: bool
    is_measurable: bool
    reason: str
    scored_at_ns: int


@dataclass(frozen=True)
class ExitQuality:
    """How much of the move the exit actually captured.

    Two numbers rather than one: the share of the favourable excursion captured, and
    the share of the adverse excursion suffered. An exit that captured 60% of the
    peak while sitting through the whole drawdown is not the same as one that
    captured 60% and never went underwater.
    """

    trade_id: str
    captured_fraction: float | None
    gave_back: float | None
    suffered_fraction: float | None
    exit_price: float
    peak_price: float | None
    is_measurable: bool
    reason: str
    scored_at_ns: int


@dataclass(frozen=True)
class ExitCounterfactual:
    """What other exit rules would have produced on this trade, labelled as hindsight.

    Explicitly a counterfactual: it is what would have happened if a rule had been
    followed, which is not evidence that the rule is better until the same comparison
    holds across many trades.
    """

    trade_id: str
    rule_name: str
    exit_price: float | None
    realised_pnl: float | None
    difference: float | None
    would_have_been_reachable: bool
    is_hindsight: bool
    reason: str
    replayed_at_ns: int


@dataclass(frozen=True)
class ShortfallBreakdown:
    """The gap between the price a decision assumed and the price it got.

    Separated because the three causes have different fixes: a wider spread is a
    symbol choice, a slow decision is a latency problem, and market impact is a size
    problem. One "slippage" number cannot tell them apart.
    """

    trade_id: str
    decision_price: float
    arrival_price: float
    achieved_price: float
    spread_cost: float
    delay_cost: float
    impact_cost: float
    total_shortfall: float
    quantity: float
    reason: str
    measured_at_ns: int

    @property
    def largest_cause(self) -> str:
        return max(
            (("spread", abs(self.spread_cost)), ("delay", abs(self.delay_cost)),
             ("impact", abs(self.impact_cost))),
            key=lambda pair: pair[1],
        )[0]


@dataclass(frozen=True)
class StopAudit:
    """Whether the stop was placed where normal movement would reach it.

    The single most common avoidable loss: a stop inside the symbol's ordinary
    noise is not a risk control, it is a scheduled exit.
    """

    trade_id: str
    stop_price: float | None
    distance: float | None
    typical_movement: float | None
    distance_in_typical_movements: float | None
    was_hit: bool
    would_have_recovered: bool | None
    verdict: str
    is_measurable: bool
    reason: str
    audited_at_ns: int


@dataclass(frozen=True)
class PeakExcursionProfile:
    """How far trades run in favour and against before they resolve (RL-042).

    Built across many trades rather than one, because a single trade's excursion is
    a draw and the distribution is what a stop or a target should be set from.
    """

    venue_id: str
    symbol: str
    regime: str | None
    trades: int
    median_favourable: float | None
    median_adverse: float | None
    favourable_quantile: float | None
    adverse_quantile: float | None
    quantile: float
    is_fitted: bool
    measured_at_ns: int


@dataclass(frozen=True)
class HorizonProfile:
    """How long an edge survives, per setup, measured from counterfactual exits.

    Answers the question a holding period is usually guessed at: does this setup pay
    more when held longer, and where does that stop being true.
    """

    setup: str
    trades: int
    best_horizon_seconds: float | None
    payoff_by_horizon: dict
    decays_after_seconds: float | None
    is_fitted: bool
    reason: str
    measured_at_ns: int


@dataclass(frozen=True)
class TradeCluster:
    """Trades that were not independent bets, however separate they looked.

    Ten correlated longs are one bet with ten fee payments. Any statistic computed
    over them as independent samples overstates its own confidence by roughly the
    square root of the cluster size.
    """

    cluster_id: str
    trade_ids: tuple
    correlation_group: str | None
    direction: str
    opened_within_seconds: float
    effective_bets: float
    reason: str
    detected_at_ns: int

    @property
    def was_one_bet(self) -> bool:
        return len(self.trade_ids) > 1 and self.effective_bets < len(self.trade_ids)


@dataclass(frozen=True)
class OutcomeSignificance:
    """Whether an outcome is distinguishable from luck.

    Compared against the move the symbol makes anyway over the same horizon. A 2%
    gain on something that moves 5% a day is not a result.
    """

    trade_id: str
    realised: float
    expected_noise: float | None
    standardised: float | None
    is_significant: bool
    is_measurable: bool
    sample_size: int
    reason: str
    assessed_at_ns: int


@dataclass(frozen=True)
class NearMissEpisode:
    """A trade that was considered and not taken, with what happened next.

    Without these the record contains only trades that were taken, which is
    survivorship applied to this system's own decisions: a filter that rejects every
    good setup looks perfect, because nothing it rejected is ever recorded.
    """

    episode_id: str
    venue_id: str
    symbol: str
    side: str
    reference_price: float
    why_not_taken: str
    would_have_realised: float | None
    is_resolved: bool
    considered_at_ns: int
    resolved_at_ns: int | None

    @property
    def was_a_mistake_to_skip(self) -> bool:
        return (
            self.is_resolved
            and self.would_have_realised is not None
            and self.would_have_realised > 0
        )


@dataclass(frozen=True)
class RegimeTransitionFlag:
    """Whether the market changed character while the trade was open.

    A trade entered in one regime and closed in another was not the trade that was
    decided on, and learning from it as though it were teaches the wrong lesson
    about the setup.
    """

    trade_id: str
    regime_at_entry: str | None
    regime_at_exit: str | None
    changed: bool
    changed_at_ns: int | None
    fraction_of_the_trade_in_the_new_regime: float | None
    reason: str
    tagged_at_ns: int


@dataclass(frozen=True)
class SequencePattern:
    """Something true about trades in order rather than trades in aggregate.

    Order carries information a per-trade view destroys: losses arriving together,
    size growing after a win, quality falling as the day goes on.
    """

    pattern_id: str
    description: str
    kind: str
    trades_examined: int
    occurrences: int
    effect: float
    is_significant: bool
    reason: str
    found_at_ns: int


@dataclass(frozen=True)
class WinnerPattern:
    """What winning trades had in common that losing ones did not.

    The second half is the part usually skipped, and skipping it produces "winners
    were in BTCUSDT" when everything was in BTCUSDT.
    """

    pattern_id: str
    conditions: dict
    winners_matching: int
    losers_matching: int
    winners_total: int
    losers_total: int
    lift: float
    is_significant: bool
    reason: str
    mined_at_ns: int

    @property
    def separates(self) -> bool:
        return self.is_significant and self.lift > 1.0


@dataclass(frozen=True)
class TradeNarrative:
    """The trade in sentences, every one of which traces to a measurement."""

    trade_id: str
    text: str
    sentences_kept: tuple
    sentences_removed: tuple
    facts_used: dict
    was_written_by_a_model: bool
    written_at_ns: int


@dataclass(frozen=True)
class PairVerdict:
    """What an exploratory paired trade actually established (RL-005).

    A pair is run to learn something, so its verdict is about the question, not
    about the money. A pair that lost money and settled the question did its job.
    """

    pair_id: str
    question: str
    verdict: str
    long_realised: float | None
    short_realised: float | None
    difference: float | None
    is_conclusive: bool
    reason: str
    decided_at_ns: int


@dataclass(frozen=True)
class ReplayMismatch:
    """Where the record of a trade disagrees with what the venue reported.

    The check nothing else performs: every downstream conclusion is computed from
    the journal, so a journal that has drifted from the fills makes every one of
    them confidently wrong.
    """

    trade_id: str
    field: str
    journal_value: object
    venue_value: object
    difference: float | None
    severity: str
    reason: str
    found_at_ns: int


@dataclass(frozen=True)
class DecodedTradeInstruction:
    """One thing to do differently, derived from a decomposed trade.

    An instruction is not a lesson: it names the condition it applies under, so it
    can be tested and retired. "Be more patient" is not one of these.
    """

    instruction_id: str
    applies_when: dict
    change: str
    derived_from: tuple
    expected_effect: float | None
    trades_supporting: int
    is_testable: bool
    reason: str
    written_at_ns: int

    @property
    def can_be_acted_on(self) -> bool:
        return self.is_testable and self.trades_supporting > 1


def unexplained_only(realised: float) -> dict:
    """The honest attribution when nothing could be decomposed."""
    return {component: 0.0 for component in PNL_COMPONENTS} | {
        FROM_UNEXPLAINED: realised
    }
