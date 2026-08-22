"""The eight analysis parts of market-data-feed, on real captured messages."""

import pytest

from parts.market_data_feed.api_key_pool_rotator import ApiKeyPoolRotator
from parts.market_data_feed.ban_signal_detector import (
    BANNED, SERVING, THROTTLED, UNKNOWN, BanSignalDetector,
)
from parts.market_data_feed.cross_venue_price_consolidator import (
    CrossVenuePriceConsolidator, VenueQuote,
)
from parts.market_data_feed.feed_coverage_auditor import (
    COVERED, PARTIAL, UNCOVERED, FeedCoverageAuditor,
)
from parts.market_data_feed.feed_gap_detector import (
    SEQUENCE_BREAK, SILENCE, FeedGapDetector,
)
from parts.market_data_feed.feed_jump_detector import Candle, FeedJumpDetector
from parts.market_data_feed.tick_size_resolver import (
    DECLARED, INFERRED, UNKNOWN as TICK_UNKNOWN, TickSizeResolver,
)
from parts.market_data_feed.venue_pool_rotator import VenuePoolRotator
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.tape import StreamKind
from runtime.venues.adapter_registry import load_venue_adapter

BLOCK_PARTS = {
    "feed-gap-detector": "parts.market_data_feed.feed_gap_detector",
    "feed-jump-detector": "parts.market_data_feed.feed_jump_detector",
    "tick-size-resolver": "parts.market_data_feed.tick_size_resolver",
    "ban-signal-detector": "parts.market_data_feed.ban_signal_detector",
    "venue-pool-rotator": "parts.market_data_feed.venue_pool_rotator",
    "api-key-pool-rotator": "parts.market_data_feed.api_key_pool_rotator",
    "cross-venue-price-consolidator": "parts.market_data_feed.cross_venue_price_consolidator",
    "feed-coverage-auditor": "parts.market_data_feed.feed_coverage_auditor",
}

BOOK_FIXTURES = {
    "binance-usdm": "2026-08-22-public-ws-depth20.jsonl",
    "bybit-linear": "2026-08-22-public-linear-orderbook.jsonl",
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    import importlib

    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- feed-gap-detector -------------------------------------------------------

def facts_of(venue_id, payloads):
    adapter = load_venue_adapter(venue_id)
    return [f for p in payloads if (f := adapter.read_message_facts(p)) is not None]


def data_pairs(venue_id, payloads):
    """(payload, facts) together -- the control frames sit between them otherwise."""
    adapter = load_venue_adapter(venue_id)
    return [(p, f) for p in payloads if (f := adapter.read_message_facts(p)) is not None]


def test_an_unbroken_real_book_stream_reports_no_gap(read_captured_payloads):
    adapter = load_venue_adapter("bybit-linear")
    payloads = [p for _, p in read_captured_payloads("bybit-linear", BOOK_FIXTURES["bybit-linear"])]
    detector = FeedGapDetector(adapter, feed_gap_threshold_seconds=60.0)
    gaps = [detector.observe(f) for f in facts_of("bybit-linear", payloads)]
    assert [g for g in gaps if g] == []
    assert detector.standing.resyncs_seen == 1


def test_a_missing_book_message_is_reported_as_a_sequence_break(read_captured_payloads):
    """Bybit's u steps by one; skipping one real message must be caught."""
    adapter = load_venue_adapter("bybit-linear")
    payloads = [p for _, p in read_captured_payloads("bybit-linear", BOOK_FIXTURES["bybit-linear"])]
    every_message = facts_of("bybit-linear", payloads)
    with_a_hole = every_message[:3] + every_message[5:]

    detector = FeedGapDetector(adapter, feed_gap_threshold_seconds=60.0)
    gaps = [g for f in with_a_hole if (g := detector.observe(f))]
    assert len(gaps) == 1
    assert gaps[0].reason == SEQUENCE_BREAK
    assert gaps[0].observed_sequence - gaps[0].expected_sequence == 2


def test_a_resync_is_not_reported_as_a_gap(read_captured_payloads):
    adapter = load_venue_adapter("bybit-linear")
    payloads = [p for _, p in read_captured_payloads("bybit-linear", BOOK_FIXTURES["bybit-linear"])]
    every = facts_of("bybit-linear", payloads)
    detector = FeedGapDetector(adapter, feed_gap_threshold_seconds=60.0)
    detector.observe(every[1])
    reordered = every[0]
    assert reordered.resets_sequence is True
    assert detector.observe(reordered) is None


def test_silence_past_the_threshold_is_a_gap(read_captured_payloads):
    adapter = load_venue_adapter("binance-usdm")
    payloads = [p for _, p in read_captured_payloads("binance-usdm", BOOK_FIXTURES["binance-usdm"])]
    clock = Clock()
    detector = FeedGapDetector(adapter, feed_gap_threshold_seconds=60.0, monotonic=clock.monotonic)
    detector.observe(facts_of("binance-usdm", payloads)[0])
    assert detector.check_for_silence() == ()
    clock.now += 61
    gaps = detector.check_for_silence()
    assert len(gaps) == 1 and gaps[0].reason == SILENCE
    assert gaps[0].silent_for_seconds > 60


def test_a_binance_book_that_lies_about_its_predecessor_is_caught(read_captured_payloads):
    adapter = load_venue_adapter("binance-usdm")
    payloads = [p for _, p in read_captured_payloads("binance-usdm", BOOK_FIXTURES["binance-usdm"])]
    pairs = data_pairs("binance-usdm", payloads)
    detector = FeedGapDetector(adapter, feed_gap_threshold_seconds=60.0)
    detector.observe(pairs[0][1])
    # Message 2 chains to message 1, so pairing it with message 0 must break.
    assert detector.check_chained_predecessor(*pairs[2]) is not None
    assert detector.check_chained_predecessor(*pairs[1]) is None


# ---- feed-jump-detector ------------------------------------------------------

def candle(symbol, open_price, close_price, at_ns):
    return Candle("binance-usdm", symbol, at_ns, open_price, close_price, max(open_price, close_price), min(open_price, close_price))


def test_a_continuous_candle_sequence_reports_no_jump():
    detector = FeedJumpDetector(jump_threshold_increments=2.0, jump_threshold_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1)) is None
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.5, 101.0, 2)) is None


def test_a_gap_wider_than_two_ticks_is_a_jump():
    detector = FeedJumpDetector(jump_threshold_increments=2.0, jump_threshold_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1))
    jump = detector.observe_closed_candle(candle("BTCUSDT", 101.5, 101.6, 2))
    assert jump is not None
    assert jump.previous_close == 100.5 and jump.next_open == 101.5
    assert jump.gap_increments == pytest.approx(10.0)


def test_without_a_declared_increment_the_fraction_threshold_is_used():
    detector = FeedJumpDetector(jump_threshold_increments=2.0, jump_threshold_fraction=0.01)
    detector.observe_closed_candle(candle("ETHUSDT", 100.0, 100.0, 1))
    assert detector.observe_closed_candle(candle("ETHUSDT", 100.5, 100.5, 2)) is None
    assert detector.observe_closed_candle(candle("ETHUSDT", 105.0, 105.0, 3)) is not None


# ---- tick-size-resolver ------------------------------------------------------

def test_a_declared_increment_beats_an_inferred_one(read_captured_json):
    adapter = load_venue_adapter("binance-usdm")
    listings = adapter.read_symbol_listings(
        read_captured_json("binance-usdm", "2026-08-22-catalogue-subset.json")
    )
    declared = next(l for l in listings if l.price_increment)
    resolver = TickSizeResolver(minimum_observations=4)
    resolver.declare_from_catalogue("binance-usdm", declared.symbol, declared.price_increment)
    resolver.observe_book("binance-usdm", declared.symbol, [100.0, 99.0, 98.0], [101.0, 102.0, 103.0])
    result = resolver.resolve("binance-usdm", declared.symbol)
    assert result.source == DECLARED
    assert result.increment == declared.price_increment
    assert resolver.standing.disagreements == 1


def test_a_real_book_infers_an_increment_when_the_venue_declares_none(read_captured_payloads):
    import json

    payloads = [p for _, p in read_captured_payloads("binance-usdm", BOOK_FIXTURES["binance-usdm"])]
    resolver = TickSizeResolver(minimum_observations=4)
    for payload in payloads[:5]:
        message = json.loads(payload)
        if "b" not in message:
            continue
        resolver.observe_book(
            "binance-usdm", message["s"],
            [float(price) for price, _ in message["b"]],
            [float(price) for price, _ in message["a"]],
        )
    result = resolver.resolve("binance-usdm", "BTCUSDT")
    assert result.source == INFERRED
    assert result.increment == pytest.approx(0.1)


def test_too_few_observations_stay_unknown_rather_than_guessing():
    resolver = TickSizeResolver(minimum_observations=10)
    resolver.declare_from_catalogue("bybit-linear", "BTCUSDT", None)
    resolver.observe_book("bybit-linear", "BTCUSDT", [100.0, 99.9], [100.1, 100.2])
    assert resolver.resolve("bybit-linear", "BTCUSDT").source == TICK_UNKNOWN


# ---- ban-signal-detector -----------------------------------------------------

def detector_for(*venues, gaps=3):
    return BanSignalDetector(
        {v: load_venue_adapter(v) for v in venues}, gaps_before_withheld=gaps
    )


def test_nothing_observed_is_unknown_not_serving():
    assert detector_for("binance-usdm").read_standing("binance-usdm").state == UNKNOWN


def test_a_429_puts_a_venue_out_of_service():
    detector = detector_for("binance-usdm")
    assert detector.observe_http_response("binance-usdm", 429, {"Retry-After": "30"}) is not None
    assert detector.read_standing("binance-usdm").state == BANNED


def test_a_ban_with_no_stated_duration_stays_banned():
    detector = detector_for("binance-usdm")
    detector.observe_http_response("binance-usdm", 418, {})
    standing = detector.read_standing("binance-usdm")
    assert standing.state == BANNED and standing.banned_until_ns is None


def test_an_expired_ban_clears():
    times = iter([0, 0, 10**12, 10**12])
    detector = BanSignalDetector(
        {"binance-usdm": load_venue_adapter("binance-usdm")},
        gaps_before_withheld=3,
        now_ns=lambda: next(times),
    )
    detector.observe_http_response("binance-usdm", 429, {"Retry-After": "1"})
    detector.clear_expired_bans()
    assert detector.read_standing("binance-usdm").state != BANNED


def test_repeated_feed_gaps_throttle_a_venue_that_never_errored():
    detector = detector_for("bybit-linear", gaps=3)
    detector.observe_data("bybit-linear")
    for _ in range(3):
        detector.observe_feed_gap("bybit-linear")
    assert detector.read_standing("bybit-linear").state == THROTTLED


def test_data_arriving_clears_silence_but_not_a_ban():
    detector = detector_for("bybit-linear", gaps=1)
    detector.observe_feed_gap("bybit-linear")
    detector.observe_data("bybit-linear")
    assert detector.read_standing("bybit-linear").state == SERVING
    detector.observe_http_response("bybit-linear", 403, {})
    detector.observe_data("bybit-linear")
    assert detector.read_standing("bybit-linear").state == BANNED


def test_running_near_the_venues_stated_budget_throttles():
    detector = detector_for("binance-usdm")
    detector.observe_data("binance-usdm")
    detector.observe_rate_budget("binance-usdm", used=2300, limit=2400)
    assert detector.read_standing("binance-usdm").state == THROTTLED


# ---- venue-pool-rotator ------------------------------------------------------

def test_traffic_spreads_across_serving_venues():
    rotator = VenuePoolRotator()
    for venue in ("binance-usdm", "bybit-linear"):
        rotator.set_venue_standing(venue, SERVING)
        rotator.set_rate_headroom(venue, 1.0)
    rotator.set_symbol_venues("BTCUSDT", {"binance-usdm", "bybit-linear"})
    chosen = [rotator.route("depth", "BTCUSDT").venue_id for _ in range(10)]
    assert set(chosen) == {"binance-usdm", "bybit-linear"}
    assert abs(chosen.count("binance-usdm") - chosen.count("bybit-linear")) <= 1


def test_a_banned_venue_receives_nothing():
    rotator = VenuePoolRotator()
    rotator.set_venue_standing("binance-usdm", BANNED)
    rotator.set_venue_standing("bybit-linear", SERVING)
    rotator.set_rate_headroom("bybit-linear", 1.0)
    rotator.set_symbol_venues("BTCUSDT", {"binance-usdm", "bybit-linear"})
    assert {rotator.route("depth", "BTCUSDT").venue_id for _ in range(6)} == {"bybit-linear"}


def test_a_symbol_no_serving_venue_carries_routes_nowhere_and_says_so():
    rotator = VenuePoolRotator()
    rotator.set_venue_standing("binance-usdm", BANNED)
    rotator.set_symbol_venues("ONLYHERE", {"binance-usdm"})
    routing = rotator.route("depth", "ONLYHERE")
    assert routing.venue_id is None
    assert "binance-usdm" in routing.reason
    assert rotator.standing.unroutable == 1


def test_less_headroom_earns_less_traffic():
    rotator = VenuePoolRotator()
    rotator.set_venue_standing("binance-usdm", SERVING)
    rotator.set_venue_standing("bybit-linear", SERVING)
    rotator.set_rate_headroom("binance-usdm", 1.0)
    rotator.set_rate_headroom("bybit-linear", 0.1)
    rotator.set_symbol_venues("BTCUSDT", {"binance-usdm", "bybit-linear"})
    chosen = [rotator.route("depth", "BTCUSDT").venue_id for _ in range(22)]
    assert chosen.count("binance-usdm") > chosen.count("bybit-linear")


# ---- api-key-pool-rotator ----------------------------------------------------

def test_an_empty_key_pool_routes_nowhere_rather_than_pretending():
    """Phase 1 holds no keys. The logic is real; the set is honestly empty."""
    rotator = ApiKeyPoolRotator(rejection_rest_seconds=60.0)
    assert rotator.next_key("binance-usdm") is None
    assert rotator.standing.keys_registered == 0
    assert rotator.standing.calls_unroutable == 1


def test_calls_spread_across_keys_and_a_rejected_key_rests():
    times = iter([0] * 40)
    rotator = ApiKeyPoolRotator(rejection_rest_seconds=60.0, now_ns=lambda: next(times, 0))
    for key in ("key-a", "key-b"):
        rotator.register_key("binance-usdm", key)
    assert {rotator.next_key("binance-usdm") for _ in range(4)} == {"key-a", "key-b"}
    rotator.record_rejection("binance-usdm", "key-a", "signature rejected")
    assert {rotator.next_key("binance-usdm") for _ in range(3)} == {"key-b"}


def test_a_banned_venue_takes_no_key():
    rotator = ApiKeyPoolRotator(rejection_rest_seconds=60.0)
    rotator.register_key("binance-usdm", "key-a")
    rotator.set_venue_standing("binance-usdm", "banned")
    assert rotator.next_key("binance-usdm") is None


def test_no_secret_ever_appears_in_a_standing():
    rotator = ApiKeyPoolRotator(rejection_rest_seconds=60.0)
    rotator.register_key("binance-usdm", "key-a")
    standing = rotator.read_standings()[0]
    assert set(standing.__dict__) == {
        "venue_id", "key_id", "state", "reason", "calls_made", "rejections", "observed_at_ns"
    }


# ---- cross-venue-price-consolidator -----------------------------------------

def test_a_thin_venue_moves_the_price_less_than_a_deep_one():
    now = 10**18
    consolidator = CrossVenuePriceConsolidator(now_ns=lambda: now)
    consolidator.observe_quote(VenueQuote("binance-usdm", "BTCUSDT", 100.0, 90.0, now))
    consolidator.observe_quote(VenueQuote("bybit-linear", "BTCUSDT", 200.0, 10.0, now))
    price = consolidator.consolidate("BTCUSDT")
    assert price.price == pytest.approx(110.0)
    assert price.weights["binance-usdm"] == pytest.approx(0.9)
    assert price.oldest_contribution_seconds == pytest.approx(0.0)


def test_a_stale_quote_is_excluded_and_named():
    now = 10**18
    consolidator = CrossVenuePriceConsolidator(maximum_quote_age_seconds=5.0, now_ns=lambda: now)
    consolidator.observe_quote(VenueQuote("binance-usdm", "BTCUSDT", 100.0, 1.0, now))
    consolidator.observe_quote(VenueQuote("bybit-linear", "BTCUSDT", 999.0, 1.0, now - 6 * 10**9))
    price = consolidator.consolidate("BTCUSDT")
    assert price.price == pytest.approx(100.0)
    assert price.excluded_venues == ("bybit-linear",)


def test_a_banned_venue_does_not_contribute():
    now = 10**18
    consolidator = CrossVenuePriceConsolidator(now_ns=lambda: now)
    consolidator.set_venue_standing("bybit-linear", BANNED)
    consolidator.observe_quote(VenueQuote("bybit-linear", "BTCUSDT", 999.0, 1.0, now))
    assert consolidator.consolidate("BTCUSDT").price is None


def test_no_usable_quote_reports_no_price_rather_than_the_last_one():
    now = 10**18
    consolidator = CrossVenuePriceConsolidator(maximum_quote_age_seconds=1.0, now_ns=lambda: now)
    consolidator.observe_quote(VenueQuote("binance-usdm", "BTCUSDT", 100.0, 1.0, now - 10**10))
    price = consolidator.consolidate("BTCUSDT")
    assert price.price is None
    assert "fresh" in price.reason


# ---- feed-coverage-auditor ---------------------------------------------------

def auditor_for(*symbols, streams=(StreamKind.TRADE, StreamKind.CANDLE, StreamKind.BOOK)):
    clock = Clock()
    auditor = FeedCoverageAuditor(expected_streams=streams, monotonic=clock.monotonic)
    for symbol in symbols:
        auditor.expect_symbol(symbol, "binance-usdm")
    return auditor, clock


def test_a_symbol_nothing_covers_is_reported_as_uncovered():
    """The Rule 8 case: absent from the feed, present in the report."""
    auditor, _ = auditor_for("NOBODYSENDSTHIS")
    reports = auditor.audit_all()
    assert len(reports) == 1
    assert reports[0].state == UNCOVERED
    assert reports[0].missing_streams == ("TRADE", "CANDLE", "BOOK")
    assert auditor.standing.uncovered == 1


def test_a_fully_supplied_symbol_is_covered():
    auditor, _ = auditor_for("BTCUSDT")
    for kind in (StreamKind.TRADE, StreamKind.CANDLE, StreamKind.BOOK):
        auditor.observe("binance-usdm", "BTCUSDT", kind)
    assert auditor.audit_symbol("BTCUSDT").state == COVERED


def test_a_symbol_missing_one_stream_is_partial_not_covered():
    auditor, _ = auditor_for("BTCUSDT")
    auditor.observe("binance-usdm", "BTCUSDT", StreamKind.TRADE)
    report = auditor.audit_symbol("BTCUSDT")
    assert report.state == PARTIAL
    assert report.missing_streams == ("CANDLE", "BOOK")


def test_coverage_lapses_when_a_stream_goes_quiet_past_the_window():
    auditor, clock = auditor_for("BTCUSDT", streams=(StreamKind.TRADE,))
    auditor.observe("binance-usdm", "BTCUSDT", StreamKind.TRADE)
    assert auditor.audit_symbol("BTCUSDT").state == COVERED
    clock.now += 301
    report = auditor.audit_symbol("BTCUSDT")
    assert report.state == UNCOVERED
    assert report.silent_venues == ("binance-usdm",)
