"""The closed-trade-decoding block: taking a finished trade apart.

Realised PnL is one number and the least informative thing about a trade. These tests
are about the pieces: where the money came from, whether the outcome was
distinguishable from the symbol moving, whether the stop was a control or an
appointment, and whether anything learned is stated so it can be tested and retired.
"""

import importlib

import pytest

from parts.closed_trade_decoding.entry_quality_scorer import (
    EntryQualityScorer, NO_SIGNAL_TIME, NO_WINDOW, SCORED as ENTRY_SCORED,
)
from parts.closed_trade_decoding.excursion_profiler import (
    ExcursionProfiler, PROFILED as EXCURSION_PROFILED, TOO_FEW_TRADES as EXCURSION_THIN,
)
from parts.closed_trade_decoding.exit_counterfactual_replayer import (
    ExitCounterfactualReplayer, FIXED_TARGET, NEVER_TRIGGERED, REPLAYABLE_RULES,
    REPLAYED, TIME_EXIT, TRAILING_STOP, UNREACHABLE,
)
from parts.closed_trade_decoding.exit_quality_scorer import (
    ExitQualityScorer, NEVER_WENT_FAVOURABLE, NO_EXCURSION, SCORED as EXIT_SCORED,
)
from parts.closed_trade_decoding.exploration_pair_decoder import (
    CLOSED_DIFFERENTLY, DIFFERENT_INSTRUMENTS, ExplorationPairDecoder,
    INCOMPLETE as PAIR_INCOMPLETE,
    LONG_SIDE_WON, NOT_SYMMETRIC, NO_DIRECTIONAL_EDGE,
)
from parts.closed_trade_decoding.holding_horizon_profiler import (
    FLAT, HoldingHorizonProfiler, PROFILED as HORIZON_PROFILED,
    TOO_FEW_TRADES as HORIZON_THIN,
)
from parts.closed_trade_decoding.lesson_extractor import (
    CONTRADICTED, EXPRESSIBLE_CHANGES, EXTRACTED, LessonExtractor, NOT_SIGNIFICANT,
    TIGHTEN_THE_STOP, TOO_FEW_TRADES as LESSON_THIN, UNCONDITIONAL, WIDEN_THE_STOP,
)
from parts.closed_trade_decoding.loss_cause_classifier import (
    CLASSIFIED, LossCauseClassifier, NOT_A_LOSS,
)
from parts.closed_trade_decoding.luck_skill_separator import (
    INDISTINGUISHABLE, LuckSkillSeparator, NO_VOLATILITY, SIGNIFICANT,
)
from parts.closed_trade_decoding.near_miss_recorder import (
    NearMissRecorder, RECORDED as NEAR_MISS_RECORDED, RESOLVED,
)
from parts.closed_trade_decoding.pnl_attributor import (
    ATTRIBUTED, MIXED_CURRENCIES, NOTHING_TO_ATTRIBUTE, PnlAttributor,
)
from parts.closed_trade_decoding.regime_transition_tagger import (
    FLICKER_ONLY, NO_CHANGE, NO_REGIME_DATA, RegimeTransitionTagger, TAGGED,
)
from parts.closed_trade_decoding.sequence_pattern_miner import (
    OUTCOME_CONDITIONING, PATTERN_KINDS, SIZE_DRIFT, SURVIVES_SHUFFLING,
    SequencePatternMiner, STREAKS, TOO_FEW_TRADES as SEQUENCE_THIN,
)
from parts.closed_trade_decoding.shortfall_decomposer import (
    DECOMPOSED, NO_DECISION_PRICE, ShortfallDecomposer,
)
from parts.closed_trade_decoding.stop_placement_auditor import (
    INSIDE_THE_NOISE, NEVER_APPROACHED, NO_STOP, StopPlacementAuditor, TOO_WIDE,
    WELL_PLACED_AND_HIT,
)
from parts.closed_trade_decoding.trade_cluster_detector import (
    CLUSTERED, INDEPENDENT, TradeClusterDetector,
)
from parts.closed_trade_decoding.trade_episode_encoder import (
    ENCODED, INCOMPLETE as EPISODE_INCOMPLETE, REQUIRED_PIECES, SUPERSEDED,
    TradeEpisodeEncoder,
)
from parts.closed_trade_decoding.trade_narrative_writer import (
    ALREADY_WRITTEN, AWAITING_PHRASING, TradeNarrativeWriter, WRITTEN,
)
from parts.closed_trade_decoding.trade_replay_verifier import (
    AGREES, CRITICAL, EXISTENCE, MINOR, MISMATCHED, QUANTITY, SERIOUS,
    TradeReplayVerifier,
)
from parts.closed_trade_decoding.winner_pattern_miner import (
    DOES_NOT_SEPARATE, FOUND, TOO_FEW_TRADES as WINNER_THIN, WinnerPatternMiner,
)
from runtime.trade_decoding_types import (
    COSTS_ATE_IT, FROM_DIRECTION, FROM_FEES, FROM_UNEXPLAINED, IT_WAS_JUST_VARIANCE,
    PNL_COMPONENTS, THE_STOP_WAS_INSIDE_THE_NOISE,
)
from runtime.trading_types import ClosedTrade, Fill
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "trade-episode-encoder": "parts.closed_trade_decoding.trade_episode_encoder",
    "lesson-extractor": "parts.closed_trade_decoding.lesson_extractor",
    "loss-cause-classifier": "parts.closed_trade_decoding.loss_cause_classifier",
    "winner-pattern-miner": "parts.closed_trade_decoding.winner_pattern_miner",
    "exit-quality-scorer": "parts.closed_trade_decoding.exit_quality_scorer",
    "pnl-attributor": "parts.closed_trade_decoding.pnl_attributor",
    "entry-quality-scorer": "parts.closed_trade_decoding.entry_quality_scorer",
    "exit-counterfactual-replayer": (
        "parts.closed_trade_decoding.exit_counterfactual_replayer"
    ),
    "holding-horizon-profiler": "parts.closed_trade_decoding.holding_horizon_profiler",
    "trade-cluster-detector": "parts.closed_trade_decoding.trade_cluster_detector",
    "luck-skill-separator": "parts.closed_trade_decoding.luck_skill_separator",
    "near-miss-recorder": "parts.closed_trade_decoding.near_miss_recorder",
    "regime-transition-tagger": "parts.closed_trade_decoding.regime_transition_tagger",
    "shortfall-decomposer": "parts.closed_trade_decoding.shortfall_decomposer",
    "trade-narrative-writer": "parts.closed_trade_decoding.trade_narrative_writer",
    "sequence-pattern-miner": "parts.closed_trade_decoding.sequence_pattern_miner",
    "stop-placement-auditor": "parts.closed_trade_decoding.stop_placement_auditor",
    "excursion-profiler": "parts.closed_trade_decoding.excursion_profiler",
    "exploration-pair-decoder": "parts.closed_trade_decoding.exploration_pair_decoder",
    "trade-replay-verifier": "parts.closed_trade_decoding.trade_replay_verifier",
}

SECOND_NS = 1_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


def a_closed_trade(direction="long", entry=100.0, exit_price=110.0, quantity=1.0,
                   realised=10.0, fees=0.0, opened_at_ns=0, closed_at_ns=3600 * SECOND_NS,
                   symbol="BTCUSDT", best=None, worst=None):
    return ClosedTrade(
        venue_id="binance-usdm", symbol=symbol, direction=direction, quantity=quantity,
        entry_price=entry, exit_price=exit_price, realised_pnl=realised, fees_paid=fees,
        opened_at_ns=opened_at_ns, closed_at_ns=closed_at_ns,
        best_unrealised=best, worst_unrealised=worst,
    )


def a_fill(price=100.0, quantity=1.0, fee=0.1, fill_id="f-1"):
    return Fill(
        fill_id=fill_id, venue_id="binance-usdm", symbol="BTCUSDT", side="buy",
        price=price, quantity=quantity, fee=fee, filled_at_ns=0,
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- pnl-attributor ---------------------------------------------------------

def an_attributor(tolerance=1e-6):
    return PnlAttributor(reconciliation_tolerance=tolerance, now_ns=Clock())


def test_the_components_add_back_to_the_realised_total():
    subject = an_attributor()
    trade = a_closed_trade(entry=100.0, exit_price=110.0, quantity=1.0, realised=9.8)
    subject.observe_fill("t-1", a_fill(fee=0.2))
    outcome = subject.attribute("t-1", trade)
    assert outcome.state == ATTRIBUTED
    assert sum(outcome.attribution.components.values()) == pytest.approx(9.8)
    assert outcome.attribution.reconciles


def test_the_residual_is_kept_rather_than_smeared():
    """A large residual says the model of where PnL comes from is missing something."""
    subject = an_attributor()
    trade = a_closed_trade(entry=100.0, exit_price=110.0, realised=4.0)
    subject.observe_fill("t-1", a_fill(fee=0.0))
    outcome = subject.attribute("t-1", trade)
    assert outcome.attribution.components[FROM_UNEXPLAINED] == pytest.approx(-6.0)
    assert outcome.attribution.components[FROM_DIRECTION] == pytest.approx(10.0)


def test_funding_is_signed_and_can_be_received():
    subject = an_attributor()
    subject.observe_fill("t-1", a_fill(fee=0.0))
    subject.observe_funding("t-1", 1.5)
    outcome = subject.attribute("t-1", a_closed_trade(realised=11.5))
    assert outcome.attribution.components["funding"] == 1.5
    assert subject.standing.funding_received_trades == 1


def test_slippage_is_measured_from_the_decision_price():
    subject = an_attributor()
    subject.observe_decision_price("t-1", 99.0)
    subject.observe_fill("t-1", a_fill(price=100.0, fee=0.0))
    outcome = subject.attribute("t-1", a_closed_trade(realised=9.0))
    assert outcome.attribution.components["slippage"] == pytest.approx(-1.0)


def test_fills_in_an_unconvertible_currency_are_refused():
    subject = an_attributor()
    subject.observe_fill("t-1", a_fill(), quote_currency="BUSD")
    assert subject.attribute("t-1", a_closed_trade()).state == MIXED_CURRENCIES


def test_a_trade_with_no_fills_is_wholly_unexplained():
    subject = an_attributor()
    outcome = subject.attribute("t-1", a_closed_trade(realised=5.0))
    assert outcome.state == NOTHING_TO_ATTRIBUTE
    assert outcome.attribution.components[FROM_UNEXPLAINED] == 5.0


def test_the_attributor_never_smears_the_residual():
    assert importlib.import_module(
        BLOCK_PARTS["pnl-attributor"]
    ).describe_attribution(an_attributor())[
        "smears_the_residual_across_the_components"
    ] is False


# ---- shortfall-decomposer ---------------------------------------------------

def a_decomposer():
    return ShortfallDecomposer(now_ns=Clock())


def test_the_three_costs_sum_to_the_total_shortfall():
    subject = a_decomposer()
    subject.observe_decision("o-1", price=100.0, decided_at_ns=0, side="buy")
    subject.observe_arrival("o-1", mid_price=100.5, half_spread=0.1, arrived_at_ns=SECOND_NS)
    subject.observe_fill("o-1", price=100.8, quantity=2.0)
    outcome = subject.decompose("o-1", "t-1")
    assert outcome.state == DECOMPOSED
    breakdown = outcome.breakdown
    assert (
        breakdown.spread_cost + breakdown.delay_cost + breakdown.impact_cost
        == pytest.approx(breakdown.total_shortfall)
    )


def test_delay_cost_is_separated_from_spread_and_impact():
    subject = a_decomposer()
    subject.observe_decision("o-1", price=100.0, decided_at_ns=0, side="buy")
    subject.observe_arrival("o-1", mid_price=101.0, half_spread=0.01, arrived_at_ns=SECOND_NS)
    subject.observe_fill("o-1", price=101.02, quantity=1.0)
    outcome = subject.decompose("o-1", "t-1")
    assert outcome.breakdown.largest_cause == "delay"
    assert "deciding faster" in outcome.breakdown.reason


def test_a_reconstructed_decision_price_is_not_accepted():
    subject = a_decomposer()
    subject.observe_arrival("o-1", 100.0, 0.1, SECOND_NS)
    subject.observe_fill("o-1", 100.2, 1.0)
    outcome = subject.decompose("o-1", "t-1")
    assert outcome.state == NO_DECISION_PRICE
    assert "makes delay cost vanish" in outcome.reason


def test_the_decomposer_reports_no_blended_number():
    assert importlib.import_module(
        BLOCK_PARTS["shortfall-decomposer"]
    ).describe_shortfall(a_decomposer())["reports_one_blended_slippage_number"] is False


# ---- entry-quality-scorer ---------------------------------------------------

def an_entry_scorer(window=60.0, minimum=2, chasing=2.0):
    return EntryQualityScorer(
        window_seconds=window, minimum_prices=minimum,
        chasing_in_typical_movements=chasing, now_ns=Clock(),
    )


def test_an_entry_is_placed_among_the_prices_it_could_have_reached():
    subject = an_entry_scorer()
    subject.observe_signal_time("t-1", 0)
    for index, price in enumerate((100.0, 101.0, 102.0, 103.0)):
        subject.observe_price("binance-usdm", "BTCUSDT", price, index * SECOND_NS)
    outcome = subject.score("t-1", a_closed_trade(entry=100.5))
    assert outcome.state == ENTRY_SCORED
    assert outcome.quality.percentile == pytest.approx(0.75)


def test_chasing_is_measured_in_the_symbols_own_movement():
    subject = an_entry_scorer(chasing=1.0)
    subject.observe_signal_time("t-1", 0)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 1.0)
    for index, price in enumerate((100.0, 101.0, 102.0, 103.0)):
        subject.observe_price("binance-usdm", "BTCUSDT", price, index * SECOND_NS)
    outcome = subject.score("t-1", a_closed_trade(entry=103.0))
    assert outcome.quality.was_chasing
    assert "edge already spent" in outcome.quality.reason


def test_an_unmeasurable_entry_is_not_scored_neutrally():
    subject = an_entry_scorer()
    subject.observe_signal_time("t-1", 0)
    outcome = subject.score("t-1", a_closed_trade())
    assert outcome.state == NO_WINDOW
    assert outcome.quality is None


def test_without_a_signal_time_the_window_is_unknown():
    assert an_entry_scorer().score("t-1", a_closed_trade()).state == NO_SIGNAL_TIME


def test_the_scorer_does_not_benchmark_against_the_sessions_best():
    assert importlib.import_module(
        BLOCK_PARTS["entry-quality-scorer"]
    ).describe_entry_scoring(an_entry_scorer())[
        "benchmarks_against_the_sessions_best_price"
    ] is False


# ---- exit-quality-scorer ----------------------------------------------------

def an_exit_scorer(gave_back=0.5):
    return ExitQualityScorer(gave_back_threshold=gave_back, now_ns=Clock())


def test_an_exit_reports_both_what_it_captured_and_what_it_sat_through():
    subject = an_exit_scorer()
    subject.observe_excursion("t-1", entry_price=100.0, best_price=110.0,
                              worst_price=95.0, direction="long")
    outcome = subject.score("t-1", exit_price=106.0)
    assert outcome.state == EXIT_SCORED
    assert outcome.quality.captured_fraction == pytest.approx(0.6)
    assert outcome.quality.suffered_fraction == pytest.approx(0.5)


def test_an_exit_that_avoided_a_larger_loss_is_recorded_as_such():
    """A capture-only view makes every stop that worked invisible."""
    subject = an_exit_scorer()
    subject.observe_excursion("t-1", 100.0, best_price=100.0, worst_price=95.0,
                              direction="long", price_after_exit=80.0)
    outcome = subject.score("t-1", exit_price=95.0)
    assert outcome.state == NEVER_WENT_FAVOURABLE
    assert outcome.avoided_a_larger_loss


def test_capturing_everything_is_noted_rather_than_rewarded():
    subject = an_exit_scorer()
    subject.observe_excursion("t-1", 100.0, 110.0, 100.0, "long")
    outcome = subject.score("t-1", exit_price=110.0)
    assert "unrepeatable" in outcome.quality.reason


def test_without_an_excursion_nothing_is_measured():
    assert an_exit_scorer().score("t-1", 100.0).state == NO_EXCURSION


# ---- stop-placement-auditor -------------------------------------------------

def an_auditor(inside=1.0, wide=5.0, approach=0.8):
    return StopPlacementAuditor(
        inside_the_noise_below=inside, too_wide_above=wide,
        approach_fraction=approach, now_ns=Clock(),
    )


def test_a_stop_inside_the_noise_is_named_as_a_scheduled_exit():
    subject = an_auditor(inside=1.0)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 5.0)
    subject.observe_stop("t-1", 98.0)
    outcome = subject.audit("t-1", a_closed_trade(entry=100.0), worst_price=97.0)
    assert outcome.state == INSIDE_THE_NOISE
    assert "taken out by noise" in outcome.audit.reason


def test_a_stop_that_did_its_job_is_a_possible_verdict():
    """Otherwise every stop that triggered is recorded as a mistake."""
    subject = an_auditor(inside=1.0, wide=5.0)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 1.0)
    subject.observe_stop("t-1", 98.0)
    outcome = subject.audit("t-1", a_closed_trade(entry=100.0), worst_price=97.5)
    assert outcome.state == WELL_PLACED_AND_HIT


def test_a_stop_never_approached_says_nothing_about_the_placement():
    subject = an_auditor(inside=1.0, wide=10.0, approach=0.8)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 1.0)
    subject.observe_stop("t-1", 95.0)
    outcome = subject.audit("t-1", a_closed_trade(entry=100.0), worst_price=99.9)
    assert outcome.state == NEVER_APPROACHED
    assert "acquires a track record" in outcome.audit.reason


def test_a_stop_too_wide_points_at_the_opposite_fix():
    subject = an_auditor(inside=1.0, wide=3.0)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 1.0)
    subject.observe_stop("t-1", 90.0)
    outcome = subject.audit("t-1", a_closed_trade(entry=100.0), worst_price=99.0)
    assert outcome.state == TOO_WIDE
    assert "how a system oscillates" in outcome.audit.reason


def test_a_recovery_after_a_stop_is_labelled_as_hindsight():
    subject = an_auditor(inside=2.0)
    subject.observe_typical_movement("binance-usdm", "BTCUSDT", 5.0)
    subject.observe_stop("t-1", 98.0)
    subject.observe_price_after_exit("t-1", 120.0)
    outcome = subject.audit("t-1", a_closed_trade(entry=100.0), worst_price=97.0)
    assert outcome.audit.would_have_recovered
    assert "not that holding would have been right" in outcome.audit.reason


def test_a_trade_without_a_stop_is_recorded_as_such():
    subject = an_auditor()
    assert subject.audit("t-1", a_closed_trade(), 90.0).state == NO_STOP


def test_stop_audit_carries_venue_symbol_and_adverse_excursion():
    """The audit is constructed with identity and a magnitude, not just a verdict."""
    from parts.closed_trade_decoding.stop_placement_auditor import StopPlacementAuditor
    from runtime.trade_decoding_types import INSIDE_THE_NOISE

    class ClosedTradeStub:
        venue_id = "binance-usdm"
        symbol = "BTCUSDT"
        direction = "long"
        entry_price = 100.0

    auditor = StopPlacementAuditor(
        inside_the_noise_below=0.5, too_wide_above=5.0, approach_fraction=0.5,
    )
    auditor.observe_typical_movement("binance-usdm", "BTCUSDT", 0.01)
    auditor.observe_stop("t1", 99.0)

    outcome = auditor.audit("t1", ClosedTradeStub(), worst_price=97.0)

    assert outcome.audit.venue_id == "binance-usdm"
    assert outcome.audit.symbol == "BTCUSDT"
    # Price went 3% against entry (100 -> 97) before the audit was taken.
    assert outcome.audit.adverse_excursion_fraction == pytest.approx(0.03)


# ---- luck-skill-separator ---------------------------------------------------

def a_separator(threshold=1.0, minimum=5):
    return LuckSkillSeparator(
        significance_threshold=threshold, minimum_comparable_outcomes=minimum,
        now_ns=Clock(),
    )


def test_a_small_gain_in_a_volatile_symbol_is_indistinguishable_from_noise():
    subject = a_separator(threshold=1.0)
    subject.observe_daily_volatility("binance-usdm", "BTCUSDT", 0.05)
    trade = a_closed_trade(entry=100.0, quantity=1.0, realised=2.0,
                           closed_at_ns=86_400 * SECOND_NS)
    outcome = subject.assess("t-1", trade)
    assert outcome.state == INDISTINGUISHABLE


def test_volatility_is_scaled_to_the_holding_period():
    subject = a_separator(threshold=1.0)
    subject.observe_daily_volatility("binance-usdm", "BTCUSDT", 0.05)
    short_trade = a_closed_trade(entry=100.0, realised=2.0, closed_at_ns=60 * SECOND_NS)
    outcome = subject.assess("t-1", short_trade)
    assert outcome.state == SIGNIFICANT
    assert abs(outcome.significance.standardised) > 1.0


def test_a_significant_loss_is_exactly_when_learning_is_worthwhile():
    subject = a_separator(threshold=1.0)
    subject.observe_daily_volatility("binance-usdm", "BTCUSDT", 0.01)
    trade = a_closed_trade(entry=100.0, realised=-10.0, closed_at_ns=60 * SECOND_NS)
    outcome = subject.assess("t-1", trade)
    assert outcome.state == SIGNIFICANT
    assert subject.standing.significant_losses == 1


def test_an_unmeasured_symbol_cannot_be_separated():
    outcome = a_separator().assess("t-1", a_closed_trade())
    assert outcome.state == NO_VOLATILITY
    assert outcome.significance.is_measurable is False


def test_sample_size_travels_with_the_verdict():
    subject = a_separator(minimum=10)
    subject.observe_daily_volatility("binance-usdm", "BTCUSDT", 0.01)
    outcome = subject.assess("t-1", a_closed_trade(closed_at_ns=60 * SECOND_NS))
    assert outcome.significance.sample_size == 1
    assert "comparable outcome(s)" in outcome.significance.reason


# ---- regime-transition-tagger -----------------------------------------------

def a_tagger(persistence=60.0, minimum=2):
    return RegimeTransitionTagger(
        minimum_persistence_seconds=persistence, minimum_observations=minimum,
        now_ns=Clock(),
    )


def _feed_regimes(tagger, readings):
    for at_ns, regime in readings:
        tagger.observe_regime("binance-usdm", "BTCUSDT", regime, at_ns)


def test_a_flicker_is_not_a_transition():
    subject = a_tagger(persistence=600.0)
    _feed_regimes(subject, [
        (0, "trending"), (1000 * SECOND_NS, "chop"), (1010 * SECOND_NS, "trending"),
        (3590 * SECOND_NS, "trending"),
    ])
    outcome = subject.tag("t-1", a_closed_trade())
    assert outcome.state == NO_CHANGE


def test_a_persisting_change_is_tagged_with_its_share():
    subject = a_tagger(persistence=60.0)
    _feed_regimes(subject, [
        (0, "trending"), (1800 * SECOND_NS, "trending"), (1810 * SECOND_NS, "chop"),
        (3500 * SECOND_NS, "chop"),
    ])
    outcome = subject.tag("t-1", a_closed_trade())
    assert outcome.state == TAGGED
    assert outcome.flag.fraction_of_the_trade_in_the_new_regime == pytest.approx(0.497, abs=0.01)
    assert "not an excuse" in outcome.flag.reason


def test_without_regime_readings_nothing_is_concluded():
    outcome = a_tagger(minimum=3).tag("t-1", a_closed_trade())
    assert outcome.state == NO_REGIME_DATA
    assert "not evidence that the regime held" in outcome.reason


# ---- trade-cluster-detector -------------------------------------------------

def a_cluster_detector(window=60.0, correlation=0.7):
    return TradeClusterDetector(
        window_seconds=window, minimum_correlation=correlation, now_ns=Clock(),
    )


def test_ten_correlated_longs_are_fewer_than_ten_bets():
    subject = a_cluster_detector()
    for symbol in ("AUSDT", "BUSDT", "CUSDT"):
        subject.observe_correlation_group(symbol, "alt")
    trades = [
        (f"t-{index}", a_closed_trade(symbol=symbol, opened_at_ns=index * SECOND_NS))
        for index, symbol in enumerate(("AUSDT", "BUSDT", "CUSDT"))
    ]
    outcome = subject.detect(trades)
    assert outcome.state == CLUSTERED
    cluster = outcome.clusters[0]
    assert cluster.was_one_bet
    assert cluster.effective_bets < 3


def test_a_hedge_is_not_a_doubled_bet():
    subject = a_cluster_detector()
    subject.observe_correlation("AUSDT", "BUSDT", 0.95)
    trades = [
        ("t-1", a_closed_trade(symbol="AUSDT", direction="long", opened_at_ns=0)),
        ("t-2", a_closed_trade(symbol="BUSDT", direction="short", opened_at_ns=SECOND_NS)),
    ]
    outcome = subject.detect(trades)
    assert outcome.state == INDEPENDENT
    assert subject.standing.hedges_not_clustered == 1


def test_uncorrelated_simultaneous_trades_are_separate_bets():
    subject = a_cluster_detector()
    trades = [
        ("t-1", a_closed_trade(symbol="AUSDT", opened_at_ns=0)),
        ("t-2", a_closed_trade(symbol="ZUSDT", opened_at_ns=SECOND_NS)),
    ]
    assert subject.detect(trades).state == INDEPENDENT


def test_effective_bets_falls_with_correlation():
    subject = a_cluster_detector()
    subject.observe_correlation("AUSDT", "BUSDT", 1.0)
    assert subject.effective_bets(["AUSDT", "BUSDT"]) == pytest.approx(1.0)
    subject.observe_correlation("AUSDT", "CUSDT", 0.0)
    assert subject.effective_bets(["AUSDT", "CUSDT"]) == pytest.approx(2.0)


# ---- near-miss-recorder -----------------------------------------------------

def a_near_miss_recorder(horizon=60.0, cost=0.001, clock=None):
    return NearMissRecorder(
        horizon_seconds=horizon, round_trip_cost_fraction=cost,
        now_ns=clock or Clock(),
    )


def test_a_refusal_is_recorded_with_the_reason_given_at_the_time():
    subject = a_near_miss_recorder()
    outcome = subject.record(
        "n-1", "binance-usdm", "BTCUSDT", "long", 100.0, "spread-too-wide", 0,
    )
    assert outcome.state == NEAR_MISS_RECORDED
    assert "reconstructed later it becomes whatever seems plausible" in outcome.reason


def test_costs_are_subtracted_from_the_counterfactual():
    """A gross comparison declares hundreds of correct refusals to be mistakes."""
    clock = Clock()
    subject = a_near_miss_recorder(horizon=60.0, cost=0.01, clock=clock)
    subject.record("n-1", "binance-usdm", "BTCUSDT", "long", 100.0, "cost", clock.now_ns)
    clock.now_ns += 61 * SECOND_NS
    subject.observe_price("binance-usdm", "BTCUSDT", 100.5, clock.now_ns)
    outcome = subject.resolve("n-1")
    assert outcome.state == RESOLVED
    assert outcome.episode.would_have_realised < 0
    assert outcome.episode.was_a_mistake_to_skip is False


def test_a_genuinely_missed_trade_is_recorded_as_a_mistake():
    clock = Clock()
    subject = a_near_miss_recorder(horizon=60.0, cost=0.001, clock=clock)
    subject.record("n-1", "binance-usdm", "BTCUSDT", "long", 100.0, "filter", clock.now_ns)
    clock.now_ns += 61 * SECOND_NS
    subject.observe_price("binance-usdm", "BTCUSDT", 110.0, clock.now_ns)
    outcome = subject.resolve("n-1")
    assert outcome.episode.was_a_mistake_to_skip


def test_which_reasons_are_systematically_wrong_is_reportable():
    clock = Clock()
    subject = a_near_miss_recorder(horizon=60.0, clock=clock)
    for index in range(3):
        subject.record(
            f"n-{index}", "binance-usdm", "BTCUSDT", "long", 100.0, "filter",
            clock.now_ns,
        )
    clock.now_ns += 61 * SECOND_NS
    subject.observe_price("binance-usdm", "BTCUSDT", 110.0, clock.now_ns)
    for index in range(3):
        subject.resolve(f"n-{index}")
    assert subject.mistakes_by_reason()["filter"]["mistakes"] == 3


def test_the_recorder_re_enters_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["near-miss-recorder"]
    ).describe_near_misses(a_near_miss_recorder())["re_enters_anything"] is False


# ---- loss-cause-classifier --------------------------------------------------

def a_classifier(gave_back=0.5, regime_share=0.3):
    return LossCauseClassifier(
        gave_back_threshold=gave_back, regime_share_threshold=regime_share,
        prior_correctness=0.5, prior_weight=4.0, half_life_observations=200,
        minimum_observations=5, now_ns=Clock(),
    )


class Significance:
    def __init__(self, is_significant=True, standardised=2.0, measurable=True):
        self.is_significant = is_significant
        self.standardised = standardised
        self.is_measurable = measurable


class StopAuditStub:
    def __init__(self, verdict=INSIDE_THE_NOISE, recovered=True, distance=0.4):
        self.verdict = verdict
        self.would_have_recovered = recovered
        self.distance_in_typical_movements = distance
        self.is_measurable = True


class AttributionStub:
    def __init__(self, direction=5.0, cost_share=1.5, residual=0.0):
        self.components = {"direction": direction}
        self.cost_share = cost_share
        self.residual = residual


def test_variance_is_a_reachable_verdict():
    """A classifier that always finds a fault tunes a working strategy to death."""
    subject = a_classifier()
    outcome = subject.classify(
        "t-1", a_closed_trade(realised=-1.0),
        significance=Significance(is_significant=False, standardised=0.1),
    )
    assert outcome.state == CLASSIFIED
    assert outcome.cause.cause == IT_WAS_JUST_VARIANCE
    assert outcome.cause.is_worth_changing_something_for is False


def test_a_stop_inside_the_noise_outranks_the_alternatives():
    subject = a_classifier()
    outcome = subject.classify(
        "t-1", a_closed_trade(realised=-5.0),
        stop_audit=StopAuditStub(), significance=Significance(),
    )
    assert outcome.cause.cause == THE_STOP_WAS_INSIDE_THE_NOISE
    assert outcome.cause.was_avoidable


def test_a_real_move_eaten_by_costs_is_named_as_such():
    subject = a_classifier()
    outcome = subject.classify(
        "t-1", a_closed_trade(realised=-1.0),
        attribution=AttributionStub(direction=5.0, cost_share=1.5),
        significance=Significance(),
    )
    assert outcome.cause.cause == COSTS_ATE_IT


def test_the_runners_up_are_kept():
    subject = a_classifier()
    outcome = subject.classify(
        "t-1", a_closed_trade(realised=-5.0),
        stop_audit=StopAuditStub(), attribution=AttributionStub(),
        significance=Significance(),
    )
    assert outcome.cause.runners_up


def test_a_winning_trade_has_no_loss_to_explain():
    assert a_classifier().classify("t-1", a_closed_trade(realised=5.0)).state == NOT_A_LOSS


def test_whether_acting_on_a_cause_helped_is_learned():
    subject = a_classifier()
    for _ in range(10):
        subject.observe_cause_outcome(THE_STOP_WAS_INSIDE_THE_NOISE, True)
    confidence, is_fitted = subject.confidence_in(THE_STOP_WAS_INSIDE_THE_NOISE)
    assert is_fitted and confidence > 0.5


# ---- exit-counterfactual-replayer -------------------------------------------

def a_replayer(cost=0.0):
    return ExitCounterfactualReplayer(round_trip_cost_fraction=cost, now_ns=Clock())


def _tape(replayer, trade_id, prices):
    for index, price in enumerate(prices):
        replayer.observe_tape(trade_id, price, index * SECOND_NS)


def test_every_counterfactual_is_labelled_hindsight():
    subject = a_replayer()
    _tape(subject, "t-1", [100.0, 105.0, 110.0])
    outcome = subject.replay(
        "t-1", a_closed_trade(entry=100.0, exit_price=105.0, realised=5.0),
        "target-110", {"kind": FIXED_TARGET, "price": 110.0},
    )
    assert outcome.state == REPLAYED
    assert outcome.counterfactual.is_hindsight


def test_an_unreachable_exit_price_is_refused():
    subject = a_replayer()
    _tape(subject, "t-1", [100.0, 101.0])
    outcome = subject.replay(
        "t-1", a_closed_trade(entry=100.0),
        "target-200", {"kind": FIXED_TARGET, "price": 200.0},
    )
    assert outcome.state == NEVER_TRIGGERED


def test_costs_are_applied_to_the_counterfactual():
    subject = a_replayer(cost=0.01)
    _tape(subject, "t-1", [100.0, 110.0])
    outcome = subject.replay(
        "t-1", a_closed_trade(entry=100.0, quantity=1.0, realised=5.0),
        "target", {"kind": FIXED_TARGET, "price": 110.0},
    )
    assert outcome.counterfactual.realised_pnl == pytest.approx(10.0 - 1.0)


def test_a_trailing_stop_can_be_replayed():
    subject = a_replayer()
    _tape(subject, "t-1", [100.0, 110.0, 104.0])
    outcome = subject.replay(
        "t-1", a_closed_trade(entry=100.0),
        "trail-5", {"kind": TRAILING_STOP, "distance": 5.0},
    )
    assert outcome.state == REPLAYED
    assert outcome.counterfactual.exit_price == pytest.approx(105.0)


def test_a_rule_shape_outside_the_declared_set_is_refused():
    subject = a_replayer()
    _tape(subject, "t-1", [100.0])
    with pytest.raises(ValueError):
        subject.replay("t-1", a_closed_trade(), "invented", {"kind": "optimised-sweep"})
    assert set(REPLAYABLE_RULES) >= {FIXED_TARGET, TRAILING_STOP, TIME_EXIT}


# ---- holding-horizon-profiler -----------------------------------------------

def a_horizon_profiler(minimum=3, margin=0.5, decay=0.5):
    return HoldingHorizonProfiler(
        horizons_seconds=(60.0, 300.0, 900.0), minimum_trades=minimum,
        winning_margin=margin, decay_fraction=decay, now_ns=Clock(),
    )


def test_a_flat_curve_is_reported_as_flat():
    """Picking the argmax of a noisy curve is how a system ends up holding 47 minutes."""
    subject = a_horizon_profiler(margin=1.0)
    for index in range(5):
        subject.observe_counterfactual("breakout", 60.0, 1.0)
        subject.observe_counterfactual("breakout", 300.0, 1.1)
        subject.observe_counterfactual("breakout", 900.0, 1.05)
    outcome = subject.profile("breakout")
    assert outcome.state == FLAT
    assert outcome.profile.best_horizon_seconds is None


def test_a_decay_point_is_what_is_actually_looked_for():
    subject = a_horizon_profiler(margin=0.1, decay=0.5)
    for index in range(5):
        subject.observe_counterfactual("breakout", 60.0, 1.0)
        subject.observe_counterfactual("breakout", 300.0, 3.0)
        subject.observe_counterfactual("breakout", 900.0, 0.5)
    outcome = subject.profile("breakout")
    assert outcome.state == HORIZON_PROFILED
    assert outcome.profile.decays_after_seconds == 900.0


def test_a_thin_sample_is_reported_unfitted():
    subject = a_horizon_profiler(minimum=10)
    subject.observe_counterfactual("breakout", 60.0, 1.0)
    subject.observe_counterfactual("breakout", 300.0, 1.0)
    subject.observe_counterfactual("breakout", 900.0, 1.0)
    outcome = subject.profile("breakout")
    assert outcome.state == HORIZON_THIN
    assert outcome.profile.is_fitted is False


def test_horizons_cannot_be_added_after_seeing_results():
    subject = a_horizon_profiler()
    with pytest.raises(ValueError):
        subject.observe_counterfactual("breakout", 137.0, 1.0)


def test_the_profiler_changes_no_exit():
    assert importlib.import_module(
        BLOCK_PARTS["holding-horizon-profiler"]
    ).describe_horizon_profiling(a_horizon_profiler())["changes_an_exit"] is False


# ---- excursion-profiler -----------------------------------------------------

def an_excursion_profiler(window=100, quantile=0.9, minimum=5):
    return ExcursionProfiler(
        window=window, quantile=quantile, minimum_trades=minimum, now_ns=Clock(),
    )


def test_the_profile_reports_quantiles_not_just_means():
    subject = an_excursion_profiler(minimum=3)
    for index in range(10):
        subject.observe_excursion(
            "binance-usdm", "BTCUSDT", "trending", favourable=1.0 + index,
            adverse=0.5 + index * 0.1, was_a_winner=True,
        )
    outcome = subject.profile("binance-usdm", "BTCUSDT", "trending")
    assert outcome.state == EXCURSION_PROFILED
    assert outcome.profile.adverse_quantile > outcome.profile.median_adverse


def test_winners_are_profiled_separately():
    """The adverse excursion of eventual winners is what a stop must accommodate."""
    subject = an_excursion_profiler(minimum=3)
    for index in range(5):
        subject.observe_excursion("binance-usdm", "BTCUSDT", "trending", 2.0, 0.5, True)
        subject.observe_excursion("binance-usdm", "BTCUSDT", "trending", 0.1, 9.0, False)
    winners = subject.profile("binance-usdm", "BTCUSDT", "trending", winners_only=True)
    pooled = subject.profile("binance-usdm", "BTCUSDT", "trending")
    assert winners.profile.adverse_quantile < pooled.profile.adverse_quantile


def test_a_thin_sample_is_not_a_distribution():
    subject = an_excursion_profiler(minimum=20)
    subject.observe_excursion("binance-usdm", "BTCUSDT", None, 1.0, 1.0, True)
    outcome = subject.profile("binance-usdm", "BTCUSDT")
    assert outcome.state == EXCURSION_THIN
    assert outcome.profile.is_fitted is False


def test_regimes_are_not_pooled_silently():
    assert importlib.import_module(
        BLOCK_PARTS["excursion-profiler"]
    ).describe_excursion_profiling(an_excursion_profiler())["pools_regimes_silently"] is False


# ---- winner-pattern-miner ---------------------------------------------------

def a_winner_miner(conditions=("regime", "session"), minimum=2, lift=1.5):
    return WinnerPatternMiner(
        declared_conditions=conditions, minimum_each_side=minimum,
        minimum_lift=lift, now_ns=Clock(),
    )


def test_a_condition_common_to_losers_too_does_not_separate():
    subject = a_winner_miner()
    for index in range(5):
        subject.observe_trade(f"w-{index}", {"regime": "trending"}, True)
        subject.observe_trade(f"l-{index}", {"regime": "trending"}, False)
    outcome = subject.mine("regime", "trending")
    assert outcome.state == DOES_NOT_SEPARATE
    assert "describes the population" in outcome.pattern.reason


def test_a_separating_condition_is_found():
    subject = a_winner_miner(lift=1.5)
    for index in range(5):
        subject.observe_trade(f"w-{index}", {"regime": "trending"}, True)
        subject.observe_trade(f"l-{index}", {"regime": "chop"}, False)
    outcome = subject.mine("regime", "trending")
    assert outcome.state == FOUND
    assert outcome.pattern.separates


def test_clustered_trades_count_once():
    subject = a_winner_miner(minimum=2, lift=1.5)
    for index in range(6):
        subject.observe_trade(f"w-{index}", {"regime": "trending"}, True)
        subject.observe_cluster(f"w-{index}", "cluster-1")
    for index in range(4):
        subject.observe_trade(f"l-{index}", {"regime": "chop"}, False)
    outcome = subject.mine("regime", "trending")
    assert outcome.state == WINNER_THIN
    assert subject.standing.clustered_trades_counted_once > 0


def test_undeclared_conditions_are_refused():
    subject = a_winner_miner(conditions=("regime",))
    with pytest.raises(ValueError):
        subject.observe_trade("w-1", {"moon_phase": "waxing"}, True)


def test_the_miner_does_not_search_for_conditions():
    assert importlib.import_module(
        BLOCK_PARTS["winner-pattern-miner"]
    ).describe_pattern_mining(a_winner_miner())["searches_for_conditions"] is False


# ---- sequence-pattern-miner -------------------------------------------------

def a_sequence_miner(minimum=4, effect=0.1, shuffle=0.05):
    return SequencePatternMiner(
        minimum_trades=minimum, effect_threshold=effect, shuffle_margin=shuffle,
        now_ns=Clock(),
    )


def _sequence(miner, realiseds, notionals=None, sessions=None):
    notionals = notionals or [1000.0] * len(realiseds)
    sessions = sessions or [index * 600.0 for index in range(len(realiseds))]
    for index, realised in enumerate(realiseds):
        miner.observe_trade(
            f"t-{index}", realised, notionals[index], index * SECOND_NS, sessions[index]
        )


def test_size_growing_after_a_win_is_detectable():
    subject = a_sequence_miner(effect=0.1, shuffle=0.05)
    _sequence(
        subject,
        [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0],
        notionals=[100.0, 2000.0, 100.0, 2000.0, 100.0, 2000.0, 100.0, 2000.0],
    )
    outcome = subject.mine(SIZE_DRIFT)
    assert outcome.effect is not None
    assert abs(outcome.effect) > 0.1


def test_a_pattern_that_survives_shuffling_is_not_about_order():
    subject = a_sequence_miner(effect=0.01, shuffle=1000.0)
    _sequence(subject, [1.0, -1.0, 1.0, -1.0, 2.0, -2.0, 1.0, -1.0])
    outcome = subject.mine(OUTCOME_CONDITIONING)
    assert outcome.state == SURVIVES_SHUFFLING


def test_too_few_trades_means_order_says_nothing():
    subject = a_sequence_miner(minimum=10)
    _sequence(subject, [1.0, -1.0])
    assert subject.mine(STREAKS).state == SEQUENCE_THIN


def test_only_declared_shapes_are_mined():
    subject = a_sequence_miner()
    with pytest.raises(ValueError):
        subject.mine("something-i-noticed")
    assert len(PATTERN_KINDS) == 4


# ---- exploration-pair-decoder -----------------------------------------------

def a_pair_decoder(gap=5.0, size_tolerance=0.05, cost=0.001):
    return ExplorationPairDecoder(
        maximum_opening_gap_seconds=gap, size_tolerance=size_tolerance,
        round_trip_cost_fraction=cost, now_ns=Clock(),
    )


def _pair(decoder, long_realised, short_realised, gap_ns=0, sizes=(1000.0, 1000.0),
          reasons=("target", "target")):
    decoder.observe_leg(
        "p-1", "long", a_closed_trade(realised=long_realised, quantity=10.0),
        reasons[0], 0, sizes[0],
    )
    decoder.observe_leg(
        "p-1", "short", a_closed_trade(realised=short_realised, direction="short",
                                       quantity=10.0),
        reasons[1], gap_ns, sizes[1],
    )


def test_a_pair_that_lost_money_can_still_settle_the_question():
    subject = a_pair_decoder(cost=0.0001)
    _pair(subject, long_realised=-1.0, short_realised=-50.0)
    outcome = subject.decode("p-1")
    assert outcome.state == LONG_SIDE_WON
    assert outcome.verdict.is_conclusive
    assert subject.standing.pairs_that_lost_money_and_settled_the_question == 1


def test_a_difference_inside_the_cost_of_finding_out_establishes_nothing():
    subject = a_pair_decoder(cost=0.01)
    _pair(subject, 1.0, 0.5)
    outcome = subject.decode("p-1")
    assert outcome.state == NO_DIRECTIONAL_EDGE
    assert "manufactures one" in outcome.verdict.reason


def test_legs_opened_apart_measure_the_delay():
    subject = a_pair_decoder(gap=1.0)
    _pair(subject, 10.0, -10.0, gap_ns=60 * SECOND_NS)
    assert subject.decode("p-1").state == NOT_SYMMETRIC


def test_legs_closed_for_different_reasons_are_not_a_comparison():
    subject = a_pair_decoder()
    _pair(subject, 10.0, -10.0, reasons=("stop", "target"))
    assert subject.decode("p-1").state == CLOSED_DIFFERENTLY


def test_an_incomplete_pair_is_not_decoded():
    subject = a_pair_decoder()
    subject.observe_leg("p-1", "long", a_closed_trade(), "target", 0, 1000.0)
    assert subject.decode("p-1").state == PAIR_INCOMPLETE


def test_a_verdict_names_the_instrument_the_pair_was_run_on():
    """Without it no consumer can match a verdict to one of its positions.

    `tail-winner-selector` could not, for the whole of 2026-08-28: 3,531,494
    held positions examined and every one rejected for having no verdict, while
    verdicts were being published a few parts away.
    """
    subject = a_pair_decoder(cost=0.0001)
    _pair(subject, long_realised=20.0, short_realised=-20.0)
    verdict = subject.decode("p-1").verdict
    assert (verdict.venue_id, verdict.symbol) == ("binance-usdm", "BTCUSDT")
    assert verdict.winning_side == "long"


def test_a_verdict_nobody_reached_has_no_winning_side():
    """Inconclusive and unresolved are different facts and must not both read as a side."""
    subject = a_pair_decoder(cost=0.01)
    _pair(subject, 1.0, 0.5)
    assert subject.decode("p-1").verdict.winning_side is None


def test_two_symbols_are_not_a_pair():
    """The difference between two instruments measures the instruments."""
    subject = a_pair_decoder()
    subject.observe_leg("p-1", "long", a_closed_trade(realised=10.0), "target", 0, 1000.0)
    subject.observe_leg(
        "p-1", "short",
        a_closed_trade(realised=-10.0, direction="short", symbol="ETHUSDT"),
        "target", 0, 1000.0,
    )
    outcome = subject.decode("p-1")
    assert outcome.state == DIFFERENT_INSTRUMENTS
    assert outcome.verdict is None
    assert subject.standing.refused_different_instruments == 1


def test_the_verdict_this_decoder_publishes_is_the_one_the_tailgater_can_read():
    """The defect no unit test on either side could see (2026-08-28).

    `tail-winner-selector` declared its own class named `PairVerdict` with four
    fields -- `venue_id`, `winning_symbol`, `losing_symbol`, `has_resolved` -- that
    the published payload has never carried. Its tests built that class, so they
    passed; the contract checkers followed the local annotation, so they passed;
    and the first verdict to reach the part would have killed it with
    AttributeError. Only feeding the real producer's output to the real consumer
    catches that, so this test does exactly that and nothing else.
    """
    from parts.profit_tailgating_bot.tail_winner_selector import TailWinnerSelector

    decoder = a_pair_decoder(cost=0.0001)
    _pair(decoder, long_realised=20.0, short_realised=-20.0)
    published = decoder.decode("p-1").verdict

    selector = TailWinnerSelector(
        maximum_retraced_fraction=0.3, maximum_symbol_share_of_book=0.25,
        minimum_profit_fraction=0.005, default_setup_weight=1.0,
    )
    selector.observe_pair_verdict(published)

    assert selector.standing.verdicts_without_an_instrument == 0
    assert ("binance-usdm", "BTCUSDT") in selector._verdicts


# ---- trade-episode-encoder --------------------------------------------------

class EntryQualityStub:
    def __init__(self, percentile=0.6, chasing=False):
        self.percentile = percentile
        self.was_chasing = chasing


def an_encoder():
    return TradeEpisodeEncoder(now_ns=Clock())


def _encode(encoder, trade_id="t-1", significance=None, **extra):
    return encoder.encode(
        trade_id=trade_id, closed_trade=a_closed_trade(), detector="breakout",
        action="long", outcome="target",
        attribution=AttributionStub(), entry_quality=EntryQualityStub(),
        significance=significance or Significance(), **extra,
    )


def test_a_partial_episode_names_what_is_missing():
    subject = an_encoder()
    outcome = subject.encode(
        trade_id="t-1", closed_trade=a_closed_trade(), detector="d", action="long",
        outcome="target", attribution=AttributionStub(),
    )
    assert outcome.state == EPISODE_INCOMPLETE
    assert set(outcome.missing) <= set(REQUIRED_PIECES)
    assert "fabricated field" in outcome.reason


def test_an_episode_is_never_edited_only_superseded():
    subject = an_encoder()
    _encode(subject)
    outcome = _encode(subject)
    assert outcome.state == SUPERSEDED
    assert len(subject.history_for("t-1")) == 2
    assert not hasattr(subject, "edit")


def test_an_insignificant_outcome_is_kept_and_marked():
    subject = an_encoder()
    outcome = _encode(subject, significance=Significance(is_significant=False, standardised=0.1))
    assert outcome.state == ENCODED
    assert outcome.episode.conditions["is_significant"] is False
    assert subject.standing.insignificant_episodes_kept == 1


def test_a_cluster_travels_into_the_episode():
    class ClusterStub:
        cluster_id = "cluster-1"
        effective_bets = 1.4

    subject = an_encoder()
    outcome = _encode(subject, cluster=ClusterStub())
    assert outcome.episode.conditions["effective_bets"] == 1.4


# ---- trade-narrative-writer -------------------------------------------------

def a_writer(sentences=5):
    return TradeNarrativeWriter(
        relative_tolerance=0.01, maximum_sentences=sentences, now_ns=Clock(),
    )


class EpisodeStub:
    def __init__(self, realised=10.0, conditions=None):
        self.venue_id = "binance-usdm"
        self.symbol = "BTCUSDT"
        self.realised = realised
        self.conditions = conditions or {
            "holding_seconds": 600.0, "entry_percentile": 0.6, "cost_share": 0.1,
        }


def test_a_narrative_can_be_written_without_a_model_at_all():
    subject = a_writer()
    outcome = subject.write("t-1", EpisodeStub())
    assert outcome.state == AWAITING_PHRASING
    assert outcome.narrative.was_written_by_a_model is False
    assert outcome.request is not None


def test_a_sentence_with_an_unsupported_number_is_removed():
    subject = a_writer()
    outcome = subject.write(
        "t-1", EpisodeStub(),
        phrased_text="The trade realised 10.0. Volume was 999999.0 that day.",
    )
    assert outcome.state == WRITTEN
    assert outcome.narrative.sentences_removed


def test_a_causal_claim_without_a_classifier_verdict_is_removed():
    subject = a_writer()
    subject.write(
        "t-1", EpisodeStub(),
        phrased_text="The trade realised 10.0 because the regime turned favourable.",
    )
    assert subject.standing.causal_claims_removed >= 1


def test_a_narrative_is_never_rewritten_later():
    subject = a_writer()
    subject.write("t-1", EpisodeStub(), phrased_text="The trade realised 10.0.")
    outcome = subject.write("t-1", EpisodeStub(), phrased_text="Actually it was fine.")
    assert outcome.state == ALREADY_WRITTEN
    assert "believed now rather than what was understood then" in outcome.reason


# ---- lesson-extractor -------------------------------------------------------

def an_extractor(minimum=2, significant=0.5):
    return LessonExtractor(
        minimum_trades=minimum, minimum_significant_fraction=significant, now_ns=Clock(),
    )


def _support(extractor, change, conditions, count=3, significant=True, effect=1.0):
    for index in range(count):
        extractor.observe_evidence(
            change, conditions, f"t-{change}-{index}", effect, significant
        )


def test_an_instruction_names_the_condition_it_applies_under():
    subject = an_extractor()
    _support(subject, WIDEN_THE_STOP, {"regime": "chop"})
    outcome = subject.extract(WIDEN_THE_STOP, {"regime": "chop"})
    assert outcome.state == EXTRACTED
    assert outcome.instruction.applies_when == {"regime": "chop"}
    assert outcome.instruction.can_be_acted_on


def test_an_unconditional_instruction_is_refused():
    subject = an_extractor()
    _support(subject, WIDEN_THE_STOP, {})
    outcome = subject.extract(WIDEN_THE_STOP, {})
    assert outcome.state == UNCONDITIONAL
    assert "one bad quarter rewrites a working strategy" in outcome.reason


def test_one_trade_produces_a_narrative_not_a_rule():
    subject = an_extractor(minimum=5)
    _support(subject, WIDEN_THE_STOP, {"regime": "chop"}, count=1)
    assert subject.extract(WIDEN_THE_STOP, {"regime": "chop"}).state == LESSON_THIN


def test_insignificant_outcomes_cannot_support_an_instruction():
    subject = an_extractor(significant=0.8)
    _support(subject, WIDEN_THE_STOP, {"regime": "chop"}, significant=False)
    outcome = subject.extract(WIDEN_THE_STOP, {"regime": "chop"})
    assert outcome.state == NOT_SIGNIFICANT
    assert "nothing there to learn from" in outcome.reason


def test_contradictory_instructions_block_each_other():
    subject = an_extractor()
    _support(subject, WIDEN_THE_STOP, {"regime": "chop"})
    _support(subject, TIGHTEN_THE_STOP, {"regime": "chop"})
    subject.extract(WIDEN_THE_STOP, {"regime": "chop"})
    outcome = subject.extract(TIGHTEN_THE_STOP, {"regime": "chop"})
    assert outcome.state == CONTRADICTED
    assert "not what mattered" in outcome.reason


def test_a_change_nothing_can_apply_is_refused():
    subject = an_extractor()
    with pytest.raises(ValueError):
        subject.observe_evidence("be-more-patient", {"regime": "chop"}, "t-1", 1.0, True)
    assert "be-more-patient" not in EXPRESSIBLE_CHANGES


def test_the_extractor_applies_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["lesson-extractor"]
    ).describe_lesson_extraction(an_extractor())["applies_an_instruction"] is False


# ---- trade-replay-verifier --------------------------------------------------

def a_verifier():
    return TradeReplayVerifier(
        quantity_tolerance=1e-9, price_tolerance=1e-9, fee_tolerance=1e-9,
        timestamp_tolerance_seconds=0.001, now_ns=Clock(),
    )


def _both(verifier, fill_id="f-1", journal=None, venue=None):
    base = {"quantity": 1.0, "price": 100.0, "fee": 0.1, "timestamp": 1_000}
    verifier.observe_journal_entry(fill_id, {**base, **(journal or {})})
    verifier.observe_venue_fill(fill_id, {**base, **(venue or {})})


def test_a_matching_record_agrees():
    subject = a_verifier()
    _both(subject)
    assert subject.verify("t-1", ["f-1"]).state == AGREES


def test_a_fill_the_journal_never_saw_is_critical():
    """The position is real and the system does not know it exists."""
    subject = a_verifier()
    subject.observe_venue_fill("f-1", {"quantity": 1.0, "price": 100.0})
    outcome = subject.verify("t-1", ["f-1"])
    assert outcome.state == MISMATCHED
    assert outcome.mismatches[0].field == EXISTENCE
    assert outcome.worst_severity == CRITICAL


def test_a_price_disagreement_is_serious_and_a_fee_one_is_minor():
    subject = a_verifier()
    _both(subject, journal={"price": 99.0, "fee": 0.2})
    outcome = subject.verify("t-1", ["f-1"])
    severities = {mismatch.field: mismatch.severity for mismatch in outcome.mismatches}
    assert severities["price"] == SERIOUS
    assert severities["fee"] == MINOR


def test_a_quantity_disagreement_is_critical():
    subject = a_verifier()
    _both(subject, journal={"quantity": 0.5})
    outcome = subject.verify("t-1", ["f-1"])
    assert any(
        mismatch.field == QUANTITY and mismatch.severity == CRITICAL
        for mismatch in outcome.mismatches
    )


def test_a_clock_drift_corrupts_ordering_and_is_reported():
    subject = a_verifier()
    _both(subject, journal={"timestamp": 1_000_000_000_000})
    outcome = subject.verify("t-1", ["f-1"])
    assert any(mismatch.field == "timestamp" for mismatch in outcome.mismatches)


def test_the_verifier_repairs_nothing():
    described = importlib.import_module(
        BLOCK_PARTS["trade-replay-verifier"]
    ).describe_replay_verification(a_verifier())
    assert described["repairs_the_journal"] is False
    assert described["repairs_made"] == 0
