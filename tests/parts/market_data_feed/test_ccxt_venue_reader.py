"""Candles onto the tape, against the minutes that actually closed while capturing.

Both fixtures were captured by running until the venue sent a closed candle and
then keeping a few more, because a minute closes once a minute and forty messages
of a busy symbol is ten seconds -- a fixture sized by message count would contain
no close at all, and the closed flag is the one field of this stream worth
testing.
"""

import pytest

from parts.market_data_feed.ccxt_venue_reader import (
    CAPTURED_STREAM_KIND,
    PART_DECLARATION,
    PART_ID,
    CandleStreamReader,
    describe_capture,
    is_closed_candle,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.stream_plan import ConnectionAssignment, StreamPlan
from runtime.tape import StreamKind, read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import StreamRequest

CAPTURED_SYMBOL = "BTCUSDT"
CANDLE_INTERVAL = "1m"
WRITEBACK_INTERVAL = 8 * 1024 * 1024
BACKOFF_FLOOR = 1.0
BACKOFF_CEILING = 60.0
DRAIN_INTERVAL = 0.5

FIXTURES = {
    "binance-usdm": "2026-08-22-market-ws-kline-through-close.jsonl",
    "bybit-linear": "2026-08-22-public-linear-kline-through-close.jsonl",
}


class ReplayingConnection:
    """Hands the reader the payloads a venue really sent, one tick at a time."""

    def __init__(self, payloads):
        self._ticks = [list(payloads)]
        self.url = "wss://replayed"
        self.topics = ()
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

    def drain(self):
        return self._ticks.pop(0) if self._ticks else []

    def close(self):
        pass


def build_reader(venue_id, tape_root):
    plan = StreamPlan(
        connections=(
            ConnectionAssignment(
                venue_id=venue_id,
                requests=(
                    StreamRequest(
                        CAPTURED_STREAM_KIND, CAPTURED_SYMBOL, candle_interval=CANDLE_INTERVAL
                    ),
                ),
            ),
        )
    )
    return CandleStreamReader(
        adapter=load_venue_adapter(venue_id),
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=WRITEBACK_INTERVAL,
        reconnect_backoff_floor_seconds=BACKOFF_FLOOR,
        reconnect_backoff_ceiling_seconds=BACKOFF_CEILING,
        drain_interval_seconds=DRAIN_INTERVAL,
    )


def replay(reader, payloads):
    for connection in reader.connections:
        connection.close()
    reader._connections = [ReplayingConnection(payloads)]
    return reader.capture_one_tick()


@pytest.fixture
def tape_root(durable_tmp_path):
    return durable_tmp_path / "tape"


def only_day_paths(tape_root, venue_id):
    import pathlib

    from runtime.tape import tape_paths_for

    directory = pathlib.Path(tape_root) / venue_id / CAPTURED_SYMBOL
    days = sorted({path.stem for path in directory.glob("*.index")})
    assert len(days) == 1
    return tape_paths_for(tape_root, venue_id, CAPTURED_SYMBOL, days[0])


def test_the_built_wiring_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_this_part_and_the_trade_reader_do_not_share_a_stream_kind():
    """Two parts on one topic would put two copies of every message on the tape."""
    from parts.market_data_feed.venue_trade_stream_reader import (
        CAPTURED_STREAM_KIND as TRADE_KIND,
    )

    assert CAPTURED_STREAM_KIND is StreamKind.CANDLE
    assert CAPTURED_STREAM_KIND is not TRADE_KIND


@pytest.mark.parametrize("venue_id", sorted(FIXTURES))
def test_every_captured_candle_lands_on_the_tape_byte_for_byte(
    venue_id, tape_root, read_captured_payloads
):
    payloads = [payload for _, payload in read_captured_payloads(venue_id, FIXTURES[venue_id])]
    adapter = load_venue_adapter(venue_id)

    reader = build_reader(venue_id, tape_root)
    with reader:
        written = replay(reader, payloads)
    expected = [
        payload
        for payload in payloads
        if (facts := adapter.read_message_facts(payload)) is not None
        and facts.stream_kind is StreamKind.CANDLE
    ]
    assert written == len(expected) > 0

    index_path, blob_path = only_day_paths(tape_root, venue_id)
    index = read_tape_index(index_path)
    assert [read_payload(blob_path, record) for record in index] == expected


@pytest.mark.parametrize("venue_id", sorted(FIXTURES))
def test_the_closed_minute_is_recoverable_from_the_tape(
    venue_id, tape_root, read_captured_payloads
):
    """The flag is not in the index, and it does not need to be.

    The payload is stored exactly as it arrived, so the closed flag is recovered
    by asking the adapter. Putting it in the index would be normalising on write,
    which is unrecoverable if wrong -- while this is a fix.
    """
    payloads = [payload for _, payload in read_captured_payloads(venue_id, FIXTURES[venue_id])]
    adapter = load_venue_adapter(venue_id)

    reader = build_reader(venue_id, tape_root)
    with reader:
        replay(reader, payloads)

    index_path, blob_path = only_day_paths(tape_root, venue_id)
    index = read_tape_index(index_path)
    flags = [is_closed_candle(adapter, read_payload(blob_path, record)) for record in index]
    assert flags.count(True) == 1, f"expected exactly one closed minute, saw {flags.count(True)}"
    assert flags.count(False) > 0, "every candle was closed, so the open case is untested"
    assert None not in flags


@pytest.mark.parametrize("venue_id", sorted(FIXTURES))
def test_an_open_candle_is_never_recorded_as_closed(
    venue_id, tape_root, read_captured_payloads
):
    """The failure this part exists to prevent: a partial minute read as finished.

    Both venues push updates to the current candle continuously, so a reader that
    took the last update it happened to see would record a partial minute as a
    complete one -- and every indicator built on it would be wrong with nothing
    to show for it.
    """
    payloads = [payload for _, payload in read_captured_payloads(venue_id, FIXTURES[venue_id])]
    adapter = load_venue_adapter(venue_id)
    closed_at = [
        index
        for index, payload in enumerate(payloads)
        if is_closed_candle(adapter, payload) is True
    ]
    assert len(closed_at) == 1
    # Everything after the close belongs to the next minute and is open again --
    # so "the last message" is emphatically not "the closed candle".
    assert closed_at[0] < len(payloads) - 1
    assert is_closed_candle(adapter, payloads[-1]) is False


def test_a_trade_arriving_on_a_candle_connection_is_not_written(
    tape_root, read_captured_payloads
):
    payloads = [
        payload
        for _, payload in read_captured_payloads(
            "binance-usdm", "2026-08-22-market-ws-aggtrade-kline.jsonl"
        )
    ]
    adapter = load_venue_adapter("binance-usdm")
    trades = [
        payload
        for payload in payloads
        if (facts := adapter.read_message_facts(payload)) is not None
        and facts.stream_kind is StreamKind.TRADE
    ]
    assert trades

    reader = build_reader("binance-usdm", tape_root)
    with reader:
        assert replay(reader, trades) == 0
    assert reader.standing.wrong_kind_messages == len(trades)
    assert reader.standing.records_written == 0


def test_the_description_names_this_part_and_its_stream_kind(
    tape_root, read_captured_payloads
):
    payloads = [payload for _, payload in read_captured_payloads("bybit-linear", FIXTURES["bybit-linear"])]
    reader = build_reader("bybit-linear", tape_root)
    with reader:
        replay(reader, payloads)
        description = describe_capture(reader)
    assert description["part_id"] == PART_ID
    assert description["stream_kind"] == "CANDLE"
    assert description["records_written"] > 0
    assert description["symbols_written"] == [CAPTURED_SYMBOL]
