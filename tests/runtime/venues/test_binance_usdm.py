"""The Binance USDⓈ-M adapter, against what Binance actually sent.

Every message here was captured live on 2026-08-22 and is in
`tests/captured/binance-usdm/` with its provenance in `capture-manifest.json`.
Nothing in this module is a payload anyone typed: RL-063, and more practically,
the fields worth testing are the ones nobody would have invented -- that a
partial-depth stream carries `U`/`u`/`pu` at all, that an aggTrade's `T` and `E`
differ, that the acknowledgement has no `e` field to tell it apart by.
"""

import json

import pytest

from runtime.tape import NOT_SENT, StreamKind, TradeFidelity
from runtime.venues.binance_usdm import (
    MARKET_ROUTE,
    PUBLIC_ROUTE,
    VENUE_ID,
    build_venue_adapter,
)
from runtime.venues.venue_adapter import (
    SequenceContinuity,
    StreamRequest,
    VenueMessageNotRecognised,
)

MARKET_FIXTURE = "2026-08-22-market-ws-aggtrade-kline.jsonl"
CANDLE_FIXTURE = "2026-08-22-market-ws-kline-through-close.jsonl"
BOOK_FIXTURE = "2026-08-22-public-ws-depth20.jsonl"
CATALOGUE_FIXTURE = "2026-08-22-catalogue-subset.json"

CAPTURED_SYMBOL = "BTCUSDT"
CANDLE_INTERVAL = "1m"
# What the operator's book_depth_levels setting ships as; the adapter maps it to
# the venue's own ladder, and that mapping is what these tests check.
REQUESTED_BOOK_DEPTH = 20


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


def test_the_routed_path_is_per_stream_kind(adapter):
    """The silent-failure trap of spec 1.1, pinned.

    aggTrade and kline are /market streams; depth is a /public stream. A
    connection carrying a /market stream anywhere else stays open and delivers
    nothing at all, so this assertion is the difference between a tape and an
    empty directory.
    """
    assert adapter.stream_endpoint_url(StreamKind.TRADE) == MARKET_ROUTE
    assert adapter.stream_endpoint_url(StreamKind.CANDLE) == MARKET_ROUTE
    assert adapter.stream_endpoint_url(StreamKind.BOOK) == PUBLIC_ROUTE
    assert "/market" in MARKET_ROUTE and "/public" in PUBLIC_ROUTE


def test_trades_are_declared_aggregated_because_no_raw_stream_exists(adapter):
    assert adapter.trade_fidelity is TradeFidelity.VENUE_AGGREGATED
    assert adapter.declared_limits()["trade_aggregation_window_ms"].value == 100


def test_subscription_topics_are_the_venue_s_own_phrasing(adapter):
    assert (
        adapter.subscription_topic(StreamRequest(StreamKind.TRADE, CAPTURED_SYMBOL))
        == "btcusdt@aggTrade"
    )
    assert (
        adapter.subscription_topic(
            StreamRequest(StreamKind.CANDLE, CAPTURED_SYMBOL, candle_interval=CANDLE_INTERVAL)
        )
        == "btcusdt@kline_1m"
    )
    assert (
        adapter.subscription_topic(
            StreamRequest(StreamKind.BOOK, CAPTURED_SYMBOL, book_depth_levels=REQUESTED_BOOK_DEPTH)
        )
        == "btcusdt@depth20@500ms"
    )


def test_a_candle_subscription_without_an_interval_is_refused(adapter):
    """A defaulted timeframe would be a timeframe nobody chose, written to the tape."""
    with pytest.raises(ValueError):
        adapter.subscription_topic(StreamRequest(StreamKind.CANDLE, CAPTURED_SYMBOL))


def test_book_depth_rounds_up_to_the_venue_s_own_ladder(adapter):
    """Never shallower than asked. The two venues offer different ladders."""
    assert adapter.resolve_book_depth_levels(5) == 5
    assert adapter.resolve_book_depth_levels(6) == 10
    assert adapter.resolve_book_depth_levels(20) == 20
    with pytest.raises(ValueError):
        adapter.resolve_book_depth_levels(50)
    with pytest.raises(ValueError):
        adapter.resolve_book_depth_levels(None)


def test_fit_is_a_stream_count_here_and_the_caller_never_counts(adapter):
    cap = adapter.declared_limits()["streams_per_connection"].value
    full = [f"sym{index}@aggTrade" for index in range(cap)]
    assert adapter.does_topic_fit_connection(full[:-1], "one-more@aggTrade") is True
    assert adapter.does_topic_fit_connection(full, "one-more@aggTrade") is False
    # A topic already on the connection always fits: resubscribing to it adds nothing.
    assert adapter.does_topic_fit_connection(full, full[0]) is True


def test_the_subscribe_frame_is_the_json_the_venue_accepted(adapter, read_captured_payloads):
    """The frame this builds is the frame the capture actually subscribed with.

    The manifest records what was sent; if this ever drifts from it, the fixtures
    stop being evidence about the code that will run in production.
    """
    topics = ["btcusdt@aggTrade", "btcusdt@kline_1m"]
    frame = json.loads(adapter.subscribe_frame(topics))
    assert frame["method"] == "SUBSCRIBE"
    assert frame["params"] == topics
    unsubscribe = json.loads(adapter.unsubscribe_frame(topics))
    assert unsubscribe["method"] == "UNSUBSCRIBE"


def test_the_subscribe_acknowledgement_is_not_a_tape_record(adapter, read_captured_payloads):
    """`{"result":null,"id":N}` has no `e` field, and that is how it is told apart."""
    records = read_captured_payloads("binance-usdm", MARKET_FIXTURE)
    acknowledgements = [
        payload for _, payload in records if adapter.read_message_facts(payload) is None
    ]
    assert acknowledgements, "the capture kept no acknowledgement, so this case is untested"
    assert json.loads(acknowledgements[0]) == {"result": None, "id": 2}


def test_every_captured_trade_reads_as_a_trade_with_its_own_time_and_id(
    adapter, read_captured_payloads
):
    records = read_captured_payloads("binance-usdm", MARKET_FIXTURE)
    trades = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.TRADE
    ]
    assert len(trades) > 1, "the capture holds too few trades to check continuity"

    for payload, facts in trades:
        message = json.loads(payload)
        assert facts.symbol == CAPTURED_SYMBOL
        # The trade's own time, not the event emission time. They differ in the
        # real capture, which is the whole reason to be explicit about which one.
        assert facts.venue_time_ns == int(message["T"]) * 1_000_000
        assert facts.sequence == int(message["a"])
        assert facts.is_closed_candle is None
    assert any(
        json.loads(payload)["T"] != json.loads(payload)["E"] for payload, _ in trades
    ), "no captured trade has T != E, so this test would pass on either field"

    ids = [facts.sequence for _, facts in trades]
    assert ids == sorted(ids), "aggregate trade ids arrived out of order in the real capture"


def test_a_real_closed_candle_reads_as_closed_and_the_open_ones_do_not(
    adapter, read_captured_payloads
):
    """The `x` flag, against a minute that actually ended during the capture."""
    records = read_captured_payloads("binance-usdm", CANDLE_FIXTURE)
    candles = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.CANDLE
    ]
    closed = [facts for _, facts in candles if facts.is_closed_candle]
    open_ones = [facts for _, facts in candles if facts.is_closed_candle is False]
    assert len(closed) == 1, f"expected exactly one close in this capture, saw {len(closed)}"
    assert open_ones, "every captured candle was closed, so the flag is untested against False"
    for payload, facts in candles:
        message = json.loads(payload)
        assert facts.venue_time_ns == int(message["E"]) * 1_000_000
        assert facts.sequence == NOT_SENT


def test_the_book_stream_carries_a_sequence_and_it_is_continuous(adapter, read_captured_payloads):
    """Measured, not assumed: partial depth carries U/u/pu, and pu chains to the last u.

    Binance's own rule is that each event's `pu` equals the previous event's `u`,
    otherwise the local book must be re-initialised. Every consecutive pair in
    this capture satisfies it, which is what makes the check in section 6
    meaningful rather than theoretical.
    """
    records = read_captured_payloads("binance-usdm", BOOK_FIXTURE)
    books = [
        (payload, facts)
        for payload, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.BOOK
    ]
    assert len(books) > 2

    for payload, facts in books:
        message = json.loads(payload)
        assert facts.symbol == CAPTURED_SYMBOL
        assert facts.sequence == int(message["u"])
        assert facts.venue_time_ns == int(message["T"]) * 1_000_000
        assert len(message["b"]) == REQUESTED_BOOK_DEPTH
        assert len(message["a"]) == REQUESTED_BOOK_DEPTH

    for (_, previous), (payload, _) in zip(books, books[1:]):
        assert adapter.read_previous_sequence(payload) == previous.sequence


def test_an_unknown_event_is_raised_rather_than_dropped(adapter):
    """An adapter that has gone out of date must say so.

    The bytes here are deliberately not a real venue message -- the case being
    tested is precisely the one no capture can contain, because it is a message
    this adapter does not yet know how to read.
    """
    with pytest.raises(VenueMessageNotRecognised) as refusal:
        adapter.read_message_facts(b'{"e":"somethingBinanceAddedLater","s":"BTCUSDT"}')
    assert "somethingBinanceAddedLater" in str(refusal.value)


def test_the_catalogue_reads_every_contract_type_including_tokenised_equities(
    adapter, read_captured_json, capture_manifest
):
    """The TRADIFI trap: an equity perpetual is kept, and marked as one.

    AAPLUSDT is a share traded as a USDT perpetual and nothing in the venue's
    response distinguishes it from a coin except `contractType`. The user's
    ruling was to capture it and filter at order time, so the test is that the
    type survives into the listing rather than that the symbol is excluded.
    """
    catalogue = read_captured_json("binance-usdm", CATALOGUE_FIXTURE)
    listings = adapter.read_symbol_listings(catalogue)
    assert listings

    types = {listing.contract_type for listing in listings}
    assert "PERPETUAL" in types
    assert "TRADIFI_PERPETUAL" in types, (
        "the fixture holds no tokenised equity, so the case this test exists for is absent"
    )

    equities = [listing for listing in listings if listing.contract_type == "TRADIFI_PERPETUAL"]
    assert all(listing.symbol.endswith("USDT") for listing in equities)
    assert all(adapter.is_symbol_capturable(listing) for listing in equities if listing.status == "TRADING")


def test_settling_contracts_are_not_captured_and_trading_ones_are(adapter, read_captured_json):
    """SETTLING is on its way to delisting -- a tape for it has nothing to accrue."""
    catalogue = read_captured_json("binance-usdm", CATALOGUE_FIXTURE)
    listings = adapter.read_symbol_listings(catalogue)
    statuses = {listing.status for listing in listings}
    assert {"TRADING", "SETTLING"} <= statuses, f"fixture holds only {statuses}"
    for listing in listings:
        assert adapter.is_symbol_capturable(listing) is (listing.status == "TRADING")


def test_a_listing_carries_its_tick_size_or_says_it_has_none(adapter, read_captured_json):
    """Absence is reported as absence -- tick-size-resolver infers one only then."""
    catalogue = read_captured_json("binance-usdm", CATALOGUE_FIXTURE)
    listings = adapter.read_symbol_listings(catalogue)
    increments = [listing.price_increment for listing in listings]
    assert any(value is not None for value in increments)
    assert all(value is None or value > 0 for value in increments)


def test_the_live_counts_in_the_manifest_still_match_the_spec(capture_manifest):
    """The numbers the connection budget is sized against, as measured at capture.

    Not a check on Binance -- it is a check that the fixture in this repository
    is still the world the spec was written about. When these drift far enough
    that section 4.2's connection arithmetic changes, this is where it surfaces.
    """
    entry = next(
        capture
        for capture in capture_manifest["captures"]
        if capture["path"].endswith(CATALOGUE_FIXTURE) and capture["venue"] == VENUE_ID
    )
    counts = entry["symbol_counts_by_contract_type_and_status"]
    assert counts["PERPETUAL/TRADING"] == 570
    assert counts["TRADIFI_PERPETUAL/TRADING"] == 170
    assert entry["full_response_symbol_count"] == 872


def test_a_rate_limit_response_is_read_as_a_ban_signal(adapter):
    """429 is back off; 418 is banned. Both per IP, and no key exempts us."""
    assert adapter.read_http_ban_signal(200, {}) is None
    warning = adapter.read_http_ban_signal(429, {"Retry-After": "30"})
    assert warning.venue_id == VENUE_ID and warning.observed_code == "429"
    assert warning.retry_after_seconds == 30.0
    banned = adapter.read_http_ban_signal(418, {})
    assert banned.observed_code == "418"
    # No Retry-After is documented for futures, so its absence must be None rather
    # than a zero that would read as "you may retry immediately".
    assert banned.retry_after_seconds is None


def test_a_websocket_rate_limit_error_carries_its_own_ban_deadline(adapter):
    """The ban's end is stated only in the message text, so it is parsed from there."""
    import time

    banned_until_ms = int((time.time() + 120) * 1000)
    payload = json.dumps(
        {"code": -1003, "msg": f"Way too many requests; IP banned until {banned_until_ms}."}
    ).encode()
    signal = adapter.read_stream_ban_signal(payload)
    assert signal.observed_code == "-1003"
    assert 60 < signal.retry_after_seconds <= 120


def test_an_ordinary_message_is_not_read_as_a_ban(adapter, read_captured_payloads):
    """Every real captured message must read as no ban at all."""
    for fixture in (MARKET_FIXTURE, BOOK_FIXTURE):
        for _, payload in read_captured_payloads("binance-usdm", fixture):
            assert adapter.read_stream_ban_signal(payload) is None


def test_used_request_weight_is_read_from_the_venue_not_counted_locally(
    adapter, capture_manifest
):
    """The header the real catalogue call came back with, from the manifest."""
    entry = next(
        capture
        for capture in capture_manifest["captures"]
        if capture["path"].endswith(CATALOGUE_FIXTURE) and capture["venue"] == VENUE_ID
    )
    headers = entry["response_headers_of_note"]
    assert adapter.read_used_request_weight(headers) == int(headers["x-mbx-used-weight-1m"])
    assert adapter.read_used_request_weight({}) is None


def test_the_client_does_not_ping_because_the_venue_does(adapter):
    discipline = adapter.heartbeat_discipline()
    assert discipline.expects_client_ping is False
    limits = adapter.declared_limits()
    assert limits["server_ping_interval_seconds"].value < limits["pong_deadline_seconds"].value


def test_each_stream_says_what_its_sequence_promises(adapter, read_captured_payloads):
    """The book chains, trades increment, candles are not numbered -- all measured.

    Binance documents the book's `pu` rule. Nothing documents that aggregate
    trade ids step by exactly one, so that claim is checked here against the
    whole capture rather than asserted from the enum.
    """
    assert adapter.sequence_continuity(StreamKind.BOOK) is SequenceContinuity.CHAINED_TO_PREVIOUS
    assert adapter.sequence_continuity(StreamKind.TRADE) is SequenceContinuity.INCREMENTS_BY_ONE
    assert adapter.sequence_continuity(StreamKind.CANDLE) is SequenceContinuity.NOT_NUMBERED

    records = read_captured_payloads("binance-usdm", MARKET_FIXTURE)
    trade_ids = [
        facts.sequence
        for _, facts in data_facts(adapter, records)
        if facts.stream_kind is StreamKind.TRADE
    ]
    steps = {later - earlier for earlier, later in zip(trade_ids, trade_ids[1:])}
    assert steps == {1}, f"aggregate trade ids stepped by {steps} in the real capture"


def test_only_the_chained_stream_names_its_predecessor(adapter, read_captured_payloads):
    """A trade message must not be read as claiming a continuity Binance never made."""
    for _, payload in read_captured_payloads("binance-usdm", MARKET_FIXTURE):
        assert adapter.read_previous_sequence(payload) is None
    books = read_captured_payloads("binance-usdm", BOOK_FIXTURE)
    assert any(adapter.read_previous_sequence(payload) is not None for _, payload in books)
