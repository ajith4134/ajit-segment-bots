"""The shapes a directional bot works in, shared by three bots that never meet.

Substrate, not a part. The bull bot, the bear bot and the profit tailgater are
peers: R-03 forbids any wire between them and T-4 forbids one importing another,
so the vocabulary they all speak lives here rather than in whichever of them was
written first.

The chain is deliberately several types rather than one:

    candidate -> features -> raw conviction -> calibrated conviction
              -> timing + exit plan -> opinion

Each arrow is a place a part can be turned off or replaced without the rest
noticing (T-1, T-6), and each type carries what it could **not** establish. A
feature vector that quietly filled a missing funding rate with zero would teach
the model that missing means zero, and the model would then trade on it.

An **opinion is not an order.** It is one bot saying what it would do and how
sure it is; the arbiter weighs it against its peers and the risk gate can refuse
it entirely. Keeping that in the type is what stops a bot from becoming the
trading system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate
from runtime.online_learner import ModelBelief

LONG = "long"
SHORT = "short"

# What a bot wants done, from `state_vocabulary`. Standing down is a real answer
# and the most common correct one; a bot with no way to say it will always find
# a reason to trade.
ENTER_NOW = "enter-now"
WAIT_FOR_TRIGGER = "wait-for-trigger"
STAND_DOWN = "stand-down"
CLOSE_POSITION = "close-position"
REDUCE_POSITION = "reduce-position"

# Why an opinion could not be formed. Each is a different fix.
NO_CANDIDATE = "no-candidate-for-this-symbol"
FEATURES_INCOMPLETE = "features-incomplete"
FEATURES_OUT_OF_DISTRIBUTION = "features-out-of-distribution"
CONVICTION_TOO_LOW = "conviction-below-threshold"
NO_EXIT_PLAN = "no-exit-plan-could-be-built"
TIMING_REFUSED = "timing-refused"


@dataclass(frozen=True)
class SideCandidate:
    """A candidate that survived one side's filter, with the weight it carried."""

    bot: str
    side: str
    venue_id: str
    symbol: str
    detector: str
    expectation: str
    signal_strength: float
    detector_confidence: Estimate
    setup_weight: float
    horizon_seconds: float
    evidence: dict
    reason: str
    accepted_at_ns: int


@dataclass(frozen=True)
class FeatureVector:
    """What the model gets to look at, and what could not be measured.

    `missing` is not a diagnostic afterthought. A model handed a zero for a
    funding rate that was simply unavailable learns that unavailable means
    neutral, and there is no later stage that can undo that.
    """

    bot: str
    venue_id: str
    symbol: str
    features: dict
    missing: tuple
    sources: dict
    built_at_ns: int

    @property
    def is_complete(self) -> bool:
        return not self.missing

    def with_feature(self, name: str, value: float, source: str) -> "FeatureVector":
        return FeatureVector(
            bot=self.bot,
            venue_id=self.venue_id,
            symbol=self.symbol,
            features={**self.features, name: value},
            missing=tuple(item for item in self.missing if item != name),
            sources={**self.sources, name: source},
            built_at_ns=self.built_at_ns,
        )


@dataclass(frozen=True)
class OutOfDistributionFlag:
    """Whether this vector looks like anything the model was trained on."""

    bot: str
    venue_id: str
    symbol: str
    is_out_of_distribution: bool
    worst_feature: str | None
    worst_deviation: float | None
    features_judged: int
    features_unjudgeable: tuple
    reason: str
    flagged_at_ns: int


@dataclass(frozen=True)
class RawConviction:
    """The model's own number, before anyone asks what it has meant."""

    bot: str
    venue_id: str
    symbol: str
    side: str
    belief: ModelBelief
    reason: str
    formed_at_ns: int

    @property
    def probability(self) -> float:
        return self.belief.probability


@dataclass(frozen=True)
class CalibratedConviction:
    """The model's number after its own record has been applied to it."""

    bot: str
    venue_id: str
    symbol: str
    side: str
    raw_probability: float
    calibrated: Estimate
    scorecard_observations: int
    reason: str
    calibrated_at_ns: int
    # What the model behind this number has been trained on. Carried because the
    # two questions a composer has to ask are different, and asking one while
    # meaning the other stopped the bot trading entirely on 2026-08-23:
    #
    #   "has this model ever seen an outcome?"      -> model_is_trained
    #   "has this number been checked against       -> is_measured
    #    observed frequencies?"
    #
    # The second cannot become true before the first trade closes: calibration
    # needs a bot-scorecard, which needs trade episodes, which need trades. A
    # composer that demanded it was demanding a measurement that trading itself
    # has to produce. Defaulted so a conviction built before this field existed
    # reads as untrained rather than as trained-by-omission.
    model_observations: int = 0
    model_is_trained: bool = False

    @property
    def probability(self) -> float:
        return self.calibrated.value

    @property
    def is_measured(self) -> bool:
        """Whether this number has been checked against what actually happened.

        False until enough trades have closed for the calibrator to fit. It is a
        statement about the *calibration*, never about the model.
        """
        return self.calibrated.is_fitted


@dataclass(frozen=True)
class EntryTiming:
    """When to act, which is a separate question from whether to.

    A right setup entered at the wrong moment is a losing trade, so this is its
    own part and its own type rather than a field on the opinion.
    """

    bot: str
    venue_id: str
    symbol: str
    side: str
    action: str
    trigger_price: float | None
    valid_until_ns: int | None
    quality: float | None
    reason: str
    decided_at_ns: int

    @property
    def is_actionable(self) -> bool:
        return self.action in (ENTER_NOW, WAIT_FOR_TRIGGER)


@dataclass(frozen=True)
class ExitTarget:
    """One place to take part of the position off, and how much."""

    price: float
    fraction: float
    reason: str


@dataclass(frozen=True)
class ExitPlan:
    """Where the trade is wrong, where it is finished, and when it has expired.

    Built before entry on purpose. A stop chosen after a position is open is
    chosen by whoever is losing money, and the excursion profile that says how
    far this symbol normally goes against a winner is only honest before there
    is a position to defend.
    """

    bot: str
    venue_id: str
    symbol: str
    side: str
    stop_price: float
    targets: tuple
    invalidation_reason: str
    horizon_seconds: float
    risk_fraction: float
    reward_to_risk: float | None
    reason: str
    planned_at_ns: int

    @property
    def is_complete(self) -> bool:
        return bool(self.targets) and self.stop_price > 0.0


@dataclass(frozen=True)
class DirectionalOpinion:
    """One bot's whole answer, including the answer that nothing should be done.

    Produced by the opinion composer and by the invalidation watcher, which is
    the same type on purpose: a bot changing its mind about an open position is
    an opinion, not a special control message, and the arbiter weighs it the
    same way.
    """

    bot: str
    side: str
    venue_id: str
    symbol: str
    action: str
    conviction: Estimate
    timing: EntryTiming | None
    exit_plan: ExitPlan | None
    features_summary: dict
    refusal: str | None
    reason: str
    formed_at_ns: int

    @property
    def is_a_call_to_act(self) -> bool:
        return self.action != STAND_DOWN and self.refusal is None

    @property
    def is_about_an_open_position(self) -> bool:
        return self.action in (CLOSE_POSITION, REDUCE_POSITION)


# Where a follow candidate came from. The tailgater has three sources and they
# fail in different ways -- a scanner move can be a print, our own winner can be
# one we are already too big in, a tracked trader can be someone we cannot keep
# up with -- so the source travels with the candidate rather than being inferred.
FROM_A_SCANNER_MOVE = "scanner-continuation"
FROM_OUR_OWN_WINNER = "our-own-winning-leg"
FROM_A_TRACKED_TRADER = "tracked-trader-position"


@dataclass(frozen=True)
class FollowCandidate:
    """Something already working that the tailgating bot may join.

    `move_so_far` and `move_normal` are both fractions of price and both are
    measured: the first from the tape, the second from this symbol's own recorded
    moves. Their ratio is how much of a typical move has already happened, which
    is the number this whole bot turns on -- a move that has barely started might
    still reverse, and one past what this symbol normally covers is a move to be
    on the other side of.
    """

    bot: str
    source: str
    venue_id: str
    symbol: str
    direction: str
    move_so_far: float
    move_normal: float
    observations_in_move: int
    entry_cost_fraction: float | None
    setup_weight: float
    detector: str
    evidence: dict
    reason: str
    qualified_at_ns: int

    @property
    def fraction_of_a_normal_move_done(self) -> float:
        return self.move_so_far / self.move_normal if self.move_normal > 0 else 0.0


# What the tailgating bot's two readings look like. Both are produced by one
# part and consumed by others, and no part may import another (T-4).

FROM_HISTORY = "this-symbol's-own-recorded-moves"
FROM_FORECAST = "the-price-forecast"
FROM_DECAY = "the-move's-own-rate-of-progress"

ESTIMATES_AGREE = "the-estimates-agree"
ESTIMATES_DISAGREE = "the-estimates-disagree"
ONLY_ONE_ESTIMATE = "only-one-estimate-could-be-made"
NO_ESTIMATE_AVAILABLE = "no-estimate-could-be-made"


@dataclass(frozen=True)
class MoveRemaining:
    """How much of a move is left, and how much the independent views disagree.

    `spread` is not a confidence interval. It is the distance between estimates
    that were made independently, so a wide one means the views contradict each
    other -- a different thing from a wide distribution, and the parts below act
    on the difference.
    """

    bot: str
    venue_id: str
    symbol: str
    direction: str
    remaining_fraction: float | None
    lowest: float | None
    highest: float | None
    estimates: dict
    agreement: str
    reason: str
    estimated_at_ns: int

    @property
    def spread(self) -> float | None:
        if self.lowest is None or self.highest is None:
            return None
        return self.highest - self.lowest

    @property
    def is_sizeable(self) -> bool:
        """Whether anything below should act on this at all."""
        return self.remaining_fraction is not None and self.agreement != ESTIMATES_DISAGREE


FROM_THE_BOOK = "the-book-is-one-sided"
# FROM_FUNDING replaced by FROM_ORDER_FLOW 2026-09-01 (options-segment-bots
# conversion): funding rate has no Indian equivalent; order-flow imbalance
# from broker-open-interest plays the same role -- the price of consensus,
# in a market that doesn't charge one directly. FROM_SENTIMENT retired with
# no replacement: no Indian sentiment data source exists yet.
FROM_ORDER_FLOW = "order-flow-favours-one-side"

NOT_CROWDED = "not-crowded"
CROWDED = "crowded"
CROWDING_NOT_MEASURED = "not-measured"


@dataclass(frozen=True)
class CrowdingReading:
    """How crowded a move is, per source, and which source says so.

    Unmeasured is its own state rather than a low reading (Rule 8): a symbol
    nobody could read is not an uncrowded symbol.
    """

    bot: str
    venue_id: str
    symbol: str
    direction: str
    state: str
    tripped_by: tuple
    readings: dict
    sources_measured: int
    sources_unavailable: tuple
    reason: str
    read_at_ns: int

    @property
    def is_crowded(self) -> bool:
        return self.state == CROWDED

    @property
    def is_measured(self) -> bool:
        return self.state != CROWDING_NOT_MEASURED


@dataclass(frozen=True)
class SetupWeight:
    """How much one bot should trust one detector's setups, learned from results."""

    bot: str
    detector: str
    weight: float
    hit_rate: Estimate
    trades_judged: int
    reason: str
    learned_at_ns: int


@dataclass
class ScorecardEntry:
    trades: int = 0
    wins: int = 0
    realised: float = 0.0


@dataclass
class BotScorecard:
    """What actually happened after this bot's calls, split by what it claimed.

    Split by detector and by regime because a bot with one number describes
    neither of the two markets it works in, and the weight learner needs to know
    which setup stopped working rather than that something did.
    """

    bot: str
    _by_detector: dict = field(default_factory=dict)
    _by_regime: dict = field(default_factory=dict)
    _by_probability_band: dict = field(default_factory=dict)

    def record_closed_trade(
        self, detector: str, regime: str, stated_probability: float,
        was_win: bool, realised: float,
    ) -> None:
        band = min(9, max(0, int(stated_probability * 10)))
        for table, key in (
            (self._by_detector, detector),
            (self._by_regime, regime),
            (self._by_probability_band, band),
        ):
            entry = table.get(key)
            if entry is None:
                entry = ScorecardEntry()
                table[key] = entry
            entry.trades += 1
            entry.wins += 1 if was_win else 0
            entry.realised += realised

    def detector_record(self, detector: str) -> ScorecardEntry:
        return self._by_detector.get(detector, ScorecardEntry())

    def regime_record(self, regime: str) -> ScorecardEntry:
        return self._by_regime.get(regime, ScorecardEntry())

    @property
    def trades(self) -> int:
        return sum(entry.trades for entry in self._by_detector.values())

    def describe(self) -> dict:
        return {
            "bot": self.bot,
            "trades": self.trades,
            "by_detector": {
                detector: {"trades": entry.trades, "wins": entry.wins, "realised": entry.realised}
                for detector, entry in sorted(self._by_detector.items())
            },
            "by_regime": {
                regime: {"trades": entry.trades, "wins": entry.wins, "realised": entry.realised}
                for regime, entry in sorted(self._by_regime.items())
            },
            "by_probability_band": {
                f"{band / 10:.1f}-{(band + 1) / 10:.1f}": {
                    "trades": entry.trades, "wins": entry.wins, "realised": entry.realised
                }
                for band, entry in sorted(self._by_probability_band.items())
            },
        }


def stand_down(
    bot: str, side: str, venue_id: str, symbol: str, refusal: str, reason: str,
    conviction: Estimate | None = None, now_ns=time.time_ns,
) -> DirectionalOpinion:
    """The answer a bot must be able to give, with the reason it is giving it.

    A refusal is published rather than dropped: a symbol nothing was said about
    is indistinguishable from a symbol nobody looked at, and Rule 8 says an
    absence must render as its own state rather than as silence.
    """
    return DirectionalOpinion(
        bot=bot,
        side=side,
        venue_id=venue_id,
        symbol=symbol,
        action=STAND_DOWN,
        conviction=conviction
        or Estimate(
            value=0.0, is_fitted=False, observations=0, prior=0.0,
            was_clamped=False, bound_low=None, bound_high=None, reason=refusal,
        ),
        timing=None,
        exit_plan=None,
        features_summary={},
        refusal=refusal,
        reason=reason,
        formed_at_ns=now_ns(),
    )
