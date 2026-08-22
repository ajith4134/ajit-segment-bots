"""The book reader, and the one stream it is not allowed to thin.

Both fixtures are real: binance-usdm's `depth20@500ms` partial-depth pushes and
bybit-linear's opening snapshot followed by deltas. The difference between them
is the whole test -- one may be recorded every few seconds, the other may not be
recorded any other way than whole.
"""

import json
import pathlib

import pytest

from parts.market_data_feed.order_book_reader import (
    CAPTURED_STREAM_KIND,
    PART_DECLARATION,
    PART_ID,
    OrderBookReader,
    describe_capture,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.stream_plan import ConnectionAssignment, StreamPlan
from runtime.tape import StreamKind, count_whole_records, read_payload, read_tape_index, tape_paths_for
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import StreamRequest

CAPTURED_SYMBOL = "BTCUSDT"
BOOK_DEPTH = 20
WRITEBACK_INTERVAL = 8 * 1024 * 1024
BACKOFF_FLOOR = 1.0
BACKOFF_CEILING = 60.0
DRAIN_INTERVAL = 0.5
SNAPSHOT_INTERVAL = 5.0

FIXTURES = {
    "binance-usdm": "2026-08-22-public-ws-depth20.jsonl",
    "bybit-linear": "2026-08-22-public-linear-orderbook.jsonl",
}


class FakeClock:
    """A clock the test moves, so a 5-second interval is asserted, not endured."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class ReplayingConnection:
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


def build_reader(venue_id, tape_root, clock=None, interval=SNAPSHOT_INTERVAL):
    plan = StreamPlan(
        connections=(
            ConnectionAssignment(
                venue_id=venue_id,
                requests=(
                    StreamRequest(CAPTURED_STREAM_KIND, CAPTURED_SYMBOL, book_depth_levels=BOOK_DEPTH),
                ),
            ),
        )
    )
    return OrderBookReader(
        adapter=load_venue_adapter(venue_id),
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=WRITEBACK_INTERVAL,
        reconnect_backoff_floor_seconds=BACKOFF_FLOOR,
        reconnect_backoff_ceiling_seconds=BACKOFF_CEILING,
        drain_interval_seconds=DRAIN_INTERVAL,
        book_snapshot_interval_seconds=interval,
        monotonic=(clock or FakeClock()).monotonic,
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
    directory = pathlib.Path(tape_root) / venue_id / CAPTURED_SYMBOL
    days = sorted({path.stem for path in directory.glob("*.index")})
    assert len(days) == 1
    return tape_paths_for(tape_root, venue_id, CAPTURED_SYMBOL, days[0])


def book_payloads(venue_id, read_captured_payloads):
    adapter = load_venue_adapter(venue_id)
    return [
        payload
        for _, payload in read_captured_payloads(venue_id, FIXTURES[venue_id])
        if (facts := adapter.read_message_facts(payload)) is not None
        and facts.stream_kind is StreamKind.BOOK
    ]


def test_the_built_wiring_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)
    assert "order-book-snapshot" in PART_DECLARATION.produces


def test_the_two_venues_disagree_about_whether_thinning_is_safe(tape_root):
    """Measured, and it decides everything else this part does."""
    assert build_reader("binance-usdm", tape_root).thins_this_venue is True
    assert build_reader("bybit-linear", tape_root).thins_this_venue is False


def test_a_delta_stream_is_recorded_whole_whatever_the_interval_says(
    tape_root, read_captured_payloads
):
    """Dropping a Bybit delta costs the book, not resolution -- so none are dropped.

    The clock never moves here, so a reader that thinned would write exactly one
    message. It writes all of them.
    """
    payloads = book_payloads("bybit-linear", read_captured_payloads)
    assert len(payloads) > 2

    reader = build_reader("bybit-linear", tape_root)
    with reader:
        written = replay(reader, payloads)
    assert written == len(payloads)
    assert reader.thinned_messages == 0

    index_path, blob_path = only_day_paths(tape_root, "bybit-linear")
    assert count_whole_records(index_path) == len(payloads)
    # And the update ids on the tape are the venue's own, unbroken -- which is
    # what makes §6's continuity check possible on a replay.
    index = read_tape_index(index_path)
    sequences = [int(record["sequence"]) for record in index]
    assert sequences == sorted(sequences)
    assert {later - earlier for earlier, later in zip(sequences, sequences[1:])} == {1}


def test_a_full_depth_stream_is_thinned_to_the_snapshot_interval(
    tape_root, read_captured_payloads
):
    """Each Binance push carries the whole top-20, so recording one in N loses only detail."""
    payloads = book_payloads("binance-usdm", read_captured_payloads)
    assert len(payloads) > 3

    clock = FakeClock()
    reader = build_reader("binance-usdm", tape_root, clock=clock)
    with reader:
        written = replay(reader, payloads)
    assert written == 1, "only the first push of the interval belongs on the tape"
    assert reader.thinned_messages == len(payloads) - 1

    index_path, blob_path = only_day_paths(tape_root, "binance-usdm")
    assert count_whole_records(index_path) == 1
    # What was kept is a whole book, not a fragment -- which is why thinning is
    # safe here at all.
    kept = json.loads(read_payload(blob_path, read_tape_index(index_path)[0]))
    assert len(kept["b"]) == BOOK_DEPTH and len(kept["a"]) == BOOK_DEPTH


def test_the_interval_passing_lets_the_next_push_through(tape_root, read_captured_payloads):
    payloads = book_payloads("binance-usdm", read_captured_payloads)
    clock = FakeClock()
    reader = build_reader("binance-usdm", tape_root, clock=clock)
    with reader:
        assert replay(reader, payloads[:1]) == 1
        clock.now += SNAPSHOT_INTERVAL / 2
        assert replay(reader, payloads[1:2]) == 0
        clock.now += SNAPSHOT_INTERVAL
        assert replay(reader, payloads[2:3]) == 1
    assert reader.thinned_messages == 1


def test_what_was_thinned_is_counted_and_reported(tape_root, read_captured_payloads):
    """No silent caps: a tape thinned nine tenths must not look like a quiet venue."""
    payloads = book_payloads("binance-usdm", read_captured_payloads)
    reader = build_reader("binance-usdm", tape_root)
    with reader:
        replay(reader, payloads)
        description = describe_capture(reader)
    assert description["part_id"] == PART_ID
    assert description["stream_kind"] == "BOOK"
    assert description["thins_this_venue"] is True
    assert description["thinned_messages"] == len(payloads) - 1
    assert description["book_snapshot_interval_seconds"] == SNAPSHOT_INTERVAL
    assert description["records_written"] + description["thinned_messages"] == len(payloads)


def test_a_resync_is_written_even_inside_the_snapshot_interval(
    tape_root, read_captured_payloads
):
    """A book rebuilt after a gap is not the same object as one that never gapped.

    Bybit's opening snapshot is the real message carrying that flag, and its
    deltas are the real messages around it. The venue's own stream is never
    thinned, so the reader is forced to thin here to put the rule under load:
    a delta is written, a second message arrives inside the interval, and it is
    written anyway because it announces a resync. Without that exception the tape
    would carry a gap it could not describe (spec §6).
    """
    adapter = load_venue_adapter("bybit-linear")
    payloads = book_payloads("bybit-linear", read_captured_payloads)
    snapshots = [p for p in payloads if adapter.read_message_facts(p).resets_sequence]
    deltas = [p for p in payloads if not adapter.read_message_facts(p).resets_sequence]
    assert len(snapshots) == 1 and len(deltas) > 1

    clock = FakeClock()
    reader = build_reader("bybit-linear", tape_root, clock=clock)
    reader._may_thin = True
    with reader:
        assert replay(reader, [deltas[0]]) == 1
        # Inside the interval: an ordinary delta is thinned...
        assert replay(reader, [deltas[1]]) == 0
        # ...and the resync is not.
        assert replay(reader, snapshots) == 1
    assert reader.thinned_messages == 1
