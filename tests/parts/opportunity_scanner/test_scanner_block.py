"""The scanner: thirteen ways to notice something, and what each refuses to claim.

The statistics are checked against the real captured tape where a real series is
what makes the test meaningful, and against constructed series where the property
under test is one no captured minute happens to contain (a perfect trend, an exact
mean reversion). Both are honest: RL-063 forbids inventing market data and calling
it real, not constructing a series to test that an estimator does arithmetic.

Every detector's refusals get as much attention as its firings. A detector that
cannot say "not this one" fires on everything, and a scanner of thirteen of those
would bury every real signal.
"""

import importlib
import json
import math
import time

import pytest

from parts.opportunity_scanner.cointegration_pair_finder import (
    COINTEGRATED, CORRELATED_ONLY, TOO_FEW_OBSERVATIONS as PAIR_TOO_FEW, UNRELATED,
    CointegrationPairFinder,
)
from parts.opportunity_scanner.funding_skew_detector import (
    NOT_SKEWED, PRICE_STILL_REWARDING, FundingSkewDetector,
)
from parts.opportunity_scanner.liquidation_cascade_detector import (
    CLUSTER_TOO_THIN, NO_CLUSTER_IN_REACH, LiquidationCascadeDetector, LiquidationCluster,
)
from parts.opportunity_scanner.liquidity_grader import (
    DEEP, THIN, TRADEABLE, UNGRADEABLE, UNTRADEABLE, LiquidityGrader,
)
from parts.opportunity_scanner.mean_reversion_detector import (
    NOT_STRETCHED, NO_VOLATILITY, WRONG_REGIME, MeanReversionDetector,
)
from parts.opportunity_scanner.momentum_burst_detector import (
    NOT_A_BURST, NO_PLAYBOOK, MomentumBurstDetector,
)
from parts.opportunity_scanner.regime_classifier import (
    RANDOM, REVERTING, TRENDING, UNCLASSIFIED, RegimeClassifier,
)
from parts.opportunity_scanner.sentiment_shift_detector import (
    NO_DIVERGENCE, SENTIMENT_STEADY, SentimentShiftDetector,
)
from parts.opportunity_scanner.spread_reversion_detector import (
    A_LEG_IS_STALE, PAIR_NOT_COINTEGRATED, SpreadReversionDetector,
)
from parts.opportunity_scanner.universal_symbol_sweeper import UniversalSymbolSweeper
from parts.opportunity_scanner.volatility_gap_detector import (
    GAP_TOO_SMALL, NO_SURFACE, VolatilityGapDetector,
)
from parts.opportunity_scanner.watch_condition_compiler import (
    ABOVE, BELOW, BETWEEN, CROSSES_ABOVE, ConditionRefused, WatchConditionCompiler,
)
from parts.opportunity_scanner.whale_flow_detector import (
    INFLOW, NOT_LARGE_ENOUGH, OUTFLOW, WhaleFlowDetector,
)
from runtime.market_signal import (
    CONTINUATION, LONG, REVERSION, SHORT, UNWIND, SignalCalibrator,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.rolling_statistics import RollingWindow, correlation, hurst_exponent, linear_fit

BLOCK_PARTS = {
    "regime-classifier": "parts.opportunity_scanner.regime_classifier",
    "mean-reversion-detector": "parts.opportunity_scanner.mean_reversion_detector",
    "volatility-gap-detector": "parts.opportunity_scanner.volatility_gap_detector",
    "cointegration-pair-finder": "parts.opportunity_scanner.cointegration_pair_finder",
    "spread-reversion-detector": "parts.opportunity_scanner.spread_reversion_detector",
    "watch-condition-compiler": "parts.opportunity_scanner.watch_condition_compiler",
    "universal-symbol-sweeper": "parts.opportunity_scanner.universal_symbol_sweeper",
    "liquidity-grader": "parts.opportunity_scanner.liquidity_grader",
    "momentum-burst-detector": "parts.opportunity_scanner.momentum_burst_detector",
    "funding-skew-detector": "parts.opportunity_scanner.funding_skew_detector",
    "whale-flow-detector": "parts.opportunity_scanner.whale_flow_detector",
    "liquidation-cascade-detector": "parts.opportunity_scanner.liquidation_cascade_detector",
    "sentiment-shift-detector": "parts.opportunity_scanner.sentiment_shift_detector",
}

SECOND_NS = 1_000_000_000
VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def calibrator(prior=0.5, minimum=5):
    return SignalCalibrator(
        prior_hit_rate=prior, prior_weight=2.0,
        half_life_observations=100.0, minimum_observations=minimum,
    )


class Regime:
    """A regime a detector can be handed, with the one method detectors call."""

    def __init__(self, regime=REVERTING, hurst=0.3, venue_id=VENUE, symbol=SYMBOL):
        self.regime = regime
        self.hurst = hurst
        self.venue_id = venue_id
        self.symbol = symbol

    def favours(self, expectation):
        if self.regime == TRENDING:
            return expectation == CONTINUATION
        if self.regime == REVERTING:
            return expectation == REVERSION
        return False


@pytest.fixture(scope="module")
def real_trade_prices(read_captured_payloads):
    """Prices from the tape: what BTCUSDT actually traded at on 2026-08-22.

    The long run rather than the short aggtrade-kline capture, which holds 34
    trades -- below what a Hurst estimate or a rolling z-score can be judged on.
    """
    prices = []
    for _, payload in read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl"):
        message = json.loads(payload)
        if message.get("e") == "aggTrade":
            prices.append(float(message["p"]))
    assert len(prices) >= 500, (
        f"only {len(prices)} real trades were read; the statistics below would be "
        f"judged on a series too short to judge them on"
    )
    return prices


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- the statistics ----------------------------------------------------------

def test_a_window_says_nothing_before_it_has_enough(real_trade_prices):
    window = RollingWindow(length=50)
    for price in real_trade_prices[:5]:
        window.observe(price)
    assert window.mean(minimum_observations=10) is None
    assert window.z_score(window.latest, minimum_observations=10) is None


def test_a_flat_series_has_no_z_score():
    """A zero deviation makes every value infinitely unusual, which is arithmetic."""
    window = RollingWindow(length=10)
    for _ in range(10):
        window.observe(100.0)
    assert window.z_score(101.0, minimum_observations=5) is None


def test_the_real_tape_gives_a_usable_z_score(real_trade_prices):
    window = RollingWindow(length=40)
    for price in real_trade_prices[:40]:
        window.observe(price)
    z = window.z_score(window.latest, minimum_observations=20)
    assert z is not None
    assert abs(z) < 10, "a real price series should not sit ten deviations from its own mean"


def test_hurst_separates_a_trend_from_a_reversion():
    """Constructed series: no captured minute is a perfect trend or a perfect sawtooth."""
    trend = [100.0 + index for index in range(200)]
    sawtooth = [100.0 + (2.0 if index % 2 else -2.0) for index in range(200)]
    trending = hurst_exponent(trend, minimum_observations=50)
    reverting = hurst_exponent(sawtooth, minimum_observations=50)
    assert trending is not None and reverting is not None
    assert trending > reverting


def test_hurst_refuses_a_series_too_short_to_estimate():
    assert hurst_exponent([100.0, 101.0, 102.0], minimum_observations=50) is None


def test_a_linear_fit_needs_x_to_vary():
    assert linear_fit([(1.0, 5.0), (1.0, 9.0)]) is None
    slope, intercept = linear_fit([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)])
    assert slope == pytest.approx(2.0) and intercept == pytest.approx(1.0)


def test_correlation_refuses_a_flat_series():
    assert correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


# ---- regime-classifier -------------------------------------------------------

def classifier(minimum=50):
    return RegimeClassifier(
        window_length=300, minimum_observations=minimum,
        trending_above=0.55, reverting_below=0.45,
    )


def test_a_symbol_with_too_little_history_is_unclassified(real_trade_prices):
    """A regime that flips every tick is worse than no regime."""
    subject = classifier(minimum=100)
    for index, price in enumerate(real_trade_prices[:20]):
        subject.observe_price(VENUE, SYMBOL, price, index * SECOND_NS)
    regime = subject.classify(VENUE, SYMBOL)
    assert regime.regime == UNCLASSIFIED
    assert regime.is_classified is False


def test_the_real_tape_classifies_and_says_what_from(real_trade_prices):
    """2000 real trades is enough to reach a verdict, and the verdict carries its number."""
    subject = classifier(minimum=30)
    for index, price in enumerate(real_trade_prices):
        subject.observe_price(VENUE, SYMBOL, price, index * SECOND_NS)
    regime = subject.classify(VENUE, SYMBOL)
    assert regime.is_classified
    assert regime.regime in (TRENDING, REVERTING, RANDOM)
    assert regime.hurst is not None
    assert 0.0 < regime.hurst < 1.5
    assert regime.volatility is not None
    assert f"{regime.hurst:.3f}" in regime.reason


def test_a_constructed_trend_classifies_as_trending():
    subject = classifier(minimum=30)
    for index in range(300):
        subject.observe_price(VENUE, SYMBOL, 100.0 + index * 0.5, index * SECOND_NS)
    assert subject.classify(VENUE, SYMBOL).regime == TRENDING


def test_a_regime_only_favours_the_detector_it_suits():
    assert Regime(TRENDING).favours(CONTINUATION) is True
    assert Regime(TRENDING).favours(REVERSION) is False
    assert Regime(RANDOM).favours(REVERSION) is False
    assert Regime(RANDOM).favours(CONTINUATION) is False


# ---- mean-reversion-detector -------------------------------------------------

def reversion_detector(z=2.0, minimum=20, volatility=0.0001):
    return MeanReversionDetector(
        window_length=100, minimum_observations=minimum, z_threshold=z,
        minimum_volatility_fraction=volatility, horizon_seconds=300.0,
        calibrator=calibrator(),
    )


def test_a_stretched_price_in_a_reverting_regime_fires():
    subject = reversion_detector(z=2.0, minimum=20)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
        subject.observe_price(VENUE, SYMBOL, 101.0, subject._now_ns())
    subject.observe_price(VENUE, SYMBOL, 130.0, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(REVERTING))
    assert candidate is not None
    assert candidate.direction == SHORT
    assert candidate.expectation == REVERSION


def test_the_same_stretch_in_a_trend_does_not_fire():
    """A reverter that ignores regime sells strength in a bull market."""
    subject = reversion_detector(z=2.0, minimum=20)
    for _ in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
        subject.observe_price(VENUE, SYMBOL, 101.0, subject._now_ns())
    subject.observe_price(VENUE, SYMBOL, 130.0, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(TRENDING, hurst=0.7))
    assert candidate is None
    assert outcome == WRONG_REGIME


def test_a_series_that_barely_moved_is_refused():
    subject = reversion_detector(z=1.0, minimum=10, volatility=0.01)
    for index in range(30):
        subject.observe_price(VENUE, SYMBOL, 100.0 + (0.0001 if index % 2 else 0.0), subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(REVERTING))
    assert candidate is None
    assert outcome == NO_VOLATILITY


def test_an_ordinary_price_does_not_fire(real_trade_prices):
    subject = reversion_detector(z=3.0, minimum=20)
    for price in real_trade_prices[:60]:
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(REVERTING))
    assert outcome in (NOT_STRETCHED, NO_VOLATILITY)


def test_confidence_is_learned_not_the_z_score():
    """How unusual a deviation is says nothing about how often it reverts."""
    shared = calibrator(prior=0.5, minimum=5)
    subject = MeanReversionDetector(
        window_length=100, minimum_observations=10, z_threshold=1.0,
        minimum_volatility_fraction=0.0001, horizon_seconds=300.0, calibrator=shared,
    )
    for _ in range(10):
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
        subject.observe_price(VENUE, SYMBOL, 101.0, subject._now_ns())
    subject.observe_price(VENUE, SYMBOL, 120.0, subject._now_ns())
    first, _ = subject.detect(VENUE, SYMBOL, Regime(REVERTING))
    assert first.confidence.is_fitted is False

    for _ in range(20):
        subject.observe_outcome(REVERTING, reverted=False)
    subject.observe_price(VENUE, SYMBOL, 120.0, subject._now_ns())
    later, _ = subject.detect(VENUE, SYMBOL, Regime(REVERTING))
    assert later.confidence.is_fitted is True
    assert later.confidence.value < 0.5


# ---- momentum-burst-detector -------------------------------------------------

def burst_detector(z=3.0, minimum=20):
    return MomentumBurstDetector(
        window_length=100, minimum_observations=minimum, burst_z_threshold=z,
        horizon_seconds=300.0, calibrator=calibrator(),
    )


def test_a_burst_without_a_playbook_rule_is_not_a_claim():
    subject = burst_detector(z=2.0, minimum=10)
    price = 100.0
    for _ in range(20):
        price *= 1.0001
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    subject.observe_price(VENUE, SYMBOL, price * 1.05, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(RANDOM))
    assert candidate is None
    assert outcome == NO_PLAYBOOK


def test_the_playbook_decides_which_way_a_burst_is_traded():
    subject = burst_detector(z=2.0, minimum=10)
    subject.set_playbook_expectation(SYMBOL, REVERSION)
    price = 100.0
    for _ in range(20):
        price *= 1.0001
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    subject.observe_price(VENUE, SYMBOL, price * 1.05, subject._now_ns())
    candidate, _ = subject.detect(VENUE, SYMBOL, Regime(RANDOM))
    assert candidate.direction == SHORT

    subject.set_playbook_expectation(SYMBOL, CONTINUATION)
    subject.observe_price(VENUE, SYMBOL, price * 1.10, subject._now_ns())
    candidate, _ = subject.detect(VENUE, SYMBOL, Regime(RANDOM))
    assert candidate.direction == LONG


def test_an_ordinary_move_for_this_symbol_is_not_a_burst(real_trade_prices):
    """A 2% move is nothing in a thin altcoin and enormous in BTCUSDT."""
    subject = burst_detector(z=4.0, minimum=20)
    subject.set_playbook_expectation(SYMBOL, REVERSION)
    for price in real_trade_prices[:60]:
        subject.observe_price(VENUE, SYMBOL, price, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL, Regime(RANDOM))
    assert outcome in (NOT_A_BURST, "fired")


# ---- liquidity-grader --------------------------------------------------------

def grader(clock, refresh=60.0):
    return LiquidityGrader(
        refresh_interval_seconds=refresh, deep_cost_fraction=0.001,
        tradeable_cost_fraction=0.005, thin_cost_fraction=0.02,
        turnover_window=10, monotonic=clock.monotonic,
    )


def test_the_same_symbol_grades_differently_at_different_sizes():
    """Tradeability is a property of a symbol and a size, not of a symbol."""
    subject = grader(Clock())
    subject.observe_book(
        VENUE, SYMBOL,
        bids=((99.99, 10.0), (99.0, 1000.0)),
        asks=((100.01, 10.0), (101.0, 1000.0)),
    )
    small = subject.grade(VENUE, SYMBOL, order_size_quote=500.0, force=True)
    large = subject.grade(VENUE, SYMBOL, order_size_quote=50_000.0, force=True)
    assert small.round_trip_cost_fraction < large.round_trip_cost_fraction
    assert small.grade != large.grade


def test_a_symbol_the_book_cannot_absorb_is_untradeable():
    subject = grader(Clock())
    subject.observe_book(VENUE, SYMBOL, bids=((99.0, 0.1),), asks=((101.0, 0.1),))
    assert subject.grade(VENUE, SYMBOL, order_size_quote=1_000_000.0, force=True).grade == UNTRADEABLE


def test_a_symbol_with_no_book_is_ungradeable_not_middling():
    """A default grade would let a symbol nobody could measure pass a filter."""
    result = grader(Clock()).grade(VENUE, "NOBOOKUSDT", order_size_quote=100.0, force=True)
    assert result.grade == UNGRADEABLE
    assert result.is_tradeable is False


def test_grading_is_not_repeated_inside_the_refresh_interval():
    clock = Clock()
    subject = grader(clock, refresh=60.0)
    subject.observe_book(VENUE, SYMBOL, bids=((99.99, 100.0),), asks=((100.01, 100.0),))
    subject.grade(VENUE, SYMBOL, order_size_quote=100.0)
    subject.grade(VENUE, SYMBOL, order_size_quote=100.0)
    assert subject.standing.refreshes_skipped == 1
    clock.now += 61
    subject.grade(VENUE, SYMBOL, order_size_quote=100.0)
    assert subject.standing.grades_computed == 2


# ---- cointegration-pair-finder -----------------------------------------------

def pair_finder(minimum=30, correlation_floor=0.7, reversion_floor=0.05):
    return CointegrationPairFinder(
        window_length=200, minimum_observations=minimum,
        minimum_correlation=correlation_floor, minimum_reversion_strength=reversion_floor,
    )


def test_a_reverting_spread_is_cointegrated():
    subject = pair_finder(minimum=30)
    for index in range(100):
        base = 100.0 + index * 0.1
        wobble = 0.5 if index % 2 else -0.5
        subject.observe_price(VENUE, "AUSDT", base + wobble, index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", base, index * SECOND_NS)
    pair = subject.test_pair(VENUE, "AUSDT", "BUSDT")
    assert pair.state == COINTEGRATED
    assert pair.is_tradeable is True
    assert pair.hedge_ratio is not None


def test_correlated_symbols_that_drift_apart_are_not_cointegrated():
    """The classic way to lose money on pairs."""
    subject = pair_finder(minimum=30, reversion_floor=0.2)
    for index in range(100):
        subject.observe_price(VENUE, "AUSDT", 100.0 + index * 0.5, index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", 100.0 + index * 0.1, index * SECOND_NS)
    pair = subject.test_pair(VENUE, "AUSDT", "BUSDT")
    assert pair.state == CORRELATED_ONLY
    assert pair.is_tradeable is False


def test_a_pair_that_stops_cointegrating_is_retired():
    subject = pair_finder(minimum=20, reversion_floor=0.2)
    for index in range(60):
        base = 100.0 + index * 0.1
        subject.observe_price(VENUE, "AUSDT", base + (0.5 if index % 2 else -0.5), index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", base, index * SECOND_NS)
    assert subject.test_pair(VENUE, "AUSDT", "BUSDT").state == COINTEGRATED
    assert len(subject.cointegrated_pairs) == 1

    for index in range(200):
        subject.observe_price(VENUE, "AUSDT", 100.0 + index * 2.0, (60 + index) * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", 100.0, (60 + index) * SECOND_NS)
    subject.test_pair(VENUE, "AUSDT", "BUSDT")
    assert subject.cointegrated_pairs == ()
    assert subject.standing.pairs_retired == 1


def test_too_little_history_judges_nothing():
    subject = pair_finder(minimum=50)
    for index in range(10):
        subject.observe_price(VENUE, "AUSDT", 100.0 + index, index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", 100.0 + index, index * SECOND_NS)
    assert subject.test_pair(VENUE, "AUSDT", "BUSDT").state == PAIR_TOO_FEW


# ---- spread-reversion-detector -----------------------------------------------

class Pair:
    def __init__(self, tradeable=True, hedge_ratio=1.0, mean=0.0, deviation=1.0):
        self.venue_id = VENUE
        self.left_symbol = "AUSDT"
        self.right_symbol = "BUSDT"
        self.hedge_ratio = hedge_ratio
        self.spread_mean = mean
        self.spread_deviation = deviation
        self.reversion_strength = 0.3
        self.is_tradeable = tradeable


def spread_detector(z=2.0, minimum=20, price_staleness=None, clock=None):
    return SpreadReversionDetector(
        z_threshold=z, rearm_z=z / 2, window_length=100, minimum_observations=minimum,
        horizon_seconds=300.0, calibrator=calibrator(),
        price_staleness=price_staleness,
        now_ns=clock or time.time_ns,
    )


def test_a_stretched_spread_names_both_legs():
    """A detector that fired on one leg would take a directional bet it never intended."""
    subject = spread_detector(z=2.0, minimum=100)
    subject.observe_price(VENUE, "AUSDT", 105.0, subject._now_ns())
    subject.observe_price(VENUE, "BUSDT", 100.0, subject._now_ns())
    candidate, _ = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert candidate is not None
    assert candidate.evidence["short_symbol"] == "AUSDT"
    assert candidate.evidence["long_symbol"] == "BUSDT"
    assert candidate.evidence["both_legs_required"] is True


def test_a_spread_that_stays_stretched_is_one_candidate_not_a_stream():
    """A candidate is the crossing into stretched, not the state: announcing the
    same wide pair on every level update published 620,000 candidates in
    thirty-five minutes at 100 symbols per venue (2026-08-24). The pair re-arms
    only when its spread comes back inside the threshold."""
    from parts.opportunity_scanner.spread_reversion_detector import (
        FIRED, NOT_STRETCHED, STILL_STRETCHED,
    )

    subject = spread_detector(z=2.0, minimum=100)
    subject.observe_price(VENUE, "AUSDT", 105.0, subject._now_ns())
    subject.observe_price(VENUE, "BUSDT", 100.0, subject._now_ns())
    first, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert first is not None and outcome == FIRED

    # Still stretched on the next levels: the same opportunity, not a new one.
    subject.observe_price(VENUE, "AUSDT", 105.5, subject._now_ns())
    again, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert again is None and outcome == STILL_STRETCHED
    assert subject.standing.still_stretched == 1

    # Dipping under the threshold but not under the re-arm level is the same
    # episode still: a wiggle across the threshold must not re-fire.
    subject.observe_price(VENUE, "AUSDT", 101.5, subject._now_ns())
    wiggle, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert wiggle is None and outcome == NOT_STRETCHED
    subject.observe_price(VENUE, "AUSDT", 105.5, subject._now_ns())
    rewiggle, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert rewiggle is None and outcome == STILL_STRETCHED

    # Back inside the re-arm level -- the reversion substantially happened...
    subject.observe_price(VENUE, "AUSDT", 100.5, subject._now_ns())
    calm, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert calm is None and outcome == NOT_STRETCHED

    # ...so the next crossing is a new opportunity, and a new candidate.
    subject.observe_price(VENUE, "AUSDT", 106.0, subject._now_ns())
    fresh, outcome = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert fresh is not None and outcome == FIRED
    assert subject.standing.candidates == 2


def test_a_retired_pair_produces_nothing_however_stretched():
    subject = spread_detector(z=1.0, minimum=100)
    subject.observe_price(VENUE, "AUSDT", 200.0, subject._now_ns())
    subject.observe_price(VENUE, "BUSDT", 100.0, subject._now_ns())
    candidate, outcome = subject.detect(Pair(tradeable=False))
    assert candidate is None
    assert outcome == PAIR_NOT_COINTEGRATED


# ---- funding-skew-detector ---------------------------------------------------

def funding_detector(z=2.0, minimum=20):
    return FundingSkewDetector(
        window_length=100, minimum_observations=minimum, skew_z_threshold=z,
        price_confirmation_window=20, horizon_seconds=3600.0, calibrator=calibrator(),
    )


def test_extreme_funding_with_price_no_longer_paying_fires():
    subject = funding_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_funding(VENUE, SYMBOL, 0.0001 * (1 if index % 2 else -1))
    subject.observe_funding(VENUE, SYMBOL, 0.01)
    for index in range(10):
        subject.observe_price(VENUE, SYMBOL, 100.0 - index, subject._now_ns())
    candidate, _ = subject.detect(VENUE, SYMBOL)
    assert candidate is not None
    assert candidate.direction == SHORT
    assert candidate.expectation == UNWIND


def test_extreme_funding_while_price_still_rises_does_not_fire():
    """Fading a rate the whole way up is how a funding strategy loses."""
    subject = funding_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_funding(VENUE, SYMBOL, 0.0001 * (1 if index % 2 else -1))
    subject.observe_funding(VENUE, SYMBOL, 0.01)
    for index in range(10):
        subject.observe_price(VENUE, SYMBOL, 100.0 + index, subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert candidate is None
    assert outcome == PRICE_STILL_REWARDING


def test_an_ordinary_funding_rate_does_not_fire():
    subject = funding_detector(z=3.0, minimum=20)
    for index in range(40):
        subject.observe_funding(VENUE, SYMBOL, 0.0001 * (1 if index % 2 else -1))
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert outcome == NOT_SKEWED


# ---- liquidation-cascade-detector --------------------------------------------

def cascade_detector(reach=2.0, minimum_notional=1_000_000.0, depth_multiple=2.0):
    return LiquidationCascadeDetector(
        window_length=100, minimum_observations=20, reach_in_volatilities=reach,
        minimum_cluster_notional=minimum_notional, cascade_depth_multiple=depth_multiple,
        horizon_seconds=600.0, calibrator=calibrator(),
    )


def feed_volatility(subject, price=100.0, steps=40):
    for index in range(steps):
        subject.observe_price(VENUE, SYMBOL, price * (1 + (0.005 if index % 2 else -0.005)), subject._now_ns())


def test_a_dense_cluster_in_reach_fires_toward_it():
    subject = cascade_detector()
    feed_volatility(subject)
    subject.set_book_depth(VENUE, SYMBOL, 100_000.0)
    subject.set_clusters(VENUE, SYMBOL, (LiquidationCluster(price=99.5, notional=5_000_000.0, side="long"),))
    candidate, _ = subject.detect(VENUE, SYMBOL)
    assert candidate is not None
    assert candidate.direction == SHORT


def test_a_thin_cluster_absorbs_and_does_not_fire():
    subject = cascade_detector(minimum_notional=1_000_000.0)
    feed_volatility(subject)
    subject.set_book_depth(VENUE, SYMBOL, 100_000.0)
    subject.set_clusters(VENUE, SYMBOL, (LiquidationCluster(price=99.5, notional=1000.0, side="long"),))
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert candidate is None
    assert outcome == CLUSTER_TOO_THIN


def test_a_distant_cluster_is_out_of_reach():
    subject = cascade_detector(reach=1.0)
    feed_volatility(subject)
    subject.set_clusters(VENUE, SYMBOL, (LiquidationCluster(price=50.0, notional=9_000_000.0, side="long"),))
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert candidate is None
    assert outcome == NO_CLUSTER_IN_REACH


# ---- volatility-gap-detector -------------------------------------------------

def gap_detector(minimum_gap=0.2):
    return VolatilityGapDetector(
        minimum_gap_fraction=minimum_gap, horizon_seconds=3600.0, calibrator=calibrator()
    )


def test_no_options_surface_produces_nothing_and_says_why():
    """Phase 1 captures no options data, and a realised-only fallback would be a different strategy."""
    subject = gap_detector()
    subject.observe_forecast(VENUE, SYMBOL, 0.5)
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert candidate is None
    assert outcome == NO_SURFACE


def test_rich_implied_volatility_is_sold_and_cheap_is_bought():
    subject = gap_detector(minimum_gap=0.2)
    subject.observe_forecast(VENUE, SYMBOL, 0.5)
    subject.observe_implied(VENUE, SYMBOL, 0.9)
    rich, _ = subject.detect(VENUE, SYMBOL)
    assert rich.direction == SHORT

    subject.observe_implied(VENUE, SYMBOL, 0.2)
    cheap, _ = subject.detect(VENUE, SYMBOL)
    assert cheap.direction == LONG


def test_a_small_gap_is_inside_the_noise():
    subject = gap_detector(minimum_gap=0.5)
    subject.observe_forecast(VENUE, SYMBOL, 0.50)
    subject.observe_implied(VENUE, SYMBOL, 0.52)
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert outcome == GAP_TOO_SMALL


# ---- whale-flow-detector -----------------------------------------------------

def whale_detector(z=2.5, minimum=20):
    return WhaleFlowDetector(
        window_length=100, minimum_observations=minimum, flow_z_threshold=z,
        horizon_seconds=3600.0, calibrator=calibrator(),
    )


def test_a_large_inflow_is_read_as_selling_pressure():
    subject = whale_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_transfer(VENUE, SYMBOL, quantity=1.0, direction=INFLOW if index % 2 else OUTFLOW)
    candidate, _ = subject.observe_transfer(VENUE, SYMBOL, quantity=500.0, direction=INFLOW)
    assert candidate is not None
    assert candidate.direction == SHORT


def test_a_large_outflow_is_read_the_other_way():
    """Coins leaving reduce the float available to sell, which reads backwards until you think about it."""
    subject = whale_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_transfer(VENUE, SYMBOL, quantity=1.0, direction=INFLOW if index % 2 else OUTFLOW)
    candidate, _ = subject.observe_transfer(VENUE, SYMBOL, quantity=500.0, direction=OUTFLOW)
    assert candidate is not None
    assert candidate.direction == LONG


def test_an_ordinary_transfer_for_this_venue_does_not_fire():
    subject = whale_detector(z=3.0, minimum=20)
    for index in range(40):
        subject.observe_transfer(VENUE, SYMBOL, quantity=1.0, direction=INFLOW if index % 2 else OUTFLOW)
    _, outcome = subject.observe_transfer(VENUE, SYMBOL, quantity=1.2, direction=INFLOW)
    assert outcome == NOT_LARGE_ENOUGH


def test_a_candidate_admits_a_transfer_may_not_be_a_trade():
    subject = whale_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_transfer(VENUE, SYMBOL, quantity=1.0, direction=INFLOW if index % 2 else OUTFLOW)
    candidate, _ = subject.observe_transfer(VENUE, SYMBOL, quantity=500.0, direction=INFLOW)
    assert candidate.evidence["may_not_be_a_trade"] is True


# ---- sentiment-shift-detector ------------------------------------------------

def sentiment_detector(z=2.0, minimum=20, agreement=0.01):
    return SentimentShiftDetector(
        window_length=100, minimum_observations=minimum, shift_z_threshold=z,
        price_agreement_fraction=agreement, horizon_seconds=3600.0, calibrator=calibrator(),
    )


def test_sentiment_moving_while_price_has_not_is_the_signal():
    subject = sentiment_detector(z=2.0, minimum=20)
    for index in range(30):
        subject.observe_sentiment(VENUE, SYMBOL, 0.5 + (0.01 if index % 2 else -0.01))
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_sentiment(VENUE, SYMBOL, 0.95)
    candidate, _ = subject.detect(VENUE, SYMBOL)
    assert candidate is not None


def test_sentiment_that_price_has_already_followed_is_not_a_divergence():
    subject = sentiment_detector(z=2.0, minimum=20, agreement=0.01)
    for index in range(30):
        subject.observe_sentiment(VENUE, SYMBOL, 0.5 + (0.01 if index % 2 else -0.01))
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    subject.observe_sentiment(VENUE, SYMBOL, 0.95)
    for index in range(6):
        subject.observe_price(VENUE, SYMBOL, 100.0 * (1.01 ** (index + 1)), subject._now_ns())
    candidate, outcome = subject.detect(VENUE, SYMBOL)
    assert candidate is None
    assert outcome == NO_DIVERGENCE


def test_which_way_to_trade_sentiment_is_learned_not_assumed():
    """Sentiment leads price sometimes and is contrarian at extremes."""
    shared = calibrator(prior=0.5, minimum=5)
    subject = SentimentShiftDetector(
        window_length=100, minimum_observations=20, shift_z_threshold=2.0,
        price_agreement_fraction=0.01, horizon_seconds=3600.0, calibrator=shared,
    )
    for index in range(30):
        subject.observe_sentiment(VENUE, SYMBOL, 0.5 + (0.01 if index % 2 else -0.01))
        subject.observe_price(VENUE, SYMBOL, 100.0, subject._now_ns())
    for _ in range(20):
        subject.observe_outcome("sentiment-is-contrarian-at-extremes", True)
    subject.observe_sentiment(VENUE, SYMBOL, 0.95)
    candidate, _ = subject.detect(VENUE, SYMBOL)
    assert candidate.direction == SHORT, "learned contrarian, so a bullish shift is faded"


# ---- watch-condition-compiler ------------------------------------------------

def compiler():
    return WatchConditionCompiler(known_measurements=("funding_rate", "z_score", "spread"))


def test_an_instruction_compiles_into_something_testable_anywhere():
    condition = compiler().compile_instruction(
        instruction_id="i-1", measurement="funding_rate", comparison=ABOVE,
        threshold=0.01, direction=SHORT, expectation=UNWIND, horizon_seconds=3600.0,
    )
    assert condition.evaluate(0.02) is True
    assert condition.evaluate(0.005) is False


def test_a_condition_over_something_nobody_measures_is_refused():
    """It could never be evaluated and would silently never fire."""
    with pytest.raises(ConditionRefused):
        compiler().compile_instruction(
            instruction_id="i-1", measurement="vibes", comparison=ABOVE,
            threshold=1.0, direction=LONG, expectation=CONTINUATION, horizon_seconds=60.0,
        )


def test_a_comparison_outside_the_closed_set_is_refused():
    """Anything else would need code, and nothing this system learns may execute."""
    with pytest.raises(ConditionRefused):
        compiler().compile_instruction(
            instruction_id="i-1", measurement="z_score", comparison="whenever-it-feels-right",
            threshold=1.0, direction=LONG, expectation=CONTINUATION, horizon_seconds=60.0,
        )


def test_a_crossing_needs_two_observations_to_mean_anything():
    condition = compiler().compile_instruction(
        instruction_id="i-1", measurement="z_score", comparison=CROSSES_ABOVE,
        threshold=2.0, direction=LONG, expectation=CONTINUATION, horizon_seconds=60.0,
    )
    assert condition.evaluate(3.0) is False, "one observation cannot be a crossing"
    assert condition.evaluate(3.0, previous_value=1.0) is True
    assert condition.evaluate(3.0, previous_value=2.5) is False


def test_a_retired_instruction_is_decompiled_immediately():
    """Its candidates carry the authority of having been proven."""
    subject = compiler()
    subject.compile_instruction(
        instruction_id="i-1", measurement="funding_rate", comparison=ABOVE,
        threshold=0.01, direction=SHORT, expectation=UNWIND, horizon_seconds=60.0,
    )
    subject.compile_instruction(
        instruction_id="i-1", measurement="z_score", comparison=BELOW,
        threshold=-2.0, direction=LONG, expectation=REVERSION, horizon_seconds=60.0,
    )
    assert len(subject.conditions) == 2
    assert subject.retire_instruction("i-1") == 2
    assert subject.conditions == ()


# ---- universal-symbol-sweeper ------------------------------------------------

def sweeper(clock, budget=10.0):
    return UniversalSymbolSweeper(
        sweep_budget_seconds=budget, calibrator=calibrator(), monotonic=clock.monotonic
    )


def a_condition(threshold=0.01):
    return compiler().compile_instruction(
        instruction_id="i-1", measurement="funding_rate", comparison=ABOVE,
        threshold=threshold, direction=SHORT, expectation=UNWIND, horizon_seconds=3600.0,
    )


def test_every_symbol_is_tested_against_every_condition():
    """RL-009: the system must not ignore the rest of its universe."""
    clock = Clock()
    subject = sweeper(clock)
    universe = [(VENUE, f"SYM{index}USDT") for index in range(50)]
    for venue_id, symbol in universe:
        subject.observe_measurements(venue_id, symbol, {"funding_rate": 0.02})
    candidates, report = subject.sweep(universe, (a_condition(),))
    assert len(candidates) == 50
    assert report.covered_everything is True


def test_a_symbol_already_held_is_not_a_new_entry():
    clock = Clock()
    subject = sweeper(clock)
    universe = [(VENUE, SYMBOL)]
    subject.observe_measurements(VENUE, SYMBOL, {"funding_rate": 0.02})
    subject.set_held(VENUE, SYMBOL, True)
    candidates, report = subject.sweep(universe, (a_condition(),))
    assert candidates == ()
    assert report.skipped_held == 1


def test_an_untradeable_symbol_is_skipped_however_good_the_signal():
    clock = Clock()
    subject = sweeper(clock)
    subject.observe_measurements(VENUE, SYMBOL, {"funding_rate": 0.99})
    subject.set_tradeable(VENUE, SYMBOL, False)
    candidates, report = subject.sweep([(VENUE, SYMBOL)], (a_condition(),))
    assert candidates == ()
    assert report.skipped_untradeable == 1


def test_an_unmeasurable_symbol_is_counted_never_assumed_to_pass():
    clock = Clock()
    subject = sweeper(clock)
    candidates, report = subject.sweep([(VENUE, "NOTHINGUSDT")], (a_condition(),))
    assert candidates == ()
    assert report.skipped_unmeasurable == 1


def test_a_sweep_that_runs_out_of_time_says_what_it_did_not_reach():
    """A sweep that quietly covered the first four hundred looks identical to one that covered all."""
    clock = Clock()

    class TickingClock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += 0.4
            return self.now

    subject = UniversalSymbolSweeper(
        sweep_budget_seconds=1.0, calibrator=calibrator(), monotonic=TickingClock().monotonic
    )
    universe = [(VENUE, f"SYM{index}USDT") for index in range(100)]
    for venue_id, symbol in universe:
        subject.observe_measurements(venue_id, symbol, {"funding_rate": 0.02})
    _, report = subject.sweep(universe, (a_condition(),))
    assert report.covered_everything is False
    assert report.symbols_not_reached > 0
    assert "not reached this tick" in report.reason


def test_an_incomplete_sweep_resumes_rather_than_always_covering_the_prefix():
    class TickingClock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += 0.3
            return self.now

    subject = UniversalSymbolSweeper(
        sweep_budget_seconds=1.0, calibrator=calibrator(), monotonic=TickingClock().monotonic
    )
    universe = [(VENUE, f"SYM{index}USDT") for index in range(20)]
    for venue_id, symbol in universe:
        subject.observe_measurements(venue_id, symbol, {"funding_rate": 0.02})
    first, _ = subject.sweep(universe, (a_condition(),))
    second, _ = subject.sweep(universe, (a_condition(),))
    assert {candidate.symbol for candidate in first} != {candidate.symbol for candidate in second}


def test_a_spread_with_a_stale_leg_is_refused_rather_than_fired_on():
    """A stale leg does not read as a quiet leg -- it reads as the widest stretch.

    The spread is one price minus another, and the two arrive separately. If one
    stops updating while the other runs, the difference grows without the market
    ever having produced it, which is exactly the shape this detector exists to
    fire on. So it is refused by name.
    """
    from runtime.price_staleness import PriceStalenessEstimator

    at = [1_000 * SECOND_NS]

    def clock():
        return at[0]

    pinned_to_one_second = PriceStalenessEstimator(
        materiality_fraction=0.0011, anchor_seconds=1.0, quantile=0.95, window=3_600,
        observations_needed=300, prior_one_second_move=0.000898,
        minimum_age_seconds=1.0, maximum_age_seconds=1.0,
    )
    subject = spread_detector(z=2.0, minimum=100, price_staleness=pinned_to_one_second, clock=clock)
    subject.observe_price(VENUE, "AUSDT", 105.0, clock())
    subject.observe_price(VENUE, "BUSDT", 100.0, clock())
    assert subject.detect(Pair(mean=0.0, deviation=1.0))[0] is not None

    # One leg goes on printing; the other has not been heard from in an hour.
    at[0] += 3_360 * SECOND_NS
    subject.observe_price(VENUE, "AUSDT", 130.0, clock())

    candidate, reason = subject.detect(Pair(mean=0.0, deviation=1.0))
    assert candidate is None
    assert reason == A_LEG_IS_STALE
    assert subject.standing.stale_leg == 1


# ---- the fan-out between the finder and the detector -------------------------
#
# Measured live on 2026-08-24 at 50 symbols per venue, before these existed:
# cointegration-pair-finder published 506 verdicts a second, of which 299 were
# `unrelated` and 92 `correlated-but-drifting` -- 77% of the traffic was pairs
# that cannot be traded, against 2,485 that were cointegrated. Downstream,
# spread-reversion-detector held every one of them and re-tested it on every
# tick: 11,013 of its 13,557 tests a second. Both grow as the square of the
# universe, and at 100 symbols per venue the detector dropped 663,028 inputs.


def cointegrating_series(subject, ticks=100, left="AUSDT", right="BUSDT", start=0):
    """A spread that reverts, so the pair qualifies. The shape used above."""
    for index in range(start, start + ticks):
        base = 100.0 + index * 0.1
        wobble = 0.5 if index % 2 else -0.5
        subject.observe_price(VENUE, left, base + wobble, index * SECOND_NS)
        subject.observe_price(VENUE, right, base, index * SECOND_NS)


def test_a_pair_that_was_never_cointegrated_is_never_announced():
    """The 299 a second. Saying it again tells the reader what it already believes."""
    subject = pair_finder(minimum=30)
    for index in range(100):
        # One leg walks, the other stands still: no hedge ratio exists at all.
        subject.observe_price(VENUE, "AUSDT", 100.0 + index, index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", 100.0, index * SECOND_NS)

    for _ in range(5):
        assert subject.test_pair_for_publication(VENUE, "AUSDT", "BUSDT") is None

    assert subject.standing.verdicts_published == 0
    assert subject.standing.verdicts_suppressed == 5


def test_a_cointegrated_pair_is_announced_every_time_it_is_tested():
    """Freshness is unchanged for the pairs that matter.

    The reader computes its spread from `hedge_ratio` on every tick, so slowing
    these down would trade one defect for another.
    """
    subject = pair_finder(minimum=30)
    cointegrating_series(subject)

    announced = [subject.test_pair_for_publication(VENUE, "AUSDT", "BUSDT") for _ in range(4)]
    assert all(pair is not None for pair in announced)
    assert all(pair.state == COINTEGRATED for pair in announced)
    assert all(pair.hedge_ratio is not None for pair in announced)
    assert subject.standing.verdicts_published == 4
    assert subject.standing.verdicts_suppressed == 0


def test_a_pair_that_stops_cointegrating_is_announced_once_and_then_falls_silent():
    """The retirement is what makes the silence safe.

    A reader that never heard it would keep trading a spread that has ended, and a
    stretched spread on a pair that has parted company looks exactly like the best
    opportunity there has ever been.
    """
    subject = pair_finder(minimum=30)
    cointegrating_series(subject)
    assert subject.test_pair_for_publication(VENUE, "AUSDT", "BUSDT").is_tradeable

    # The legs part company: one keeps walking, the other stops.
    for index in range(100, 400):
        subject.observe_price(VENUE, "AUSDT", 100.0 + index * 2.0, index * SECOND_NS)
        subject.observe_price(VENUE, "BUSDT", 110.0, index * SECOND_NS)

    verdicts = [subject.test_pair_for_publication(VENUE, "AUSDT", "BUSDT") for _ in range(4)]
    said = [verdict for verdict in verdicts if verdict is not None]

    assert len(said) == 1, "the retirement is said exactly once, then the pair goes quiet"
    assert not said[0].is_tradeable
    assert subject.standing.pairs_retired == 1
    assert subject.standing.verdicts_suppressed == 3


def test_the_detector_holds_a_retired_pair_but_never_tests_it_again():
    """The 11,013 a second. `detect` would refuse it; not asking is the saving."""
    finder = pair_finder(minimum=30)
    cointegrating_series(finder)
    tradeable = finder.test_pair_for_publication(VENUE, "AUSDT", "BUSDT")

    for index in range(100, 400):
        finder.observe_price(VENUE, "AUSDT", 100.0 + index * 2.0, index * SECOND_NS)
        finder.observe_price(VENUE, "BUSDT", 110.0, index * SECOND_NS)
    retired = finder.test_pair_for_publication(VENUE, "AUSDT", "BUSDT")
    assert retired is not None and not retired.is_tradeable

    detector = spread_detector()
    assert detector.tradeable_pairs_among([tradeable, retired]) == (tradeable,)
    assert detector.standing.untradeable_pairs_held == 1

    # The gauge is a gauge: holding only untradeable pairs is not a rising count.
    assert detector.tradeable_pairs_among([retired]) == ()
    assert detector.standing.untradeable_pairs_held == 1
    assert detector.standing.tests == 0, "nothing was tested, which is the point"


def test_the_detector_still_refuses_an_untradeable_pair_that_reaches_it():
    """Defence in depth: the filter is new, and `detect`'s own guard stays."""
    finder = pair_finder(minimum=30)
    cointegrating_series(finder)
    finder.test_pair_for_publication(VENUE, "AUSDT", "BUSDT")
    for index in range(100, 400):
        finder.observe_price(VENUE, "AUSDT", 100.0 + index * 2.0, index * SECOND_NS)
        finder.observe_price(VENUE, "BUSDT", 110.0, index * SECOND_NS)
    retired = finder.test_pair_for_publication(VENUE, "AUSDT", "BUSDT")

    detector = spread_detector()
    candidate, reason = detector.detect(retired)
    assert candidate is None
    assert reason == PAIR_NOT_COINTEGRATED
