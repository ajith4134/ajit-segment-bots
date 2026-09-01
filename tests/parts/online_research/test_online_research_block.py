"""The online-research block: reading the outside world without adopting it.

Every part here consumes material this system did not produce, and each test below
is about one specific way that material misleads: a leaderboard that conditions on
having won, a wallet showing one leg of a hedge, a return published gross, a forum
where six accounts sound like a crowd, a repository whose stars measure marketing.

None of these parts is allowed to conclude on behalf of the system. They record what
a source said, with enough attached to go back and check it.
"""

import importlib

import pytest

from parts.online_research.arxiv_feed_reader import (
    ALREADY_HAVE_THIS_VERSION, ArxivFeedReader, FETCHED, NOTHING_MATCHED, NO_GAP as ARXIV_NO_GAP,
    NO_IDENTIFIER, NOT_WORTH_FETCHING, REVISED, WITHDRAWN,
)
from parts.online_research.copy_latency_estimator import CopyLatencyEstimator
from parts.online_research.copy_worthiness_scorer import (
    BOOK_NOT_VISIBLE, CopyWorthinessScorer, LATENCY_NOT_MEASURED, NOT_VERIFIED,
    NOT_WORTH_COPYING, TOO_LITTLE_EVIDENCE, WORTH_COPYING,
)
from parts.online_research.edge_comparator import (
    EdgeComparator, GAP_FOUND, NO_GAP as NO_EDGE_GAP, NO_OVERLAP, NO_VENUE_ACCESS,
    TOO_FEW_TRADES as EDGE_TOO_FEW_TRADES, WE_ARE_AHEAD,
)
from parts.online_research.exchange_announcement_reader import (
    ALREADY_IN_EFFECT, ANNOUNCEMENT_KINDS, CHANGES_THE_RULES, DELISTING,
    ExchangeAnnouncementReader, LEVERAGE_CHANGE, NO_SYMBOL_MATCHED, PROMOTION,
    RECORDED as ANNOUNCEMENT_RECORDED, UNCLASSIFIED,
)
from parts.online_research.github_strategy_miner import (
    GithubStrategyMiner, KNOWN_DEFECTS, LOOKAHEAD, Mechanism, MINED, NOTHING_FOUND,
    NO_COST_MODEL, NOT_TESTABLE_HERE, ONLY_A_DEFECT,
)
from parts.online_research.leaderboard_reader import (
    EMPTY_BOARD, LeaderboardReader, RATE_LIMITED, READ as BOARD_READ, READ_FAILED,
)
from parts.online_research.onchain_position_reader import (
    NOT_CONFIRMED, NOT_PUBLIC, OnchainPositionReader, READ as POSITION_READ, STALE_HEIGHT,
)
from parts.online_research.options_flow_reader import (
    AT_THE_MONEY, BUY, CALL, EXPIRED, FAR_OUT_OF_THE_MONEY, OptionsFlowReader, PUT,
    RECORDED as FLOW_RECORDED, SELL, TOO_SMALL,
)
from parts.online_research.strategy_decoder import (
    AWAITING_PHRASING, DECODED, DIRECTION_BIAS, MEASURABLE_REGULARITIES, NO_PATTERN,
    StrategyDecoder, TOO_FEW_POSITIONS,
)
from parts.online_research.trader_record_verifier import (
    CONTRADICTED, ONE_TRADE, TOO_SHORT, TraderRecordVerifier, UNVERIFIABLE, VERIFIED,
)
from runtime.external_research_types import (
    ExternalPosition, TrackedTrader, VENUE_PUBLISHED, WebIdea,
)
from runtime.knowledge_types import TradeEpisode
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "leaderboard-reader": "parts.online_research.leaderboard_reader",
    "onchain-position-reader": "parts.online_research.onchain_position_reader",
    "strategy-decoder": "parts.online_research.strategy_decoder",
    "edge-comparator": "parts.online_research.edge_comparator",
    "copy-worthiness-scorer": "parts.online_research.copy_worthiness_scorer",
    "trader-record-verifier": "parts.online_research.trader_record_verifier",
    "copy-latency-estimator": "parts.online_research.copy_latency_estimator",
    "exchange-announcement-reader": "parts.online_research.exchange_announcement_reader",
    "options-flow-reader": "parts.online_research.options_flow_reader",
    "arxiv-feed-reader": "parts.online_research.arxiv_feed_reader",
    "github-strategy-miner": "parts.online_research.github_strategy_miner",
}

DAY_NS = 86_400_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


class TickingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def a_trader(identity="alice", public=True, venue="binance-usdm"):
    return TrackedTrader(
        trader_id=f"{venue}:{identity}", venue_id=venue, identity_reference=identity,
        source_kind=VENUE_PUBLISHED, rank=1, period_days=7, reported_return=2.0,
        is_public_by_choice=public, first_seen_at_ns=0, observed_at_ns=0,
    )


def a_position(trader_id="binance-usdm:alice", symbol="BTCUSDT", side="long",
               notional=10_000.0, full_book=True, opened_at_ns=0, venue="binance-usdm"):
    return ExternalPosition(
        trader_id=trader_id, venue_id=venue, symbol=symbol, side=side, notional=notional,
        entry_price=70_000.0, leverage=5.0, opened_at_ns=opened_at_ns, observed_at_ns=0,
        is_full_book=full_book, completeness="complete" if full_book else "partial",
        source_reference="block:100",
    )


def an_episode(symbol="BTCUSDT", realised=0.01, opened_at_ns=0, closed_at_ns=DAY_NS):
    return TradeEpisode(
        episode_id=f"e-{opened_at_ns}", venue_id="binance-usdm", symbol=symbol,
        detector="d", regime="trending", conditions={}, action="long", outcome="target",
        realised=realised, opened_at_ns=opened_at_ns, closed_at_ns=closed_at_ns,
        narrative="",
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- leaderboard-reader -----------------------------------------------------

def a_board_reader(reads=5, window=60.0, monotonic=None, clock=None):
    return LeaderboardReader(
        reads_per_window=reads, window_seconds=window,
        monotonic=monotonic or TickingClock(), now_ns=clock or Clock(),
    )


def test_the_traders_who_fell_off_the_board_are_kept():
    """They are the losses, and a reader that keeps only the current page deletes them."""
    subject = a_board_reader()
    rows = [{"identity_reference": "alice", "rank": 1, "is_public": True}]
    subject.install_reader(lambda venue, period: (rows, len(rows)))
    subject.read("binance-usdm", 7)
    subject.install_reader(
        lambda venue, period: ([{"identity_reference": "bob", "rank": 1}], 1)
    )
    result = subject.read("binance-usdm", 7)
    assert result.disappeared == ("alice",)
    assert subject.standing.traders_that_fell_off == 1


def test_a_partial_page_does_not_report_the_tail_as_having_fallen_off():
    subject = a_board_reader()
    full = [{"identity_reference": name} for name in ("alice", "bob", "carol")]
    subject.install_reader(lambda venue, period: (full, 3))
    subject.read("binance-usdm", 7)
    subject.install_reader(lambda venue, period: (full[:1], 3))
    result = subject.read("binance-usdm", 7)
    assert result.completeness == "partial"
    assert result.disappeared == ()


def test_the_window_travels_with_every_trader():
    """A 7-day rank and a 30-day rank are different claims."""
    subject = a_board_reader()
    subject.install_reader(
        lambda venue, period: ([{"identity_reference": "alice", "rank": 3}], 1)
    )
    result = subject.read("binance-usdm", 7)
    assert result.traders[0].period_days == 7


def test_a_trader_who_hides_their_positions_is_tracked_but_not_readable():
    subject = a_board_reader()
    subject.install_reader(
        lambda venue, period: ([{"identity_reference": "alice", "is_public": False}], 1)
    )
    trader = subject.read("binance-usdm", 7).traders[0]
    assert trader.can_be_read_further is False


def test_an_unread_board_and_an_empty_board_are_different_facts():
    subject = a_board_reader()
    subject.install_reader(lambda venue, period: ([], 0))
    assert subject.read("binance-usdm", 7).state == EMPTY_BOARD

    def failing(venue, period):
        raise RuntimeError("network")

    subject.install_reader(failing)
    assert subject.read("binance-usdm", 7).state == READ_FAILED


def test_the_reader_is_rate_limited_per_venue():
    clock = TickingClock()
    subject = a_board_reader(reads=1, window=60.0, monotonic=clock)
    subject.install_reader(lambda venue, period: ([{"identity_reference": "a"}], 1))
    assert subject.read("binance-usdm", 7).state == BOARD_READ
    assert subject.read("binance-usdm", 7).state == RATE_LIMITED
    clock.now = 61.0
    assert subject.read("binance-usdm", 7).state == BOARD_READ


def test_the_reader_does_not_rank_or_judge():
    described = importlib.import_module(
        BLOCK_PARTS["leaderboard-reader"]
    ).describe_leaderboard_reading(a_board_reader())
    assert described["ranks_traders_itself"] is False
    assert described["treats_rank_as_skill"] is False


# ---- onchain-position-reader ------------------------------------------------

def a_position_reader(confirmations=2, behind=5):
    return OnchainPositionReader(
        confirmations_required=confirmations, maximum_blocks_behind=behind,
        now_ns=Clock(),
    )


def test_an_unconfirmed_position_is_a_proposal_not_a_position():
    subject = a_position_reader(confirmations=3)
    subject.install_reader(
        lambda identity: (
            [{"symbol": "BTCUSDT", "side": "long", "confirmations": 1, "is_full_book": True}],
            100, 100,
        )
    )
    assert subject.read(a_trader()).state == NOT_CONFIRMED


def test_a_partial_book_is_marked_so_one_leg_is_never_read_as_a_view():
    subject = a_position_reader()
    subject.install_reader(
        lambda identity: (
            [{"symbol": "ETHUSDT", "side": "long", "confirmations": 5, "is_full_book": False}],
            100, 100,
        )
    )
    result = subject.read(a_trader())
    assert result.is_full_book is False
    assert result.positions[0].can_be_reasoned_about_alone is False
    assert "the invisible other leg may reverse it" in result.reason


def test_a_node_answering_from_an_old_block_is_refused():
    """The block is the timestamp, not the response header."""
    subject = a_position_reader(behind=3)
    subject.install_reader(lambda identity: ([], 90, 100))
    assert subject.read(a_trader()).state == STALE_HEIGHT


def test_six_wallets_of_one_owner_are_one_owner():
    subject = a_position_reader()
    subject.observe_wallet_owner("wallet-a", "owner-1")
    subject.observe_wallet_owner("wallet-b", "owner-1")
    subject.install_reader(
        lambda identity: (
            [
                {"symbol": "BTCUSDT", "side": "long", "confirmations": 5,
                 "is_full_book": True, "wallet": "wallet-a"},
                {"symbol": "BTCUSDT", "side": "long", "confirmations": 5,
                 "is_full_book": True, "wallet": "wallet-b"},
            ],
            100, 100,
        )
    )
    result = subject.read(a_trader())
    assert result.state == POSITION_READ
    assert result.wallets_seen == 1


def test_a_trader_who_hid_their_book_is_not_guessed_at():
    subject = a_position_reader()
    subject.install_reader(lambda identity: ([], 100, 100))
    result = subject.read(a_trader(public=False))
    assert result.state == NOT_PUBLIC
    assert "guessing the book from their return would be inventing the evidence" in result.reason


# ---- trader-record-verifier -------------------------------------------------

def a_verifier(days=7.0, trades=5, tolerance=0.2, single=0.5):
    return TraderRecordVerifier(
        minimum_days=days, minimum_trades=trades, contradiction_tolerance=tolerance,
        single_trade_share=single, now_ns=Clock(),
    )


def _record_trades(subject, trader_id, returns, leverage=None):
    for index, value in enumerate(returns):
        subject.observe_closed_position(
            trader_id, f"p-{index}", value,
            opened_at_ns=index * DAY_NS, closed_at_ns=(index + 1) * DAY_NS,
            leverage=leverage,
        )


def test_a_record_with_nothing_reconstructible_is_unverifiable_not_false():
    subject = a_verifier()
    subject.observe_claim("t-1", 4.0)
    record = subject.verify("t-1")
    assert record.verification_state == UNVERIFIABLE
    assert "not the same as the claim being false" in record.reason


def test_a_claim_the_positions_do_not_support_is_contradicted_and_kept():
    subject = a_verifier(tolerance=0.2)
    _record_trades(subject, "t-1", [0.01] * 10)
    subject.observe_claim("t-1", 4.0)
    record = subject.verify("t-1")
    assert record.verification_state == CONTRADICTED
    assert record.claimed_return == 4.0
    assert record.verifiable_return < 4.0


def test_a_record_that_is_one_lucky_position_says_so():
    """The shape behind nearly every spectacular public account."""
    subject = a_verifier(single=0.5)
    _record_trades(subject, "t-1", [4.0] + [0.001] * 9)
    record = subject.verify("t-1")
    assert record.verification_state == ONE_TRADE
    assert record.is_one_lucky_trade


def test_a_short_record_is_refused_however_good_it_looks():
    subject = a_verifier(days=30.0, trades=50)
    _record_trades(subject, "t-1", [0.2] * 5)
    assert subject.verify("t-1").verification_state == TOO_SHORT


def test_leverage_is_reported_because_a_return_is_not_comparable_without_it():
    subject = a_verifier()
    _record_trades(subject, "t-1", [0.02] * 10, leverage=50.0)
    subject.observe_claim("t-1", 0.2)
    record = subject.verify("t-1")
    assert record.was_leveraged_beyond == 50.0
    assert record.verification_state == VERIFIED


def test_the_verifier_never_upgrades_a_claim():
    assert importlib.import_module(
        BLOCK_PARTS["trader-record-verifier"]
    ).describe_verification(a_verifier())["ever_upgrades_a_claim"] is False


# ---- copy-latency-estimator -------------------------------------------------

def a_latency_estimator(window=50, minimum=3):
    return CopyLatencyEstimator(
        window=window, prior_delay_seconds=5.0, prior_adverse_move_fraction=0.001,
        adverse_quantile=0.8, minimum_observations=minimum, now_ns=Clock(),
    )


def test_adverse_is_worse_for_the_copier_and_depends_on_the_side():
    subject = a_latency_estimator()
    worse_long = subject.observe("binance-usdm", "BTCUSDT", "long", 100.0, 101.0, 2.0)
    worse_short = subject.observe("binance-usdm", "BTCUSDT", "short", 100.0, 99.0, 2.0)
    assert worse_long.adverse_move_fraction > 0
    assert worse_short.adverse_move_fraction > 0


def test_a_favourable_case_is_kept_in_the_sample():
    """Dropping them measures the cost of the bad half only."""
    subject = a_latency_estimator()
    observation = subject.observe("binance-usdm", "BTCUSDT", "long", 100.0, 99.0, 1.0)
    assert observation.adverse_move_fraction < 0
    assert subject.standing.favourable_cases == 1


def test_latency_is_measured_per_symbol():
    subject = a_latency_estimator(minimum=2)
    for _ in range(5):
        subject.observe("binance-usdm", "BTCUSDT", "long", 100.0, 100.02, 1.0)
        subject.observe("binance-usdm", "TINYUSDT", "long", 100.0, 102.0, 1.0)
    liquid = subject.estimate("binance-usdm", "BTCUSDT")
    thin = subject.estimate("binance-usdm", "TINYUSDT")
    assert thin.adverse_move_fraction > liquid.adverse_move_fraction * 10


def test_an_unmeasured_symbol_says_it_is_not_fitted():
    estimate = a_latency_estimator().estimate("binance-usdm", "NEWUSDT")
    assert estimate.is_measured is False
    assert estimate.observations == 0


def test_the_estimator_does_not_use_one_global_latency():
    assert importlib.import_module(
        BLOCK_PARTS["copy-latency-estimator"]
    ).describe_latency_estimation(a_latency_estimator())["uses_one_global_latency"] is False


# ---- copy-worthiness-scorer -------------------------------------------------

def a_copy_scorer(minimum=3, threshold=0.001):
    return CopyWorthinessScorer(
        minimum_positions=minimum, worth_copying_threshold=threshold,
        prior_follow_success=0.5, prior_weight=4.0, half_life_observations=200,
        minimum_follow_observations=3, now_ns=Clock(),
    )


def a_verified_record(trader_id="binance-usdm:alice", state=VERIFIED, verifiable=0.4):
    verifier = a_verifier()
    _record_trades(verifier, trader_id, [0.03] * 10)
    verifier.observe_claim(trader_id, 0.3)
    return verifier.verify(trader_id)


def _measured_latency(symbol="BTCUSDT", adverse=0.0005):
    estimator = a_latency_estimator(minimum=2)
    for _ in range(5):
        estimator.observe("binance-usdm", symbol, "long", 100.0, 100.0 * (1 + adverse), 1.0)
    return estimator.estimate("binance-usdm", symbol)


def test_a_trader_is_scored_on_the_copyable_return_not_their_return():
    subject = a_copy_scorer()
    subject.observe_verified_record(a_verified_record())
    subject.observe_latency(_measured_latency())
    for index in range(5):
        subject.observe_position(a_position(), realised_return=0.01)
    score = subject.score("binance-usdm:alice")
    assert score.copyable_return < score.their_return
    assert score.lost_to_latency > 0


def test_a_trader_whose_edge_lives_in_the_delay_is_not_worth_copying():
    subject = a_copy_scorer(threshold=0.01)
    subject.observe_verified_record(a_verified_record())
    subject.observe_latency(_measured_latency(adverse=0.02))
    for index in range(5):
        subject.observe_position(a_position(), realised_return=0.01)
    score = subject.score("binance-usdm:alice")
    assert score.state == NOT_WORTH_COPYING
    assert "the window this system cannot reach" in score.reason


def test_an_unmeasured_latency_is_refused_rather_than_treated_as_zero():
    """The assumption that makes every trader look copyable."""
    subject = a_copy_scorer()
    subject.observe_verified_record(a_verified_record())
    for index in range(5):
        subject.observe_position(a_position(), realised_return=0.01)
    assert subject.score("binance-usdm:alice").state == LATENCY_NOT_MEASURED


def test_a_partial_book_is_not_scored():
    subject = a_copy_scorer()
    subject.observe_verified_record(a_verified_record())
    subject.observe_latency(_measured_latency())
    for index in range(5):
        subject.observe_position(a_position(full_book=False), realised_return=0.01)
    assert subject.score("binance-usdm:alice").state == BOOK_NOT_VISIBLE


def test_an_unverified_record_is_not_scored():
    subject = a_copy_scorer()
    for index in range(5):
        subject.observe_position(a_position(), realised_return=0.01)
    assert subject.score("binance-usdm:alice").state == NOT_VERIFIED


def test_too_few_positions_is_refused_rather_than_scored_thinly():
    subject = a_copy_scorer(minimum=10)
    subject.observe_verified_record(a_verified_record())
    subject.observe_latency(_measured_latency())
    subject.observe_position(a_position(), realised_return=0.01)
    assert subject.score("binance-usdm:alice").state == TOO_LITTLE_EVIDENCE


def test_a_trader_who_survives_the_delay_is_worth_copying():
    subject = a_copy_scorer(threshold=0.001)
    subject.observe_verified_record(a_verified_record())
    subject.observe_latency(_measured_latency(adverse=0.0001))
    for index in range(5):
        subject.observe_position(a_position(), realised_return=0.02)
    score = subject.score("binance-usdm:alice")
    assert score.state == WORTH_COPYING
    assert score.is_worth_copying


def test_the_scorer_does_not_decide_whether_to_follow():
    described = importlib.import_module(
        BLOCK_PARTS["copy-worthiness-scorer"]
    ).describe_copy_scoring(a_copy_scorer())
    assert described["decides_whether_to_follow"] is False
    assert described["scores_on_their_return"] is False


# ---- strategy-decoder -------------------------------------------------------

def a_decoder(minimum=3, strength=0.6):
    return StrategyDecoder(
        minimum_positions=minimum, strength_threshold=strength,
        relative_tolerance=0.01, maximum_sentences=4, now_ns=Clock(),
    )


def _decodable_positions(subject, count=10, side="long"):
    for index in range(count):
        subject.observe_position(
            a_position(side=side, opened_at_ns=index * 3_600_000_000_000),
            closed_at_ns=index * 3_600_000_000_000 + 600_000_000_000,
            realised_return=0.01,
            move_before_entry=0.02,
        )


def test_randomness_is_a_possible_answer():
    """A decoder that cannot return it describes noise as a strategy every time."""
    subject = a_decoder(strength=0.99)
    for index in range(10):
        subject.observe_position(
            a_position(
                symbol=f"SYM{index}USDT",
                side="long" if index % 2 == 0 else "short",
                notional=1_000.0 * (index + 1),
                opened_at_ns=index * DAY_NS,
            ),
            closed_at_ns=index * DAY_NS + (index + 1) * 3_600_000_000_000,
        )
    decoded = subject.decode("binance-usdm:alice")
    assert decoded.state == NO_PATTERN
    assert subject.standing.no_pattern_found == 1


def test_the_regularities_are_measured_in_code_before_a_model_sees_anything():
    subject = a_decoder()
    _decodable_positions(subject)
    measured = subject.measure("binance-usdm:alice")
    assert {regularity.kind for regularity in measured} <= set(MEASURABLE_REGULARITIES)
    assert any(regularity.kind == DIRECTION_BIAS for regularity in measured)


def test_the_model_is_asked_only_to_phrase_what_was_measured():
    subject = a_decoder()
    _decodable_positions(subject)
    decoded = subject.decode("binance-usdm:alice")
    assert decoded.state == AWAITING_PHRASING
    assert decoded.request is not None
    assert "do not infer intent" in decoded.request.instruction


def test_a_phrased_sentence_with_an_unsupported_number_is_removed():
    subject = a_decoder()
    _decodable_positions(subject)
    subject.decode("binance-usdm:alice")
    decoded = subject.decode(
        "binance-usdm:alice",
        phrased_text="This trader is long 1.00 of the time. They target 91.7% gains daily.",
    )
    assert decoded.state == DECODED
    assert subject.standing.sentences_removed_as_unsupported >= 1


def test_too_few_positions_measures_nothing():
    subject = a_decoder(minimum=20)
    _decodable_positions(subject, count=5)
    assert subject.decode("binance-usdm:alice").state == TOO_FEW_POSITIONS


def test_the_decoder_does_not_let_a_model_measure():
    assert importlib.import_module(
        BLOCK_PARTS["strategy-decoder"]
    ).describe_strategy_decoding(a_decoder())["lets_a_model_measure_anything"] is False


# ---- edge-comparator --------------------------------------------------------

def a_comparator(trades=3, days=1.0, material=0.005):
    return EdgeComparator(
        minimum_trades_each_side=trades, minimum_overlapping_days=days,
        material_difference=material, now_ns=Clock(),
    )


def _both_sides(subject, their_gross=0.05, our_net=0.01, symbol="BTCUSDT", count=5):
    for index in range(count):
        subject.observe_their_trade(
            "alice", "binance-usdm", symbol, their_gross, 10_000.0,
            opened_at_ns=index * DAY_NS, closed_at_ns=(index + 1) * DAY_NS,
        )
        subject.observe_our_episode(
            an_episode(symbol, our_net, index * DAY_NS, (index + 1) * DAY_NS)
        )


def test_their_gross_return_is_netted_with_this_systems_own_costs():
    subject = a_comparator()
    subject.install_cost_model(lambda venue, symbol, notional: 0.004)
    _both_sides(subject, their_gross=0.05, our_net=0.01)
    comparison = subject.compare("alice")
    assert subject.standing.costs_subtracted_from_their_side > 0
    assert comparison.their_edge == pytest.approx(0.046)


def test_records_that_do_not_overlap_are_not_compared():
    """Comparing a bull month with a chop month compares the market."""
    subject = a_comparator(days=5.0)
    for index in range(5):
        subject.observe_their_trade(
            "alice", "binance-usdm", "BTCUSDT", 0.05, 10_000.0,
            opened_at_ns=index * DAY_NS, closed_at_ns=(index + 1) * DAY_NS,
        )
        subject.observe_our_episode(
            an_episode("BTCUSDT", 0.01, (index + 100) * DAY_NS, (index + 101) * DAY_NS)
        )
    assert subject.compare("alice").state == NO_OVERLAP


def test_a_different_universe_is_the_finding_not_a_worse_edge():
    subject = a_comparator()
    for index in range(5):
        subject.observe_their_trade(
            "alice", "binance-usdm", "SOMETHINGUSDT", 0.05, 10_000.0,
            index * DAY_NS, (index + 1) * DAY_NS,
        )
        subject.observe_our_episode(an_episode("BTCUSDT", 0.01, index * DAY_NS, (index + 1) * DAY_NS))
    comparison = subject.compare("alice")
    assert comparison.state == GAP_FOUND
    assert "a different universe" in comparison.reason


def test_an_unreachable_gap_names_its_blocker():
    subject = a_comparator()
    subject.declare_blocker("they trade a venue with no account here", NO_VENUE_ACCESS)
    _both_sides(subject, their_gross=0.05, our_net=0.01)
    comparison = subject.compare("alice", "they trade a venue with no account here")
    assert comparison.gap.is_reachable_here is False
    assert comparison.gap.blocked_by == NO_VENUE_ACCESS


def test_a_difference_inside_the_threshold_is_not_a_gap():
    subject = a_comparator(material=0.05)
    _both_sides(subject, their_gross=0.012, our_net=0.01)
    comparison = subject.compare("alice")
    assert comparison.state == NO_EDGE_GAP
    assert "noise wearing a conclusion" in comparison.reason


def test_this_system_being_ahead_is_a_reportable_outcome():
    subject = a_comparator()
    _both_sides(subject, their_gross=0.001, our_net=0.05)
    assert subject.compare("alice").state == WE_ARE_AHEAD


def test_unequal_samples_are_refused():
    subject = a_comparator(trades=5)
    subject.observe_their_trade("alice", "binance-usdm", "BTCUSDT", 0.05, 1.0, 0, DAY_NS)
    for index in range(10):
        subject.observe_our_episode(an_episode("BTCUSDT", 0.01, index * DAY_NS, (index + 1) * DAY_NS))
    assert subject.compare("alice").state == EDGE_TOO_FEW_TRADES



# ---- exchange-announcement-reader -------------------------------------------

def an_announcement_reader(clock=None):
    reader = ExchangeAnnouncementReader(now_ns=clock or Clock())
    reader.observe_universe("binance-usdm", {"BTCUSDT", "ETHUSDT", "BTCDOMUSDT"})
    return reader


def an_announcement_row(announcement_id="a-1", kind=DELISTING, symbols=None,
                        headline="BTCUSDT will be delisted", effective_at_ns=None,
                        published_at_ns=0):
    return {
        "announcement_id": announcement_id, "venue_id": "binance-usdm", "kind": kind,
        "symbols": symbols, "headline": headline, "effective_at_ns": effective_at_ns,
        "published_at_ns": published_at_ns,
    }


def test_only_some_kinds_change_how_a_symbol_trades():
    clock = Clock()
    subject = an_announcement_reader(clock)
    rule_change = subject.read(
        an_announcement_row("a-1", LEVERAGE_CHANGE, ("BTCUSDT",),
                            effective_at_ns=clock.now_ns + DAY_NS)
    )
    informational = subject.read(
        an_announcement_row("a-2", PROMOTION, ("BTCUSDT",),
                            effective_at_ns=clock.now_ns + DAY_NS)
    )
    assert rule_change.changes_the_rules is True
    assert informational.changes_the_rules is False
    assert set(CHANGES_THE_RULES) <= set(ANNOUNCEMENT_KINDS)


def test_effective_time_and_publication_time_are_different_stamps():
    clock = Clock()
    subject = an_announcement_reader(clock)
    result = subject.read(
        an_announcement_row(effective_at_ns=clock.now_ns + 3 * DAY_NS, published_at_ns=clock.now_ns)
    )
    assert result.state == ANNOUNCEMENT_RECORDED
    assert result.seconds_until_effective == pytest.approx(3 * 86_400.0)
    assert result.needs_acting_on_now is False


def test_a_rule_change_already_in_effect_needs_acting_on_now():
    clock = Clock()
    subject = an_announcement_reader(clock)
    result = subject.read(
        an_announcement_row(effective_at_ns=clock.now_ns - DAY_NS)
    )
    assert result.state == ALREADY_IN_EFFECT
    assert result.needs_acting_on_now


def test_symbols_are_matched_against_the_universe_not_by_substring():
    """A substring match on BTC hits BTCDOMUSDT too."""
    subject = an_announcement_reader()
    subject.observe_alias("BTC/USDT", "BTCUSDT")
    matched = subject.symbols_named_in("binance-usdm", "Notice regarding BTC/USDT perpetual")
    assert matched == ("BTCUSDT",)


def test_a_notice_naming_nothing_in_the_universe_is_recorded_as_such():
    subject = an_announcement_reader()
    result = subject.read(
        an_announcement_row(symbols=(), headline="DOGEUSDT listing")
    )
    assert result.state == NO_SYMBOL_MATCHED


def test_an_unknown_kind_is_surfaced_rather_than_dropped():
    subject = an_announcement_reader()
    subject.read(an_announcement_row(kind="a-brand-new-category", symbols=("BTCUSDT",)))
    assert subject.standing.unclassified_kinds == 1
    assert "a-brand-new-category" in subject.standing.unknown_kind_names


# ---- options-flow-reader ----------------------------------------------------

def an_options_reader(minimum=50_000.0, clock=None):
    reader = OptionsFlowReader(
        minimum_premium=minimum, at_the_money_band=0.02, far_out_band=0.2,
        now_ns=clock or Clock(),
    )
    reader.observe_universe({"BTC"})
    reader.observe_spot("BTC", 70_000.0)
    return reader


def an_options_row(trade_id="o-1", strike=70_000.0, option_kind=CALL, side=BUY,
                   contracts=100.0, premium=500_000.0, expiry_ns=None, clock=None,
                   is_block=True):
    return {
        "trade_id": trade_id, "symbol": "BTC-70000-C", "underlying": "BTC",
        "expiry_ns": expiry_ns if expiry_ns is not None else (clock.now_ns + 30 * DAY_NS),
        "strike": strike, "option_kind": option_kind, "side": side,
        "contracts": contracts, "premium": premium, "is_block": is_block,
    }


def test_moneyness_is_recorded_because_contracts_alone_make_a_lottery_ticket_a_position():
    clock = Clock()
    subject = an_options_reader(clock=clock)
    at_the_money = subject.read(an_options_row("o-1", 70_000.0, clock=clock))
    far_out = subject.read(an_options_row("o-2", 140_000.0, clock=clock))
    assert at_the_money.moneyness == AT_THE_MONEY
    assert far_out.moneyness == FAR_OUT_OF_THE_MONEY


def test_a_put_is_measured_from_the_other_side_of_spot():
    clock = Clock()
    subject = an_options_reader(clock=clock)
    result = subject.read(
        an_options_row("o-1", strike=35_000.0, option_kind=PUT, clock=clock)
    )
    assert result.moneyness == FAR_OUT_OF_THE_MONEY


def test_a_sold_option_is_an_obligation_not_an_opinion():
    clock = Clock()
    subject = an_options_reader(clock=clock)
    result = subject.read(an_options_row("o-1", side=SELL, clock=clock))
    assert result.is_an_obligation
    assert "obligation, not an opinion" in result.reason


def test_time_to_expiry_travels_with_the_trade():
    clock = Clock()
    subject = an_options_reader(clock=clock)
    near = subject.read(
        an_options_row("o-1", expiry_ns=clock.now_ns + 3 * DAY_NS, clock=clock)
    )
    far = subject.read(
        an_options_row("o-2", expiry_ns=clock.now_ns + 90 * DAY_NS, clock=clock)
    )
    assert near.days_to_expiry == pytest.approx(3.0, abs=0.01)
    assert far.days_to_expiry == pytest.approx(90.0, abs=0.01)


def test_an_expired_contract_describes_risk_that_no_longer_exists():
    clock = Clock()
    subject = an_options_reader(clock=clock)
    assert subject.read(
        an_options_row("o-1", expiry_ns=clock.now_ns - DAY_NS, clock=clock)
    ).state == EXPIRED


def test_small_premium_is_below_the_floor():
    clock = Clock()
    subject = an_options_reader(minimum=1_000_000.0, clock=clock)
    assert subject.read(an_options_row("o-1", premium=1_000.0, clock=clock)).state == TOO_SMALL


def test_the_reader_computes_no_put_call_ratio():
    described = importlib.import_module(
        BLOCK_PARTS["options-flow-reader"]
    ).describe_options_flow_reading(an_options_reader())
    assert described["computes_a_put_call_ratio"] is False
    assert described["says_what_the_flow_means"] is False


# ---- arxiv-feed-reader ------------------------------------------------------

class Gap:
    def __init__(self, gap_id="g-1", query="funding rate settlement microstructure",
                 worth=True):
        self.gap_id = gap_id
        self.query = query
        self.is_worth_fetching_against = worth


def a_feed_reader(fetches=5, window=60.0, overlap=2, monotonic=None):
    return ArxivFeedReader(
        fetches_per_window=fetches, window_seconds=window,
        minimum_word_overlap=overlap, monotonic=monotonic or TickingClock(),
        now_ns=Clock(),
    )


def a_paper(identifier="2512.15720", version=1, withdrawn=False,
            title="funding rate settlement microstructure in perpetual futures"):
    return {
        "identifier": identifier, "version": version, "is_withdrawn": withdrawn,
        "title": title, "abstract": title, "content": "the body of the paper",
    }


def test_nothing_is_fetched_without_a_gap():
    """A feed reader given a topic spends the budget on recency."""
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper()])
    assert subject.fetch_for(None).state == ARXIV_NO_GAP


def test_a_paper_without_a_versioned_identifier_is_refused():
    subject = a_feed_reader()
    paper = a_paper()
    paper["identifier"] = None
    subject.install_search(lambda query: [paper])
    assert subject.fetch_for(Gap()).state == NO_IDENTIFIER


def test_a_withdrawn_paper_is_recorded_as_withdrawn():
    """That is when anything distilled from it needs invalidating."""
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper(withdrawn=True)])
    assert subject.fetch_for(Gap()).state == WITHDRAWN
    assert subject.withdrawn_papers() == ("2512.15720",)


def test_a_revision_is_fetched_again_and_names_what_it_supersedes():
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper(version=1)])
    assert subject.fetch_for(Gap()).state == FETCHED
    subject.install_search(lambda query: [a_paper(version=3)])
    result = subject.fetch_for(Gap())
    assert result.state == REVISED
    assert result.supersedes == "arxiv:2512.15720v1"


def test_the_same_version_is_not_fetched_twice():
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper(version=2)])
    subject.fetch_for(Gap())
    assert subject.fetch_for(Gap()).state == ALREADY_HAVE_THIS_VERSION


def test_category_membership_is_not_relevance():
    subject = a_feed_reader(overlap=2)
    subject.install_search(
        lambda query: [a_paper(title="attention is all you need for image captioning")]
    )
    assert subject.fetch_for(Gap()).state == NOTHING_MATCHED


def test_a_gap_a_paper_would_not_close_is_not_fetched_against():
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper()])
    assert subject.fetch_for(Gap(worth=False)).state == NOT_WORTH_FETCHING


def test_a_preprint_is_carried_as_not_peer_reviewed():
    subject = a_feed_reader()
    subject.install_search(lambda query: [a_paper()])
    assert subject.fetch_for(Gap()).is_peer_reviewed is False


# ---- github-strategy-miner --------------------------------------------------

def a_miner(available=("kline", "aggtrade"), minimum_conditions=1):
    return GithubStrategyMiner(
        available_data_kinds=available, minimum_conditions=minimum_conditions,
        now_ns=Clock(),
    )


def an_idea(reference="github.com/someone/strategy"):
    return WebIdea(
        idea_id="i-1", summary="a public backtest", origin_reference=reference,
        origin_kind="repository", stars_or_reach=9_000, mentions_a_market=True,
        observed_at_ns=0,
    )


def a_mechanism(needs=("kline",), conditions=("the funding rate flips sign",)):
    return Mechanism(
        name="funding-flip", conditions=conditions, action="enter long",
        instruments=("BTCUSDT",), needs_data=needs,
    )


def test_the_defect_is_the_finding():
    """A repository with no fee model demonstrates how much of the return was fees."""
    subject = a_miner()
    mined = subject.mine(an_idea(), (), (NO_COST_MODEL, LOOKAHEAD))
    assert mined.state == ONLY_A_DEFECT
    assert len(mined.findings) == 2
    assert all(finding.is_testable_here for finding in mined.findings)


def test_a_mechanism_needing_data_this_system_lacks_is_not_testable():
    subject = a_miner(available=("kline",))
    mined = subject.mine(an_idea(), (a_mechanism(needs=("order-book-l3",)),), ())
    assert mined.state == NOTHING_FOUND


def test_a_testable_mechanism_becomes_a_low_confidence_finding():
    subject = a_miner()
    mined = subject.mine(an_idea(), (a_mechanism(),), (), stars=9_000)
    assert mined.state == MINED
    assert mined.findings[0].confidence < 0.5
    assert "counted for nothing" in mined.reason


def test_stars_never_rank_anything():
    described = importlib.import_module(
        BLOCK_PARTS["github-strategy-miner"]
    ).describe_strategy_mining(a_miner())
    assert described["ranks_by_stars"] is False
    assert described["times_stars_influenced_a_decision"] == 0


def test_an_unrecognised_defect_name_is_not_admitted():
    subject = a_miner()
    mined = subject.mine(an_idea(), (), ("looks-a-bit-sketchy",))
    assert mined.defects == ()
    assert set(KNOWN_DEFECTS) >= {NO_COST_MODEL, LOOKAHEAD}


def test_a_finding_names_what_would_refute_it():
    subject = a_miner()
    mined = subject.mine(an_idea(), (a_mechanism(),), ())
    assert "recorded data" in mined.findings[0].would_be_refuted_by


def test_a_gap_with_no_search_installed_is_refused_by_name_not_raised():
    """The part's own docstring promised a state and the code raised.

    On 2026-08-25 the first skill-gap to reach it took the part off the air, one
    minute after phase 14 started it. A reader with no search never tried, which
    is a different fact from a fetch that was tried and failed -- and the two need
    different fixes.
    """
    from parts.online_research.arxiv_feed_reader import NO_SEARCH, ArxivFeedReader

    class Gap:
        gap_id = "gap-1"
        query = "microstructure"
        is_worth_fetching_against = True

    reader = ArxivFeedReader(
        fetches_per_window=2, window_seconds=60.0, minimum_word_overlap=1,
    )
    read = reader.fetch_for(Gap())
    assert read.state == NO_SEARCH
    assert reader.standing.refused_no_search == 1
    assert "install_search" in read.reason
