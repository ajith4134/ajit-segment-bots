"""The vocabulary for testing an instruction against history before it trades.

A backtest is a machine for producing encouraging numbers. Left alone it will find
an edge in a random walk, and the found edge will be specific, plausible and worth
nothing. So every shape in this module carries the thing that makes its number
believable or not, and the parts that produce them are built to fail rather than to
flatter.

The failures these types are shaped against, in the order they usually happen:

- **Lookahead.** A signal computed with information that did not exist at decision
  time. It is the most common defect and the hardest to see, because the code reads
  perfectly -- so `BacktestRun` records what each decision could see, not just what
  it decided.
- **Free fills.** Assuming a fill at the price a bar closed at, in unlimited size,
  with no spread and no impact. Every result here carries the cost model that
  produced it and the size that was actually available.
- **Fitting to the test.** A rule tuned until it works on the same data it is
  measured on. The split is therefore part of the run, and a result without one is
  refused rather than annotated.
- **Sample-size illusion.** Sixty trades looking excellent is thirty coin flips
  looking excellent. Sample size and the effect it can support travel with every
  result.

Nothing here decides to trade. A proven instruction is one that survived, which is
a much weaker claim than one that works, and the type says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# What a bar can be filled at, in ascending order of honesty about cost.
AT_THE_CLOSE = "at-the-close"                 # the fiction that makes backtests work
AT_THE_NEXT_OPEN = "at-the-next-open"         # honest about the decision lag
AT_THE_SPREAD = "at-the-spread"               # honest about crossing
WITH_IMPACT = "with-impact"                   # honest about size

FILL_ASSUMPTIONS = (AT_THE_CLOSE, AT_THE_NEXT_OPEN, AT_THE_SPREAD, WITH_IMPACT)

# Why a backtest cannot be believed. Each is fatal on its own.
USED_THE_FUTURE = "a-decision-used-information-that-did-not-exist-yet"
FILLED_AT_A_PRICE_THAT_NEVER_TRADED = "a-fill-at-a-price-the-tape-never-printed"
FILLED_MORE_THAN_TRADED = "a-fill-larger-than-the-volume-available"
NO_COSTS = "no-fee-or-slippage-was-subtracted"
FITTED_ON_THE_TEST_PERIOD = "measured-on-the-data-it-was-tuned-on"
SURVIVORSHIP = "the-symbol-list-is-todays-survivors"

FATAL_DEFECTS = (
    USED_THE_FUTURE, FILLED_AT_A_PRICE_THAT_NEVER_TRADED, FILLED_MORE_THAN_TRADED,
    NO_COSTS, FITTED_ON_THE_TEST_PERIOD, SURVIVORSHIP,
)


@dataclass(frozen=True)
class HistoricalWindow:
    """A slice of recorded market data, with what is missing from it named.

    Gaps are the property that matters. A window with a two-hour hole in it produces
    a backtest that skipped a crash, and a window that silently interpolates the hole
    produces one that traded through a price that never existed.
    """

    window_id: str
    venue_id: str
    symbol: str
    interval_seconds: float
    bars: tuple
    first_at_ns: int
    last_at_ns: int
    expected_bars: int
    missing_bars: int
    gap_spans: tuple
    was_interpolated: bool
    built_at_ns: int

    @property
    def completeness(self) -> float:
        if self.expected_bars <= 0:
            return 0.0
        return len(self.bars) / self.expected_bars

    @property
    def can_be_backtested(self) -> bool:
        """An interpolated window trades through prices that never existed."""
        return not self.was_interpolated and self.missing_bars == 0


@dataclass(frozen=True)
class WalkForwardSplit:
    """One in-sample period and the out-of-sample period that follows it.

    Chronological and non-overlapping by construction. A random split leaks the
    future into training through every autocorrelated feature, which is all of them
    in a price series.
    """

    split_id: str
    fold: int
    train_from_ns: int
    train_to_ns: int
    test_from_ns: int
    test_to_ns: int
    embargo_seconds: float

    @property
    def is_chronological(self) -> bool:
        return self.train_to_ns <= self.test_from_ns

    @property
    def has_an_embargo(self) -> bool:
        """The gap that stops a trade opened in training resolving inside the test."""
        return (self.test_from_ns - self.train_to_ns) / 1e9 >= self.embargo_seconds > 0


@dataclass(frozen=True)
class CostEstimate:
    """What a trade of this size in this instrument would actually cost.

    Split into its parts, because a single number cannot be checked against reality
    and a single number is what makes an unprofitable strategy look profitable.
    """

    venue_id: str
    symbol: str
    notional: float
    fee: float
    half_spread: float
    expected_impact: float
    is_fitted: bool
    observations: int
    estimated_at_ns: int

    @property
    def total(self) -> float:
        return self.fee + self.half_spread + self.expected_impact

    @property
    def fraction_of_notional(self) -> float:
        return self.total / self.notional if self.notional > 0 else 0.0


@dataclass(frozen=True)
class FillableSize:
    """How much could actually have been traded, from the volume that printed.

    A backtest filling the whole intended size regardless of what traded is the
    single most flattering assumption available in an illiquid symbol.
    """

    venue_id: str
    symbol: str
    at_ns: int
    intended: float
    fillable: float
    volume_in_bar: float
    participation_cap: float
    reason: str

    @property
    def was_capped(self) -> bool:
        return self.fillable < self.intended


@dataclass(frozen=True)
class FillSequence:
    """The order in which a bar's extremes were reached, or the admission it is unknown.

    A bar records open, high, low and close and not the path between them. Whether
    the stop or the target came first is therefore genuinely unknown from bar data,
    and a backtest that assumes the favourable one is not optimistic -- it is wrong.
    """

    venue_id: str
    symbol: str
    at_ns: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    order: tuple
    is_known: bool
    assumption: str
    reason: str

    @property
    def resolves_ambiguity_pessimistically(self) -> bool:
        return not self.is_known and self.assumption == "adverse-first"


@dataclass(frozen=True)
class BacktestTrade:
    """One simulated trade, with what the decision could see when it was made."""

    entry_at_ns: int
    exit_at_ns: int
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    gross: float
    costs: float
    net: float
    fill_assumption: str
    decided_with_data_up_to_ns: int

    @property
    def used_the_future(self) -> bool:
        return self.decided_with_data_up_to_ns > self.entry_at_ns


@dataclass(frozen=True)
class BacktestRun:
    """One instruction replayed over one split, with everything needed to distrust it."""

    run_id: str
    instruction_id: str
    split_id: str
    venue_id: str
    symbol: str
    trades: tuple
    period_from_ns: int
    period_to_ns: int
    fill_assumption: str
    costs_applied: bool
    was_out_of_sample: bool
    bars_seen: int
    ran_at_ns: int

    @property
    def net_return(self) -> float:
        return sum(trade.net for trade in self.trades)

    @property
    def gross_return(self) -> float:
        return sum(trade.gross for trade in self.trades)

    @property
    def cost_share(self) -> float:
        gross = abs(self.gross_return)
        return sum(trade.costs for trade in self.trades) / gross if gross > 0 else 0.0


@dataclass(frozen=True)
class BacktestVerdict:
    """Whether a run may be believed at all, before anyone looks at its number."""

    run_id: str
    defects: tuple
    is_believable: bool
    checks_run: tuple
    reason: str
    audited_at_ns: int

    @property
    def has_a_fatal_defect(self) -> bool:
        return any(defect in FATAL_DEFECTS for defect in self.defects)


@dataclass(frozen=True)
class BacktestResult:
    """What a believable run scored, with the sample size that produced it."""

    run_id: str
    instruction_id: str
    trades: int
    net_return: float
    gross_return: float
    cost_share: float
    win_rate: float
    average_win: float
    average_loss: float
    expectancy: float
    worst_drawdown: float
    effective_bets: float
    is_significant: bool
    required_trades: int
    scored_at_ns: int

    @property
    def survives_costs(self) -> bool:
        return self.net_return > 0 and self.gross_return > 0


@dataclass(frozen=True)
class ProvenInstruction:
    """An instruction that survived every gate. A weaker claim than 'it works'.

    Survival means: it was believable, it cleared its sample size, it was tested out
    of sample, and it was not the best of many tries without that being accounted for.
    """

    instruction_id: str
    runs: tuple
    folds_passed: int
    folds_total: int
    net_return: float
    trials_before_it: int
    survived_refutation: bool
    reason: str
    proven_at_ns: int

    @property
    def is_ready_to_trade(self) -> bool:
        return self.folds_passed == self.folds_total and self.survived_refutation


@dataclass(frozen=True)
class LiveVsReplayGap:
    """The difference between what the backtest promised and what live produced.

    The number that tells you whether any of this machinery works. A backtest that
    consistently overstates live results by the same margin is a cost model that is
    wrong; one that overstates erratically is a fill model that is wrong.
    """

    instruction_id: str
    backtested_return: float
    live_return: float
    gap: float
    live_trades: int
    backtested_trades: int
    is_consistent: bool
    likely_cause: str
    reason: str
    measured_at_ns: int


def unbelievable(run_id: str, defects, reason: str, at_ns: int) -> BacktestVerdict:
    return BacktestVerdict(
        run_id=run_id, defects=tuple(defects), is_believable=False,
        checks_run=FATAL_DEFECTS, reason=reason, audited_at_ns=at_ns,
    )
