"""The Bybit linear adapter, against what Bybit actually sent.

Captured live on 2026-08-22 into `tests/captured/bybit-linear/`, provenance in
`capture-manifest.json`. The two claims worth capturing for are the ones the
documentation does not make: that the book's update id steps by exactly one
between consecutive deltas, and that the public market-data REST endpoints send
no `X-Bapi-Limit` headers at all. Both were open questions in the research and
both are answered here by measurement rather than by reading.
"""

import json

import pytest

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.venues.bybit_linear import (
    LINEAR_PUBLIC_STREAM,
    VENUE_ID,
    build_venue_adapter,
)
from runtime.venues.venue_adapter import (
    SequenceContinuity,
    StreamRequest,
    VenueMessageNotRecognised,
)

TRADE_FIXTURE = "2026-08-22-public-linear-trade.jsonl"
CANDLE_FIXTURE = "2026-08-22-public-linear-kline-through-close.jsonl"
BOOK_FIXTURE = "2026-08-22-public-linear-orderbook.jsonl"
CATALOGUE_FIXTURE = "2026-08-22-instruments-info-subset.json"

CAPTURED_SYMBOL = "BTCUSDT"
CANDLE_INTERVAL = "1m"
# The operator's book_depth_levels default. This venue's ladder starts 1, 50,
# so 20 lands on 50 -- the mapping is the thing under test.
REQUESTED_BOOK_DEPTH = 20
SERVED_BOOK_DEPTH = 50


@pytest.fixture
def adapter():
    return build_venue_adapter()


def data_facts(adapter, records):
    """Every captured message's facts, with the control frames dropped."""
    return [
        (payload, adapter.read_message_facts(payload))
        for _, payload in records
        if adapter.read_message_facts(payload) is not None
    ]


def test_one_public_endpoint_carries_every_stream_kind(adapter):
    for stream_kind in StreamKind:
        assert adapter.stream_endpoint_url(stream_kind) == LINEAR_PUBLIC_STREAM


def test_trades_are_declared_every_print_unlike_the_other_venue(adapter):
    """The fidelity difference §7 refuses to flatten."""
    assert adapter.trade_fidelity is TradeFidelity.EVERY_PRINT


def test_topics_are_the_venue_s_own_phrasing_including_its_interval_spelling(adapter):
    """A part asks for `1m`; this venue calls that `1`, and the translation lives here."""
    assert (
        adapter.subscription_topic(StreamRequest(StreamKind.TRADE, CAPTURED_SYMBOL))
        == "publicTrade.BTCUSDT"
    )
    assert (
        adapter.subscription_topic(
            StreamRequest(StreamKind.CANDLE, CAPTURED_SYMBOL, candle_interval=CANDLE_INTERVAL)
        )
        == "kline.1.BTCUSDT"
    )
    assert (
        adapter.subscription_topic(
            StreamRequest(StreamKind.BOOK, CAPTURED_SYMBOL, book_depth_levels=REQUESTED_BOOK_DEPTH)
        )
        == f"orderbook.{SERVED_BOOK_DEPTH}.BTCUSDT"
    )


def test_an_interval_this_venue_does_not_offer_is_refused_by_name(adapter):
    with pytest.raises(ValueError) as refusal:
        adapter.resolve_candle_interval("7m")
    assert "7m" in str(refusal.value)
    with pytest.raises(ValueError):
        adapter.resolve_candle_interval(None)


def test_book_depth_rounds_up_to_this_venue_s_ladder(adapter):
    """1, 50, 200, 1000 -- so the shipped 20 is served at 50, never at 1."""
    assert adapter.resolve_book_depth_levels(1) == 1
    assert adapter.resolve_book_depth_levels(REQUESTED_BOOK_DEPTH) == SERVED_BOOK_DEPTH
    assert adapter.resolve_book_depth_levels(200) == 200
    with pytest.raises(ValueError):
        adapter.resolve_book_depth_levels(2000)


def test_fit_is_a_character_count_here_and_it_scales_with_symbol_length(adapter):
    """The cap is 21,000 characters of serialised args, not a topic count.

    Two sets of the same size fit differently depending on how long the symbol
    names are, which is exactly the arithmetic a caller must never attempt.
    """
    cap = adapter.declared_limits()["subscribe_args_characters"].value
    short = [f"publicTrade.A{index}USDT" for index in range(600)]
    long_names = [f"publicTrade.AVERYLONGSYMBOLNAME{index}USDT" for index in range(600)]
    assert len(json.dumps(short, separators=(",", ":"))) < cap
    assert adapter.does_topic_fit_connection(short, "publicTrade.BTCUSDT") is True

    filled = []
    for topic in long_names:
        if not adapter.does_topic_fit_connection(filled, topic):
            break
        filled.append(topic)
    assert len(filled) < len(long_names), "the long-name set never hit the character cap"
    assert len(json.dumps(filled, separators=(",", ":"))) <= cap
    # Same count, different lengths, different answer -- the point of asking.
    assert len(filled) < len(short)
    # A topic already subscribed always fits: resubscribing adds no characters.
    assert adapter.does_topic_fit_connection(filled, filled[0]) is True


def test_the_subscribe_frame_is_the_frame_the_capture_used(adapter, capture_manifest):
    topics = ["publicTrade.BTCUSDT"]
    frame = json.loads(adapter.subscribe_frame(topics))
    assert frame["op"] == "subscribe"
    assert frame["args"] == topics
    assert json.loads(adapter.unsubscribe_frame(topics))["op"] == "unsubscribe"
    subscribed = {
        tuple(capture["subscribed"])
        for capture in capture_manifest["captures"]
        if capture["venue"] == VENUE_ID and "subscribed" in capture
    }
    assert tuple(topics) in subscribed


def test_the_client_pings_here_and_the_interval_is_the_venue_s(adapter):
    """Binance pings us; this venue closes a connection that stops pinging it."""
    discipline = adapter.heartbeat_discipline()
    assert discipline.expects_client_ping is True
    assert discipline.interval_seconds == 20.0
    assert json.loads(discipline.ping_frame) == {"op": "ping"}


def test_the_subscribe_acknowledgement_is_not_a_tape_record(adapter, read_captured_payloads):
    """A control frame here names no topic -- that is the whole distinction."""
    records = read_captured_payloads("bybit-linear", TRADE_FIXTURE)
    controls = [payload for _, payload in records if adapter.read_message_facts(payload) is None]
    assert controls, "the capture kept no acknowledgement, so this case is untested"
    acknowledgement = json.loads(controls[0])
    assert acknowledgement["op"] == "subscribe" and acknowledgement["success"] is True
    assert "topic" not in acknowledgement


def test_every_captured_trade_reads_as_a_trade_with_a_non_decreasing_sequence(
    adapter, read_captured_payloads
):
    """`seq` may repeat across messages, so the check is direction, not step size."""
    records = read_captured_payloads("bybit-linear", TRADE_FIXTURE)
    trades = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.TRADE
    ]
    assert len(trades) > 1

    for payload, facts in trades:
        message = json.loads(payload)
        assert facts.symbol == CAPTURED_SYMBOL
        assert facts.venue_time_ns == int(message["ts"]) * 1_000_000
        assert facts.sequence == max(int(trade["seq"]) for trade in message["data"])
        assert facts.is_closed_candle is None
        assert facts.resets_sequence is False

    sequences = [facts.sequence for _, facts in trades]
    assert sequences == sorted(sequences), "cross sequences went backwards in the real capture"
    assert adapter.sequence_continuity(StreamKind.TRADE) is SequenceContinuity.NON_DECREASING


def test_a_trade_message_can_batch_several_prints(adapter, read_captured_payloads):
    """Every print, batched -- so one tape record is not one trade, and that is recorded.

    The batching is why the message's own `ts` is the index time: with up to 1024
    trades in a message, no single trade's timestamp describes the record.
    """
    records = read_captured_payloads("bybit-linear", TRADE_FIXTURE)
    batch_sizes = [
        len(json.loads(payload)["data"])
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.TRADE
    ]
    assert max(batch_sizes) > 1, "no captured message batched more than one trade"


def test_a_real_closed_candle_reads_as_closed(adapter, read_captured_payloads):
    """`confirm: true`, against a minute that actually ended during the capture."""
    records = read_captured_payloads("bybit-linear", CANDLE_FIXTURE)
    candles = [
        facts
        for _, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.CANDLE
    ]
    assert sum(1 for facts in candles if facts.is_closed_candle) == 1
    assert any(facts.is_closed_candle is False for facts in candles)
    assert all(facts.sequence == NOT_SENT for facts in candles)
    assert adapter.sequence_continuity(StreamKind.CANDLE) is SequenceContinuity.NOT_NUMBERED


def test_the_book_update_id_steps_by_one_which_the_docs_never_say(
    adapter, read_captured_payloads
):
    """The measurement the whole §6 check rests on.

    Bybit documents no client-side continuity rule and its own reference SDK,
    pybit, stores `u` without ever comparing it. So this adapter's claim that the
    id increments by one is only as good as this assertion over a real capture.
    """
    records = read_captured_payloads("bybit-linear", BOOK_FIXTURE)
    books = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.BOOK
    ]
    assert len(books) > 2

    for payload, facts in books:
        message = json.loads(payload)
        assert facts.symbol == CAPTURED_SYMBOL
        assert facts.sequence == int(message["data"]["u"])
        assert facts.venue_time_ns == int(message["ts"]) * 1_000_000

    steps = {
        later.sequence - earlier.sequence
        for (_, earlier), (_, later) in zip(books, books[1:])
    }
    assert steps == {1}, f"book update ids stepped by {steps} in the real capture"
    assert adapter.sequence_continuity(StreamKind.BOOK) is SequenceContinuity.INCREMENTS_BY_ONE
    assert all(adapter.read_previous_sequence(payload) is None for payload, _ in books)


def test_the_opening_snapshot_is_marked_as_restarting_the_numbering(
    adapter, read_captured_payloads
):
    """A snapshot is not a gap. Bybit re-sends one when its own service restarts."""
    records = read_captured_payloads("bybit-linear", BOOK_FIXTURE)
    books = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.BOOK
    ]
    snapshots = [facts for _, facts in books if facts.resets_sequence]
    assert len(snapshots) == 1, "expected exactly the opening snapshot in this capture"
    assert snapshots[0] is books[0][1]
    assert all(json.loads(payload)["type"] == "delta" for payload, facts in books[1:])


def test_an_unknown_topic_is_raised_rather_than_dropped(adapter):
    with pytest.raises(VenueMessageNotRecognised) as refusal:
        adapter.read_message_facts(
            b'{"topic":"liquidation.BTCUSDT","ts":1787373379440,"data":{}}'
        )
    assert "liquidation" in str(refusal.value)


def test_the_catalogue_reads_perpetual_and_dated_contracts_alike(adapter, read_captured_json):
    catalogue = read_captured_json("bybit-linear", CATALOGUE_FIXTURE)
    listings = adapter.read_symbol_listings(catalogue)
    assert listings
    types = {listing.contract_type for listing in listings}
    assert {"LinearPerpetual", "LinearFutures"} <= types, f"fixture holds only {types}"
    for listing in listings:
        assert adapter.is_symbol_capturable(listing) is (listing.status == "Trading")


def test_a_listing_carries_its_tick_size(adapter, read_captured_json):
    catalogue = read_captured_json("bybit-linear", CATALOGUE_FIXTURE)
    increments = [listing.price_increment for listing in adapter.read_symbol_listings(catalogue)]
    assert any(value is not None for value in increments)
    assert all(value is None or value > 0 for value in increments)


def test_a_403_is_the_whole_http_ban_and_it_states_its_own_floor(adapter):
    assert adapter.read_http_ban_signal(200, {}) is None
    assert adapter.read_http_ban_signal(429, {}) is None, "this venue does not use 429"
    banned = adapter.read_http_ban_signal(403, {})
    assert banned.venue_id == VENUE_ID and banned.observed_code == "403"
    assert banned.retry_after_seconds == 600.0


def test_only_a_rate_limit_failure_is_read_as_a_ban(adapter):
    """An ordinary failed subscribe is a mistake to fix, not a venue standing us down."""
    too_frequent = adapter.read_stream_ban_signal(
        json.dumps({"success": False, "retCode": 20003, "ret_msg": "Too frequent requests"}).encode()
    )
    assert too_frequent.observed_code == "20003"
    # No websocket ban duration is documented, and None says exactly that.
    assert too_frequent.retry_after_seconds is None

    by_text = adapter.read_stream_ban_signal(
        json.dumps({"success": False, "ret_msg": "403, access too frequent"}).encode()
    )
    assert by_text.observed_code == "403"

    ordinary_failure = adapter.read_stream_ban_signal(
        json.dumps({"success": False, "ret_msg": "Invalid symbol", "op": "subscribe"}).encode()
    )
    assert ordinary_failure is None


def test_no_real_captured_message_reads_as_a_ban(adapter, read_captured_payloads):
    for fixture in (TRADE_FIXTURE, CANDLE_FIXTURE, BOOK_FIXTURE):
        for _, payload in read_captured_payloads("bybit-linear", fixture):
            assert adapter.read_stream_ban_signal(payload) is None


def test_the_public_rest_endpoint_sends_no_rate_limit_headers(capture_manifest):
    """An open question in the research, answered by measurement.

    Bybit documents `X-Bapi-Limit` headers in an authenticated context and the
    three public market-data endpoints appear in no per-endpoint table at all.
    The live call carried none of those headers, so a rate budget for this venue
    cannot be read off a response the way Binance's used weight can -- it has to
    come from counting our own requests against the blanket 600-per-5-seconds.
    Recorded here so §9 does not later assume a header that was never sent.
    """
    entry = next(
        capture
        for capture in capture_manifest["captures"]
        if capture["path"].endswith(CATALOGUE_FIXTURE)
    )
    assert entry["response_headers_of_note"] == {}


def test_the_live_instrument_counts_are_recorded_with_the_fixture(capture_manifest):
    entry = next(
        capture
        for capture in capture_manifest["captures"]
        if capture["path"].endswith(CATALOGUE_FIXTURE)
    )
    counts = entry["symbol_counts_by_contract_type_and_status"]
    assert counts["LinearPerpetual/Trading"] > 500
    assert entry["full_response_instrument_count"] == sum(counts.values())
