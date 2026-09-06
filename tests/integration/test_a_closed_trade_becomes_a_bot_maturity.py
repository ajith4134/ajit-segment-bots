"""A closed trade, through the learning chain, to a bot-maturity.

**The replay proved the trading half and never touched this half.**
`operate/replay_a_captured_session.py` imports seven parts -- the fill
simulator, the stop manager, the cost-basis tracker, the fill reconciler, the
excursion tracker, the close detector and the exit chainer -- and mentions the
learning chain zero times. The 2026-09-04 replay closed 252 trades across the
three segments and not one of them reached `pnl-attributor`, so everything
downstream of `closed-trade` has never run on a real trade at all.

That showed up in the audit as `edge-graduation-gate` with `recv {}` and
`judgements 0`, and `bot-maturity` never produced for any of its five consumers
(`opinion-arbiter`, `autonomy-boundary`, `live-switch-guard`,
`exploration-pair-opener`, `opinion-conflict-resolver`). Every part in the chain
was running and none of them was broken -- they were all waiting on a closed
trade that had never come.

This drives the chain with a **real trade from that replay** (RL-063: real
captured data, never an invented fixture), and it is what says the learning half
works before Monday rather than after it:

    closed-trade
      -> pnl-attributor        pnl-attribution
      -> entry-quality-scorer  entry-quality
      -> luck-skill-separator  outcome-significance
      -> trade-episode-encoder trade-episode
      -> bot-scorekeeper       bot-scorecard
      -> edge-graduation-gate  bot-maturity

Two defects were found the first time it ran, both of which could only ever
appear on the first closed trade, and both now guarded in the parts themselves:
`pnl-attributor` published a refused attribution carrying every component at 0.0
with `unexplained` holding the whole PnL and `reconciles=True`, and
`luck-skill-separator` published one with `standardised=None` and
`is_measurable=False`. Both are covered here.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.closed_trade_decoding.entry_quality_scorer import EntryQualityScorer
from parts.closed_trade_decoding.luck_skill_separator import LuckSkillSeparator
from parts.closed_trade_decoding.pnl_attributor import PnlAttributor
from parts.closed_trade_decoding.trade_episode_encoder import TradeEpisodeEncoder
from parts.learning_loop.bot_scorekeeper import BotScorekeeper
from parts.learning_loop.edge_graduation_gate import EdgeGraduationGate
from runtime.trading_types import BUY, SELL, ClosedTrade, Fill

REPLAY = pathlib.Path.home() / (
    ".local/share/ajit-segment-bots/replay/2026-09-04.closed-trades.json"
)
HELD_SECONDS = 600
TRADE_ID = "the-replayed-trade"


def replayed_closed_trades() -> list[ClosedTrade]:
    """Every round trip the 2026-09-04 replay actually closed, as ClosedTrades.

    The replay records an open and a close but no close timestamp, so the hold
    is stated rather than measured and named as such -- it is the one number
    here the tape did not give.
    """
    if not REPLAY.exists():
        pytest.skip(f"no captured replay at {REPLAY}; run operate/replay_a_captured_session.py")
    recorded = json.loads(REPLAY.read_text())
    return [
        ClosedTrade(
            venue_id="upstox",
            symbol=row["symbol"],
            direction=row["direction"],
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            realised_pnl=row["realised_pnl"],
            fees_paid=row["fees_paid"],
            opened_at_ns=row["opened_at_ns"],
            closed_at_ns=row["opened_at_ns"] + HELD_SECONDS * 10**9,
        )
        for row in recorded["trades"]
        if row["closed"]
    ]


@pytest.fixture(scope="module")
def a_real_closed_trade() -> ClosedTrade:
    trades = replayed_closed_trades()
    assert trades, "the replay recorded no closed trade to drive the chain with"
    return trades[0]


def fills_of(trade: ClosedTrade) -> tuple[Fill, Fill]:
    """The two legs, with the replay's own fee split evenly across them."""
    half = trade.fees_paid / 2
    opening, closing = (BUY, SELL) if trade.direction == "long" else (SELL, BUY)
    return (
        Fill(fill_id="entry", venue_id=trade.venue_id, symbol=trade.symbol, side=opening,
             price=trade.entry_price, quantity=trade.quantity, fee=half,
             filled_at_ns=trade.opened_at_ns),
        Fill(fill_id="exit", venue_id=trade.venue_id, symbol=trade.symbol, side=closing,
             price=trade.exit_price, quantity=trade.quantity, fee=half,
             filled_at_ns=trade.closed_at_ns),
    )


def attribution_of(trade: ClosedTrade):
    attributor = PnlAttributor(reconciliation_tolerance=0.01)
    for fill in fills_of(trade):
        attributor.observe_fill(TRADE_ID, fill)
    attributor.observe_decision_price(TRADE_ID, trade.entry_price)
    return attributor.attribute(TRADE_ID, trade)


def entry_score_of(trade: ClosedTrade):
    scorer = EntryQualityScorer(
        window_seconds=300.0, minimum_prices=3, chasing_in_typical_movements=1.0,
    )
    scorer.observe_signal_time(TRADE_ID, trade.opened_at_ns - 60 * 10**9)
    for step in range(10):
        scorer.observe_price(
            trade.venue_id, trade.symbol, trade.entry_price * (1 + 0.001 * step),
            trade.opened_at_ns - (60 - step * 5) * 10**9,
        )
    scorer.observe_typical_movement(trade.venue_id, trade.symbol, trade.entry_price * 0.01)
    return scorer.score(TRADE_ID, trade)


def significance_of(trade: ClosedTrade):
    separator = LuckSkillSeparator(significance_threshold=2.0, minimum_comparable_outcomes=1)
    separator.observe_daily_volatility(trade.venue_id, trade.symbol, 0.05)
    return separator.assess(TRADE_ID, trade)


# ---- the three analyses a closed trade must produce --------------------------

def test_a_real_closed_trade_is_attributed(a_real_closed_trade):
    outcome = attribution_of(a_real_closed_trade)
    assert outcome.is_usable, outcome.reason
    assert outcome.attribution.realised_pnl == a_real_closed_trade.realised_pnl


def test_a_real_closed_trade_is_scored_for_entry_quality(a_real_closed_trade):
    scored = entry_score_of(a_real_closed_trade)
    assert scored.is_usable, scored.reason
    assert scored.quality is not None


def test_a_real_closed_trade_is_separated_from_noise(a_real_closed_trade):
    outcome = significance_of(a_real_closed_trade)
    assert outcome.is_usable, outcome.reason
    assert outcome.significance.is_measurable


# ---- the two defects that could only appear on the first closed trade --------

def test_an_attribution_with_no_fills_is_refused_and_says_so(a_real_closed_trade):
    """It is not empty when refused: every component reads 0.0, `unexplained`
    holds the whole realised PnL, and `reconciles` is True because unexplained
    absorbs everything. A consumer cannot tell that from a measured attribution,
    which is why the part withholds it rather than publishing it."""
    refused = PnlAttributor(reconciliation_tolerance=0.01).attribute(
        TRADE_ID, a_real_closed_trade,
    )
    assert not refused.is_usable
    assert refused.attribution.residual == a_real_closed_trade.realised_pnl
    assert refused.attribution.reconciles is True
    assert all(
        component == 0.0
        for name, component in refused.attribution.components.items()
        if name != "unexplained"
    )


def test_a_significance_with_no_measured_volatility_is_refused(a_real_closed_trade):
    refused = LuckSkillSeparator(
        significance_threshold=2.0, minimum_comparable_outcomes=1,
    ).assess(TRADE_ID, a_real_closed_trade)
    assert not refused.is_usable
    assert refused.significance.standardised is None
    assert refused.significance.is_measurable is False


# ---- the chain, end to end ---------------------------------------------------

def test_the_episode_refuses_until_every_required_piece_has_landed(a_real_closed_trade):
    """Filling a missing piece with a default produces a record that looks
    complete, and every statistic over it silently includes a fabricated field.
    This is the encoder's own reason, and the guards above are what keep a
    refused analysis from arriving as a complete-looking one."""
    outcome = TradeEpisodeEncoder().encode(
        TRADE_ID, a_real_closed_trade, detector="momentum-burst-detector",
        action="bought", outcome="target",
        attribution=attribution_of(a_real_closed_trade).attribution,
        entry_quality=None,
        significance=significance_of(a_real_closed_trade).significance,
    )
    assert outcome.episode is None
    assert "entry_quality" in outcome.missing


def test_a_closed_trade_becomes_a_trade_episode(a_real_closed_trade):
    """The wrapper is never what travels: each part publishes the inner value
    (`outcome.attribution`, `scored.quality`, `outcome.significance`), which is
    what the encoder reads its fields off."""
    outcome = TradeEpisodeEncoder().encode(
        TRADE_ID, a_real_closed_trade, detector="momentum-burst-detector",
        action="bought", outcome="target",
        attribution=attribution_of(a_real_closed_trade).attribution,
        entry_quality=entry_score_of(a_real_closed_trade).quality,
        significance=significance_of(a_real_closed_trade).significance,
    )
    assert outcome.episode is not None, outcome.reason
    assert not outcome.missing


def test_a_bot_that_clears_every_condition_graduates(a_real_closed_trade):
    """The end of the chain, and the answer to why `bot-maturity` has never been
    produced: nothing is broken here, it has simply never been fed."""
    won = a_real_closed_trade.realised_pnl > 0
    scorekeeper = BotScorekeeper(
        prior_hit_rate=0.5, prior_weight=2.0, half_life_observations=50,
        minimum_observations=1,
    )
    scorekeeper.record_opinion_outcome(
        bot="bull", detector="momentum-burst-detector", regime="trending",
        stated_probability=0.7, the_opinion_was_right=won,
        realised=a_real_closed_trade.realised_pnl,
    )
    by_regime = scorekeeper.scorecard_for("bull").describe()["by_regime"]
    assert by_regime["trending"]["trades"] == 1

    gate = EdgeGraduationGate(
        minimum_hit_rate=0.55, minimum_decision_quality=0.5,
        minimum_coverage=0.5, default_required_trades=3,
    )
    for outcome_was_a_win in (True, True, True, False):
        gate.observe_closed_trade("bull", "trending", outcome_was_a_win)
    gate.observe_decision_quality("bull", "trending", 0.8)
    gate.observe_refutation_verdict("bull", "survived")
    gate.observe_trial_verdict("bull", True)
    gate.observe_coverage("bull", "trending", 0.9)

    maturity = gate.judge("bull", "trending")
    assert maturity.is_mature, maturity.reason
    assert maturity.may_trade_live
    assert maturity.failing_conditions == ()


def test_one_failing_condition_is_enough_to_keep_a_bot_exploring(a_real_closed_trade):
    """They are not tradeable against each other: profit alone would graduate a
    bot that made money badly."""
    gate = EdgeGraduationGate(
        minimum_hit_rate=0.55, minimum_decision_quality=0.5,
        minimum_coverage=0.5, default_required_trades=3,
    )
    for outcome_was_a_win in (True, True, True, False):
        gate.observe_closed_trade("bull", "trending", outcome_was_a_win)
    gate.observe_decision_quality("bull", "trending", 0.8)
    gate.observe_refutation_verdict("bull", "survived")
    gate.observe_trial_verdict("bull", True)
    # Coverage deliberately left unobserved -- its record is about a slice it selected.
    maturity = gate.judge("bull", "trending")
    assert not maturity.is_mature
    assert not maturity.may_trade_live
    assert maturity.failing_conditions
