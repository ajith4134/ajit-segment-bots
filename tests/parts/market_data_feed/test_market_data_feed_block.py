"""The eight analysis parts of market-data-feed, on real captured messages."""

import pathlib

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


def test_a_continuous_candle_sequence_says_so_rather_than_saying_nothing():
    """The first bar cannot be judged; the second is judged and found continuous.

    None means no comparison was possible, not that the symbol is fine -- the
    two were the same answer before 2026-09-04 and that is exactly why a symbol
    barred from filling could never be released.
    """
    detector = FeedJumpDetector(jump_threshold_increments=2.0, warmup_floor_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1)) is None
    continuous = detector.observe_closed_candle(candle("BTCUSDT", 100.5, 101.0, 2))
    assert continuous is not None and continuous.is_continuous
    assert detector.standing.jumps_found == 0


def test_a_symbol_that_jumps_and_then_settles_is_reported_continuous_again():
    """The release the fill path needs: a break, then the break being over."""
    detector = FeedJumpDetector(jump_threshold_increments=2.0, warmup_floor_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1))

    broke = detector.observe_closed_candle(candle("BTCUSDT", 102.5, 102.6, 2))  # 2% past a 1% warm-up floor
    assert broke is not None and not broke.is_continuous
    assert detector.standing.symbols_discontinuous_now == 1

    settled = detector.observe_closed_candle(candle("BTCUSDT", 102.6, 102.7, 3))
    assert settled is not None and settled.is_continuous
    assert detector.standing.continuity_restored == 1
    assert detector.standing.symbols_discontinuous_now == 0


def test_a_symbol_is_judged_against_its_own_moves_once_it_has_shown_enough():
    """An NSE option premium moves percent-scale a bar and that is not a jump.

    Measured on the 2026-09-04 Upstox I1 tape, NSE_FO contracts move p50 0.208%
    / p99 5.69% close-to-open, against a 0.5% threshold fitted to crypto
    perpetuals -- 38.1% of ordinary option bars were called a feed artefact.
    """
    detector = FeedJumpDetector(
        jump_threshold_increments=2.0,
        warmup_floor_fraction=0.01,
        patience_multiple=2.8,
        moves_needed=8,
    )
    price = 100.0
    # Each bar opens 0.8% above the previous close -- what this part measures is
    # the gap between bars, not the move within one. Eight such gaps, each under
    # the 1% warm-up floor, so each is a move the symbol was allowed to make.
    for step in range(9):
        price = price * 1.008
        detector.observe_closed_candle(candle("NIFTY-CE", price, price, step))
    assert detector.standing.symbols_with_a_measured_rhythm == 1

    # 2% is twice the warm-up floor and would have been a jump then; it is inside
    # 2.8x this symbol's own p99 of 0.8%, so it is the market and not a discontinuity.
    ordinary = detector.observe_closed_candle(candle("NIFTY-CE", price * 1.02, price * 1.02, 99))
    assert ordinary is not None and ordinary.is_continuous
    assert ordinary.bound_is_the_symbols_own
    assert detector.standing.checks_inside_a_widened_bound > 0


def test_the_warm_up_floor_does_not_outlive_warm_up():
    """The defect of 2026-09-13: one floor sized for an option's warm-up blinded the check on an index.

    A symbol that moves a hundredth of a percent a bar -- an index's rhythm, with no
    declared tick -- is judged against its own p99 once measured, so a move twenty times
    its ordinary one is a jump even though it sits far under the warm-up floor.
    """
    detector = FeedJumpDetector(
        jump_threshold_increments=2.0, warmup_floor_fraction=0.05, patience_multiple=2.8, moves_needed=8,
    )
    price = 23_600.0
    for step in range(9):
        price = price * 1.0001
        detector.observe_closed_candle(candle("NIFTY", price, price, step))
    broke = detector.observe_closed_candle(candle("NIFTY", price * 1.002, price * 1.002, 99))
    assert broke is not None and not broke.is_continuous, "a 0.2% index gap is 20 ordinary bars"
    assert broke.bound_fraction < 0.05


def test_a_break_is_not_remembered_as_one_of_the_symbols_own_moves():
    """Otherwise a feed that gaps repeatedly widens its own bound until nothing
    can ever be a jump again."""
    detector = FeedJumpDetector(
        jump_threshold_increments=2.0,
        warmup_floor_fraction=0.01,
        patience_multiple=2.8,
        moves_needed=2,
    )
    detector.observe_closed_candle(candle("NIFTY-CE", 100.0, 100.0, 1))
    detector.observe_closed_candle(candle("NIFTY-CE", 200.0, 200.0, 2))  # a 100% break
    detector.observe_closed_candle(candle("NIFTY-CE", 400.0, 400.0, 3))  # another
    assert detector.standing.jumps_found == 2
    assert detector.standing.symbols_with_a_measured_rhythm == 0


def test_the_ticks_floor_is_permanent_so_a_flat_contract_is_not_flagged_on_a_tick():
    """After flat bars the symbol's own p99 is zero; a move of a tick or two is still the market."""
    detector = FeedJumpDetector(
        jump_threshold_increments=2.0, warmup_floor_fraction=0.05, patience_multiple=2.8, moves_needed=8,
    )
    detector.set_price_increment("binance-usdm", "ITC 265 PE", 0.05)
    for step in range(9):
        detector.observe_closed_candle(candle("ITC 265 PE", 5.0, 5.0, step))
    one_tick = detector.observe_closed_candle(candle("ITC 265 PE", 5.05, 5.05, 20))
    assert one_tick is not None and one_tick.is_continuous
    five_ticks = detector.observe_closed_candle(candle("ITC 265 PE", 5.30, 5.30, 21))
    assert five_ticks is not None and not five_ticks.is_continuous
    assert five_ticks.gap_increments == pytest.approx(5.0)


def test_a_flat_symbol_with_no_tick_stays_on_the_warm_up_floor_rather_than_a_bound_of_zero():
    """A zero bound would flag every real move, and a flagged move is never remembered."""
    detector = FeedJumpDetector(
        jump_threshold_increments=2.0, warmup_floor_fraction=0.05, patience_multiple=2.8, moves_needed=8,
    )
    for step in range(9):
        detector.observe_closed_candle(candle("QUIET", 100.0, 100.0, step))
    moved = detector.observe_closed_candle(candle("QUIET", 101.0, 101.0, 20))
    assert moved is not None and moved.is_continuous and not moved.bound_is_the_symbols_own


def test_without_a_declared_increment_the_warm_up_floor_is_used():
    detector = FeedJumpDetector(jump_threshold_increments=2.0, warmup_floor_fraction=0.01)
    detector.observe_closed_candle(candle("ETHUSDT", 100.0, 100.0, 1))
    inside = detector.observe_closed_candle(candle("ETHUSDT", 100.5, 100.5, 2))
    assert inside is not None and inside.is_continuous
    assert inside.gap_increments is None  # inferred, never mistaken for declared
    outside = detector.observe_closed_candle(candle("ETHUSDT", 105.0, 105.0, 3))
    assert outside is not None and not outside.is_continuous


def test_on_real_upstox_bars_ordinary_contract_bars_are_rarely_flagged():
    """The real I1 bars of the newest trading day on the tape, with Upstox's own declared ticks.

    Under the rule this replaced, 24.07% of ordinary option bars were flagged on
    2026-09-07/08 and paper-fill-simulator refuses to fill a flagged symbol. The live
    settings are used, read from the example file this repository ships.
    """
    import json as _json
    import tomllib

    from runtime.tape import read_payload, read_tape_index
    from tests.conftest import UPSTOX_VENUE, upstox_days_newest_first, upstox_listings_by_key

    settings = tomllib.loads((pathlib.Path(__file__).resolve().parents[3] / "settings/runtime.example.toml").read_text())
    number = lambda name: float(settings[name]["value"])
    listings = upstox_listings_by_key()
    tape = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape" / UPSTOX_VENUE

    def bars(directory, day):
        index_path = directory / f"{day}.candle.index"
        if not index_path.exists():
            return []
        by_time = {}
        for record in read_tape_index(index_path):
            payload = _json.loads(read_payload(directory / f"{day}.candle.blob", record))
            if payload.get("interval") == "I1" and payload.get("open") and payload.get("close"):
                by_time[int(payload["bar_time_ms"])] = payload
        return [by_time[t] for t in sorted(by_time)]

    for day in upstox_days_newest_first():
        contracts = [d for d in tape.glob("NSE_FO*") if (d / f"{day}.candle.index").exists()][:300]
        series = {d.name: bars(d, day) for d in contracts}
        if sum(len(b) for b in series.values()) >= 20_000:
            break
    else:
        pytest.skip("the tape holds no trading day with 20,000 option bars")

    detector = FeedJumpDetector(
        jump_threshold_increments=number("feed_jump_threshold_increments"),
        warmup_floor_fraction=number("feed_jump_warmup_floor_fraction"),
        patience_multiple=number("feed_jump_patience_multiple"),
        moves_needed=int(number("feed_jump_moves_needed")),
        moves_remembered=int(number("feed_jump_moves_remembered")),
    )
    judged = flagged = 0
    for key, symbol_bars in series.items():
        listing = listings.get(key)
        if listing is not None and listing.tick_size:
            detector.set_price_increment(UPSTOX_VENUE, key, listing.tick_size)
        for bar in symbol_bars:
            verdict = detector.observe_closed_candle(Candle(
                UPSTOX_VENUE, key, int(bar["bar_time_ms"]) * 1_000_000,
                bar["open"], bar["close"], bar["high"], bar["low"],
            ))
            if verdict is not None:
                judged += 1
                flagged += not verdict.is_continuous
    assert judged >= 10_000
    assert flagged / judged < 0.03, f"{flagged} of {judged} ordinary option bars flagged on {day}"


def test_a_closed_bar_restated_by_the_feed_is_judged_once():
    """Upstox restates the bar it just closed with every message until the next one closes.

    broker-candle-bridge marks every one of those restatements closed, so the part
    compared a bar's open against its own close. Measured live on 2026-09-15 over
    300 contracts: 17,305 closed-marked records for 1,523 distinct bars, and 15,782
    of 17,021 comparisons were a bar against itself.
    """
    detector = FeedJumpDetector(jump_threshold_increments=2.0, warmup_floor_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1))
    # Opens 3% below its own close: a restatement of that bar is not a boundary.
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.5, 103.5, 2)).is_continuous
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.5, 103.5, 2)) is None
    assert detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1)) is None
    assert detector.standing.jumps_found == 0
    assert detector.standing.restated_bars_ignored == 2
    following = detector.observe_closed_candle(candle("BTCUSDT", 103.5, 103.6, 3))
    assert following is not None and following.is_continuous


def test_on_real_upstox_bars_as_the_bridge_delivers_them_the_verdicts_equal_one_bar_one_judgement():
    """Today's I1 records in arrival order, every one the bridge would mark closed.

    The rule was fitted on one record per bar, which is not what the live part
    receives: on 2026-09-15 it flagged 10.8% of live candles against the ~1% that
    replay predicted, and paper-fill-simulator had refused 383 fills on those flags
    inside twenty minutes. Fed the restated stream, the part must reach exactly
    the verdicts it reaches on each bar once.
    """
    import json as _json
    import tomllib

    from runtime.tape import read_payload, read_tape_index
    from tests.conftest import UPSTOX_VENUE, upstox_days_newest_first, upstox_listings_by_key

    settings = tomllib.loads((pathlib.Path(__file__).resolve().parents[3] / "settings/runtime.example.toml").read_text())
    number = lambda name: float(settings[name]["value"])
    listings = upstox_listings_by_key()
    tape = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape" / UPSTOX_VENUE
    one_minute_ns = 60_000_000_000

    def closed_records(directory, day):
        index_path = directory / f"{day}.candle.index"
        if not index_path.exists():
            return []
        stream = []
        for record in read_tape_index(index_path):
            payload = _json.loads(read_payload(directory / f"{day}.candle.blob", record))
            if payload.get("interval") != "I1" or not payload.get("open") or not payload.get("close"):
                continue
            opened_ns = int(payload["bar_time_ms"]) * 1_000_000
            if int(record["received_at_ns"]) >= opened_ns + one_minute_ns:
                stream.append(payload)
        return stream

    for day in upstox_days_newest_first():
        contracts = [d for d in tape.glob("NSE_FO*") if (d / f"{day}.candle.index").exists()][:300]
        series = {d.name: closed_records(d, day) for d in contracts}
        distinct = sum(len({p["bar_time_ms"] for p in s}) for s in series.values())
        if distinct >= 1_000 and sum(map(len, series.values())) >= 2 * distinct:
            break
    else:
        pytest.skip("the tape holds no trading day whose closed bars arrive restated")

    def detector():
        return FeedJumpDetector(
            jump_threshold_increments=number("feed_jump_threshold_increments"),
            warmup_floor_fraction=number("feed_jump_warmup_floor_fraction"),
            patience_multiple=number("feed_jump_patience_multiple"),
            moves_needed=int(number("feed_jump_moves_needed")),
            moves_remembered=int(number("feed_jump_moves_remembered")),
        )

    def verdicts(part, key, bars):
        found = []
        for bar in bars:
            verdict = part.observe_closed_candle(Candle(
                UPSTOX_VENUE, key, int(bar["bar_time_ms"]) * 1_000_000,
                bar["open"], bar["close"], bar["high"], bar["low"],
            ))
            if verdict is not None:
                found.append((int(bar["bar_time_ms"]), verdict.is_continuous))
        return found

    as_delivered, once_per_bar = detector(), detector()
    delivered_verdicts = once_verdicts = 0
    for key, stream in series.items():
        listing = listings.get(key)
        for part in (as_delivered, once_per_bar):
            if listing is not None and listing.tick_size:
                part.set_price_increment(UPSTOX_VENUE, key, listing.tick_size)
        first_of_each_bar = list({int(p["bar_time_ms"]): p for p in reversed(stream)}.values())[::-1]
        live = verdicts(as_delivered, key, stream)
        once = verdicts(once_per_bar, key, first_of_each_bar)
        assert live == once, f"{key} on {day}: restated delivery changed the verdicts"
        delivered_verdicts += len(live)
        once_verdicts += len(once)
    assert delivered_verdicts == once_verdicts >= 1_000
    assert as_delivered.standing.restated_bars_ignored >= distinct


# ---- tick-size-resolver ------------------------------------------------------

def test_an_unchanged_tick_size_is_not_restated_on_every_tick(read_captured_json):
    """13% of the spine, measured 2026-09-04.

    This part published `resolve_all()` unconditionally on every tick -- one
    increment for every symbol it had ever seen a book for -- at 1,204 messages a
    second, for 1,156 symbols of which 1,006 resolved to UNKNOWN.

    The skip count is asserted rather than the absence of a crash: a `PriceIncrement`
    carries `resolved_at_ns` and an `observations` count that climbs with every
    book, so compared whole no two are ever equal and a change check would skip
    nothing while appearing to work.
    """
    from parts.market_data_feed.tick_size_resolver import increment_verdict_of
    from runtime.level_publishing import LevelPublisherByKey

    sent = []
    levels = LevelPublisherByKey(
        publish=lambda items: sent.extend(items),
        refresh_interval_seconds=1_000_000.0,
        identity_of=increment_verdict_of,
    )
    resolver = TickSizeResolver(minimum_observations=4)
    adapter = load_venue_adapter("binance-usdm")
    listings = adapter.read_symbol_listings(
        read_captured_json("binance-usdm", "2026-08-22-catalogue-subset.json")
    )
    declared = [listing for listing in listings if listing.price_increment][:20]
    assert declared, "the captured catalogue declares no increments"
    for listing in declared:
        resolver.declare_from_catalogue("binance-usdm", listing.symbol, listing.price_increment)

    first = resolver.resolve_all()
    assert first, "the catalogue produced no increments to restate"
    for increment in first:
        levels.publish_level((increment.venue_id, increment.symbol), (increment,))
    published = len(sent)
    assert published == len(first)

    # Nothing about a tick size has changed; resolving again must send nothing.
    for _ in range(30):
        for increment in resolver.resolve_all():
            levels.publish_level((increment.venue_id, increment.symbol), (increment,))

    assert len(sent) == published
    assert levels.standing.unchanged_publishes_skipped == 30 * len(first)


def test_a_tick_size_that_actually_changes_is_published_at_once(read_captured_json):
    """Stable is not silent: the check must not swallow a real change."""
    from parts.market_data_feed.tick_size_resolver import increment_verdict_of
    from runtime.level_publishing import LevelPublisherByKey

    sent = []
    levels = LevelPublisherByKey(
        publish=lambda items: sent.extend(items),
        refresh_interval_seconds=1_000_000.0,
        identity_of=increment_verdict_of,
    )
    resolver = TickSizeResolver(minimum_observations=4)
    resolver.declare_from_catalogue("binance-usdm", "BTCUSDT", 0.10)
    for increment in resolver.resolve_all():
        levels.publish_level((increment.venue_id, increment.symbol), (increment,))
    assert len(sent) == 1

    resolver.declare_from_catalogue("binance-usdm", "BTCUSDT", 0.01)
    for increment in resolver.resolve_all():
        levels.publish_level((increment.venue_id, increment.symbol), (increment,))

    assert len(sent) == 2
    assert sent[-1].increment == 0.01


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


def test_a_book_level_is_a_price_and_a_quantity():
    """BookUpdate carries `(price, quantity)` pairs, and this read bare prices.

    The first real book to reach it on 2026-08-25 -- the day anything planned a
    book stream at all -- took the part off the air subtracting one tuple from
    another. The quantity is dropped on purpose: a tick size is about where
    prices may sit, and a level with no size sits on a tick like any other.
    """
    from runtime.venues.venue_adapter import BookUpdate

    resolver = TickSizeResolver(minimum_observations=2)
    book = BookUpdate(
        venue_id="binance-usdm", symbol="BTCUSDT",
        bids=((100.00, 1.0), (99.99, 2.0), (99.98, 3.0)),
        asks=((100.01, 1.0), (100.02, 2.0)),
        is_snapshot=True, venue_time_ns=1, sequence=1,
    )
    resolver.observe_book(book.venue_id, book.symbol, book.bids, book.asks)
    assert resolver.resolve("binance-usdm", "BTCUSDT").increment == pytest.approx(0.01)


def test_a_flag_is_restated_between_bars_so_a_restarted_fill_path_learns_it_again():
    """Judging a bar once removed the only thing that kept a level current between bars."""
    from parts.market_data_feed.feed_jump_detector import ContinuityLevels, continuity_of
    from runtime.level_publishing import LevelPublisherByKey

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock, said = Clock(), []
    publisher = LevelPublisherByKey(
        publish=lambda items: said.extend(items), refresh_interval_seconds=60.0,
        monotonic=clock, identity_of=continuity_of,
    )
    levels = ContinuityLevels(publisher, sweep_interval_seconds=1.0, monotonic=clock)
    detector = FeedJumpDetector(jump_threshold_increments=2.0, warmup_floor_fraction=0.01)
    detector.set_price_increment("binance-usdm", "BTCUSDT", 0.1)
    detector.observe_closed_candle(candle("BTCUSDT", 100.0, 100.5, 1))
    levels.publish(detector.observe_closed_candle(candle("BTCUSDT", 102.5, 102.6, 2)))
    assert [j.is_continuous for j in said] == [False]

    clock.now = 30.0
    levels.restate_what_is_due()
    assert len(said) == 1, "not due yet: nothing is said more often than the refresh interval"
    clock.now = 61.0
    levels.restate_what_is_due()
    assert [j.is_continuous for j in said] == [False, False]
