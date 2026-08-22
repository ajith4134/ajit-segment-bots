"""The first part: what it writes, and what it refuses to write quietly.

The messages replayed here are the ones both venues actually sent on 2026-08-22
(RL-063). They are replayed through a connection that hands them over rather than
over a live socket, because what is under test is the tape this part produces --
that the venue's bytes survive unchanged, that a control frame is not a record,
and that a message the adapter cannot read is counted rather than dropped.

The live path is covered by the connection's own tests and was verified against
both venues directly; what is *not* coverable any other way is what lands on
disk, which is the thing the whole phase exists to produce.
"""

import json
import pathlib

import pytest

from parts.market_data_feed.venue_trade_stream_reader import (
    CAPTURED_STREAM_KIND,
    PART_DECLARATION,
    PART_ID,
    TradeStreamReader,
    describe_capture,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.stream_plan import ConnectionAssignment, StreamPlan
from runtime.tape import (
    StreamKind,
    TAPE_RECORD_BYTES,
    count_whole_records,
    read_payload,
    read_tape_index,
    tape_paths_for,
)
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import StreamRequest

CAPTURED_SYMBOL = "BTCUSDT"
WRITEBACK_INTERVAL = 8 * 1024 * 1024
BACKOFF_FLOOR = 1.0
BACKOFF_CEILING = 60.0
DRAIN_INTERVAL = 0.5

FIXTURES = {
    "binance-usdm": "2026-08-22-market-ws-aggtrade-kline.jsonl",
    "bybit-linear": "2026-08-22-public-linear-trade.jsonl",
}


class ReplayingConnection:
    """Hands a reader the payloads a venue really sent, one tick at a time.

    Deliberately not a mock of the connection's behaviour -- the connection has
    its own tests against a real server. This stands in for the socket only, so
    that what is being checked here is the tape rather than the network.
    """

    def __init__(self, payloads, url="wss://replayed", topics=()):
        self._ticks = [list(payloads)]
        self.url = url
        self.topics = tuple(topics)
        self.standing = type(
            "ReplayStanding",
            (),
            {
                "opened_count": 1,
                "scheduled_close_count": 0,
                "unexpected_close_count": 0,
                "consecutive_failures": 0,
                "messages_received": len(list(payloads)),
                "last_close_reason": None,
            },
        )()
        self.closed = False

    def drain(self):
        return self._ticks.pop(0) if self._ticks else []

    def close(self):
        self.closed = True


def build_reader(venue_id, tape_root, symbols=(CAPTURED_SYMBOL,)):
    adapter = load_venue_adapter(venue_id)
    plan = StreamPlan(
        connections=(
            ConnectionAssignment(
                venue_id=venue_id,
                requests=tuple(
                    StreamRequest(CAPTURED_STREAM_KIND, symbol) for symbol in symbols
                ),
            ),
        )
    )
    return TradeStreamReader(
        adapter=adapter,
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=WRITEBACK_INTERVAL,
        reconnect_backoff_floor_seconds=BACKOFF_FLOOR,
        reconnect_backoff_ceiling_seconds=BACKOFF_CEILING,
        drain_interval_seconds=DRAIN_INTERVAL,
    )


def replay(reader, payloads):
    """Replace the reader's live connections with one that replays real bytes."""
    for connection in reader.connections:
        connection.close()
    reader._connections = [ReplayingConnection(payloads)]
    return reader.capture_one_tick()


@pytest.fixture
def tape_root(durable_tmp_path):
    return durable_tmp_path / "tape"


def test_the_built_wiring_equals_the_blueprint():
    """RL-067, checked here as well as by the board's own probe."""
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


@pytest.mark.parametrize("venue_id", sorted(FIXTURES))
def test_every_captured_trade_lands_on_the_tape_byte_for_byte(
    venue_id, tape_root, read_captured_payloads
):
    """The tape records what arrived, not what we made of it (spec §2.2).

    Compared as bytes rather than as parsed JSON: a normalisation that happened
    on the way in would be invisible to a comparison that parsed both sides, and
    it is exactly the failure the format was chosen against, because a
    normalisation bug frozen into a tape is unrecoverable.
    """
    records = read_captured_payloads(venue_id, FIXTURES[venue_id])
    payloads = [payload for _, payload in records]

    reader = build_reader(venue_id, tape_root)
    with reader:
        written = replay(reader, payloads)
        adapter = load_venue_adapter(venue_id)
        expected = [
            payload
            for payload in payloads
            if (facts := adapter.read_message_facts(payload)) is not None
            and facts.stream_kind is StreamKind.TRADE
        ]
        assert written == len(expected) > 0

    index_path, blob_path = _paths_for_only_day(tape_root, venue_id, CAPTURED_SYMBOL)
    index = read_tape_index(index_path)
    assert len(index) == len(expected)
    on_tape = [read_payload(blob_path, record) for record in index]
    assert on_tape == expected


@pytest.mark.parametrize("venue_id", sorted(FIXTURES))
def test_the_index_carries_the_venue_s_own_time_and_sequence(
    venue_id, tape_root, read_captured_payloads
):
    """What the adapter read, and nothing this part decided."""
    payloads = [payload for _, payload in read_captured_payloads(venue_id, FIXTURES[venue_id])]
    adapter = load_venue_adapter(venue_id)

    reader = build_reader(venue_id, tape_root)
    with reader:
        replay(reader, payloads)

    index_path, _ = _paths_for_only_day(tape_root, venue_id, CAPTURED_SYMBOL)
    index = read_tape_index(index_path)
    expected = [
        adapter.read_message_facts(payload)
        for payload in payloads
        if (facts := adapter.read_message_facts(payload)) is not None
        and facts.stream_kind is StreamKind.TRADE
    ]
    for record, facts in zip(index, expected):
        assert int(record["stream_kind"]) == int(StreamKind.TRADE)
        assert int(record["venue_time_ns"]) == facts.venue_time_ns
        assert int(record["sequence"]) == facts.sequence
        # Received time is ours, not the venue's, and it must be later than the
        # venue's own timestamp for a message that has actually travelled.
        assert int(record["received_at_ns"]) > facts.venue_time_ns


def test_a_control_frame_is_counted_and_never_written(tape_root, read_captured_payloads):
    """The subscribe acknowledgement belongs on no tape, and it is not a fault."""
    payloads = [payload for _, payload in read_captured_payloads("binance-usdm", FIXTURES["binance-usdm"])]
    reader = build_reader("binance-usdm", tape_root)
    with reader:
        replay(reader, payloads)
    assert reader.standing.control_frames == 1
    assert reader.standing.records_written == reader.standing.records_written
    index_path, _ = _paths_for_only_day(tape_root, "binance-usdm", CAPTURED_SYMBOL)
    assert count_whole_records(index_path) == reader.standing.records_written


def test_a_message_the_adapter_cannot_read_is_counted_not_dropped(tape_root):
    """A venue adding an event type is a fact the board needs, not a silence.

    These bytes are deliberately not a real venue message: the case is precisely
    the one no capture can contain, because it is a message the adapter does not
    yet know how to read.
    """
    reader = build_reader("binance-usdm", tape_root)
    with reader:
        written = replay(reader, [b'{"e":"somethingBinanceAddedLater","s":"BTCUSDT"}'])
    assert written == 0
    assert reader.standing.unreadable_messages == 1
    assert "somethingBinanceAddedLater" in reader.standing.last_unreadable_reason
    assert not reader.standing.has_written_anything


def test_a_candle_arriving_on_a_trade_connection_is_not_written(
    tape_root, read_captured_payloads
):
    """Two parts writing one message would put two copies of it on the tape."""
    payloads = [
        payload
        for _, payload in read_captured_payloads("binance-usdm", FIXTURES["binance-usdm"])
    ]
    adapter = load_venue_adapter("binance-usdm")
    candles = [
        payload
        for payload in payloads
        if (facts := adapter.read_message_facts(payload)) is not None
        and facts.stream_kind is StreamKind.CANDLE
    ]
    assert candles, "the fixture holds no candle, so this case is untested"

    reader = build_reader("binance-usdm", tape_root)
    with reader:
        written = replay(reader, candles)
    assert written == 0
    # Counted as the wrong kind rather than as unreadable: the adapter read it
    # perfectly, it simply belongs to another part's tape. Merging the two counts
    # would make an out-of-date adapter and a mis-planned connection look alike.
    assert reader.standing.wrong_kind_messages == len(candles)
    assert reader.standing.unreadable_messages == 0
    assert "CANDLE message arrived" in reader.standing.last_unreadable_reason


def test_a_reader_with_nothing_assigned_is_refused(tape_root):
    """A reader with no subscriptions reports healthy while capturing nothing."""
    with pytest.raises(ValueError) as refusal:
        TradeStreamReader(
            adapter=load_venue_adapter("binance-usdm"),
            plan=StreamPlan(connections=()),
            tape_root=tape_root,
            writeback_interval_bytes=WRITEBACK_INTERVAL,
            reconnect_backoff_floor_seconds=BACKOFF_FLOOR,
            reconnect_backoff_ceiling_seconds=BACKOFF_CEILING,
            drain_interval_seconds=DRAIN_INTERVAL,
        )
    assert "no TRADE subscriptions" in str(refusal.value)


def test_a_tape_root_on_tmpfs_is_refused():
    """/tmp is tmpfs on this box: a tape there measures as working until it matters."""
    from runtime.tape import TapeRootRefused

    with pytest.raises(TapeRootRefused):
        build_reader("binance-usdm", pathlib.Path("/tmp/a-tape-that-would-vanish"))


def test_the_capture_description_reports_only_what_was_counted(
    tape_root, read_captured_payloads
):
    """Rule 8: no field here is an assertion of health, and fidelity travels with it."""
    payloads = [payload for _, payload in read_captured_payloads("bybit-linear", FIXTURES["bybit-linear"])]
    reader = build_reader("bybit-linear", tape_root)
    with reader:
        replay(reader, payloads)
        description = describe_capture(reader)

    assert description["part_id"] == PART_ID
    assert description["venue_id"] == "bybit-linear"
    assert description["trade_fidelity"] == "every-print"
    assert description["records_written"] == reader.standing.records_written
    assert description["symbols_written"] == [CAPTURED_SYMBOL]
    assert "healthy" not in json.dumps(description)


def test_the_other_venue_s_fidelity_is_recorded_as_aggregated(tape_root, read_captured_payloads):
    """Binance has no raw trade stream, and the tape must not imply otherwise."""
    payloads = [payload for _, payload in read_captured_payloads("binance-usdm", FIXTURES["binance-usdm"])]
    reader = build_reader("binance-usdm", tape_root)
    with reader:
        replay(reader, payloads)
        assert describe_capture(reader)["trade_fidelity"] == "venue-aggregated"


def test_closing_releases_every_socket_and_every_tape_file(tape_root, read_captured_payloads):
    """The suite runs under -W error::ResourceWarning and this part owns both."""
    payloads = [payload for _, payload in read_captured_payloads("binance-usdm", FIXTURES["binance-usdm"])]
    reader = build_reader("binance-usdm", tape_root)
    replay(reader, payloads)
    reader.close()
    reader.close()
    assert reader._writers == {}


def test_a_restart_appends_to_the_day_rather_than_truncating_it(
    tape_root, read_captured_payloads
):
    """A part is SIGKILLed as the ordinary way of being switched off.

    A reader that truncated on open would erase the day's capture every time the
    governor switched it, which is the failure the tape's append mode exists for
    -- checked here through the part rather than only through the tape.
    """
    payloads = [payload for _, payload in read_captured_payloads("binance-usdm", FIXTURES["binance-usdm"])]

    first = build_reader("binance-usdm", tape_root)
    with first:
        written_first = replay(first, payloads)
    second = build_reader("binance-usdm", tape_root)
    with second:
        written_second = replay(second, payloads)

    index_path, _ = _paths_for_only_day(tape_root, "binance-usdm", CAPTURED_SYMBOL)
    assert count_whole_records(index_path) == written_first + written_second
    assert index_path.stat().st_size == (written_first + written_second) * TAPE_RECORD_BYTES


def _paths_for_only_day(tape_root, venue_id, symbol):
    """The one day's index and blob this test wrote, whatever day it ran on."""
    directory = pathlib.Path(tape_root) / venue_id / symbol
    days = sorted({path.stem for path in directory.glob("*.index")})
    assert len(days) == 1, f"expected one day of tape in {directory}, found {days}"
    return tape_paths_for(tape_root, venue_id, symbol, days[0])


# --- Publishing: what leaves this part for the other 65 that read market-data ---


def replay_publishing(reader, payloads):
    """Replay, capturing what the part would have published, in order."""
    for connection in reader.connections:
        connection.close()
    reader._connections = [ReplayingConnection(payloads)]
    published = []
    written = reader.capture_one_tick(on_recorded_payload=published.append)
    return written, published


def test_only_what_reached_the_tape_is_published(tape_root, read_captured_payloads):
    """The order is the point: history first, then the rest of the system.

    A payload the tape refused -- a control frame, a message the adapter cannot
    read -- must not be handed on. The rest of the system acting on a message that
    is not in the record would make the tape and the behaviour disagree, and only
    one of the two can be rebuilt.
    """
    records = read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl")
    payloads = [payload for _received_at_ns, payload in records][:50]
    control_frame = json.dumps({"result": None, "id": 1}).encode()
    unreadable = json.dumps({"e": "somethingNew", "s": CAPTURED_SYMBOL}).encode()

    reader = build_reader("binance-usdm", tape_root)
    written, published = replay_publishing(reader, [control_frame, *payloads, unreadable])
    reader.close()

    assert written == len(payloads)
    assert published == payloads
    assert reader.standing.control_frames == 1
    assert reader.standing.unreadable_messages == 1


def test_what_is_published_normalises_to_the_trades_the_venue_sent(tape_root, read_captured_payloads):
    records = read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl")
    payloads = [payload for _received_at_ns, payload in records][:20]

    reader = build_reader("binance-usdm", tape_root)
    _written, published = replay_publishing(reader, payloads)
    adapter = reader.adapter
    trades = [trade for payload in published for trade in adapter.read_trades(payload)]
    reader.close()

    assert len(trades) == len(payloads)
    for payload, trade in zip(payloads, trades, strict=True):
        message = json.loads(payload)
        assert trade.symbol == message["s"]
        assert trade.price == float(message["p"])
        assert trade.venue_id == "binance-usdm"


def test_publishing_is_optional_so_a_reader_with_no_bus_still_records(tape_root, read_captured_payloads):
    """The tape is the part's first duty and does not depend on anyone listening."""
    records = read_captured_payloads("bybit-linear", "2026-08-22-public-linear-trade.jsonl")
    payloads = [payload for _received_at_ns, payload in records][:20]

    reader = build_reader("bybit-linear", tape_root)
    written = replay(reader, payloads)
    reader.close()

    assert written > 0
