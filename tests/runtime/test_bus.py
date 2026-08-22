"""The bus, tested on what the venues actually sent (RL-063).

The payloads here are real Binance aggTrade frames captured from the live socket,
not dictionaries written to make a test pass. What is synthetic is the topology --
two or three parts wired together by hand -- because the property under test is the
transport, and binding 65 inboxes to check a fan-out would measure pytest.

The tests that matter most are the ones about loss: that a producer never waits, that
a consumer being off is distinguishable from a consumer being behind, and that a
message which never arrived is counted by the part that did not get it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import time

import pytest

from runtime.bus import (
    CODEC_VERSION,
    FrameRefused,
    Inbox,
    Message,
    MessageTooLarge,
    PartBus,
    Publisher,
    decode_frame,
    encode_frame,
)
from runtime.part_declaration import PartDeclaration, RateRisk, ResourceClass, SkippedTickEffect
from runtime.wiring_plan import PartWiring

# Measured on this box: the default receive buffer holds 167 messages of 222 bytes.
# These are the values a settings entry supplies at runtime (spec section 7); a test
# states them itself so that what it is exercising is visible in the test.
DEFAULT_RECEIVE_BUFFER_BYTES = 212_992
SMALL_RECEIVE_BUFFER_BYTES = 4_096
MAXIMUM_MESSAGE_BYTES = 131_072

PUBLISH_MUST_NOT_TAKE_LONGER_THAN_SECONDS = 5.0


@pytest.fixture
def bus_root():
    """A scratch inbox directory short enough for the kernel's 107-byte path limit."""
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "bus-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real_trades(read_captured_payloads):
    """Real aggTrade messages off the Binance USDⓈ-M stream, as the venue sent them."""
    records = read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl")
    trades = []
    for _received_at_ns, raw in records:
        message = json.loads(raw)
        payload = message.get("data", message)
        if payload.get("e") == "aggTrade":
            trades.append(payload)
    assert len(trades) > 1, "the captured run must hold more than one trade to test a sequence"
    return trades


def declaration_for(part_id: str, consumes: tuple[str, ...], produces: tuple[str, ...]) -> PartDeclaration:
    return PartDeclaration(
        part_id=part_id,
        consumes=consumes,
        produces=produces,
        resource_class=ResourceClass.IO_BOUND,
        rate_risk=RateRisk.LATENCY_ONLY,
        skipped_tick_effect=SkippedTickEffect.DELAYS,
    )


def wiring_for(
    root: pathlib.Path,
    part_id: str,
    consumes: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    sends_to: dict[str, tuple[str, ...]] | None = None,
) -> PartWiring:
    """Build one part's plan by hand, the way derive_wiring would have."""
    sends_to = sends_to or {}
    return PartWiring(
        part_id=part_id,
        declaration=declaration_for(part_id, consumes, produces),
        inboxes={data_type: root / f"{part_id}.{data_type}" for data_type in consumes},
        outbound={
            data_type: tuple(root / f"{consumer}.{data_type}" for consumer in sends_to.get(data_type, ()))
            for data_type in produces
        },
    )


def open_bus(wiring: PartWiring, receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> PartBus:
    return PartBus(
        wiring=wiring,
        inbox_receive_buffer_bytes=receive_buffer_bytes,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )


def test_a_real_trade_arrives_as_it_was_published(bus_root, real_trades):
    consumer_wiring = wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",))
    producer_wiring = wiring_for(
        bus_root,
        "venue-trade-stream-reader",
        produces=("market-data",),
        sends_to={"market-data": ("feed-gap-detector",)},
    )
    with open_bus(consumer_wiring) as consumer, open_bus(producer_wiring) as producer:
        producer.publish("market-data", real_trades[:5])
        received = consumer.reader("market-data")()

    assert [message.payload for message in received] == real_trades[:5]
    assert [message.sequence for message in received] == [0, 1, 2, 3, 4]
    assert {message.producer_part_id for message in received} == {"venue-trade-stream-reader"}
    assert all(message.data_type == "market-data" for message in received)


def test_every_consumer_of_a_type_receives_it(bus_root, real_trades):
    consumers = ("feed-gap-detector", "feed-jump-detector", "tick-size-resolver")
    consumer_buses = [
        open_bus(wiring_for(bus_root, part_id, consumes=("market-data",))) for part_id in consumers
    ]
    producer = open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": consumers},
        )
    )
    try:
        producer.publish("market-data", real_trades[:3])
        for consumer in consumer_buses:
            assert len(consumer.reader("market-data")()) == 3
    finally:
        producer.close()
        for consumer in consumer_buses:
            consumer.close()


def test_a_consumer_that_stopped_reading_cannot_stall_its_producer(bus_root, real_trades):
    """RL-066 as arithmetic: the producer publishes far more than the buffer holds."""
    consumer = open_bus(
        wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",)),
        receive_buffer_bytes=SMALL_RECEIVE_BUFFER_BYTES,
    )
    producer = open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        )
    )
    try:
        flood = (real_trades * 200)[:2000]
        started = time.monotonic()
        producer.publish("market-data", flood)
        took = time.monotonic() - started
    finally:
        producer_standing = producer.standing()["outputs"]["market-data"]
        producer.close()
        consumer.close()

    assert took < PUBLISH_MUST_NOT_TAKE_LONGER_THAN_SECONDS, f"publishing blocked for {took:.1f}s"
    assert producer_standing["refused_by_a_full_buffer"] > 0, (
        "a consumer that never read should have made the producer drop, not wait"
    )
    assert producer_standing["delivered"] > 0
    assert producer_standing["withheld_from_a_consumer_that_is_off"] == 0, (
        "the consumer was on and behind -- that is not the same fact as being off"
    )


def test_a_part_that_never_started_is_off_not_behind(bus_root, real_trades):
    """And the producer stops paying a syscall for it after the first refusal.

    Measured on the first live run: 59 of market-data's 66 consumers were off, so
    89% of every publish was a syscall to nobody. The first send finds out; the
    rest are skipped until the address is due a re-probe. Both are counted, and
    neither is silent -- a message nobody wanted and a message nobody received are
    different facts.
    """
    producer = open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        )
    )
    try:
        producer.publish("market-data", real_trades[:3])
        standing = producer.standing()["outputs"]["market-data"]
    finally:
        producer.close()

    assert standing["withheld_from_a_consumer_that_is_off"] == 1
    assert standing["skipped_a_consumer_known_to_be_off"] == 2
    assert standing["delivered"] == 0
    assert standing["refused_by_a_full_buffer"] == 0


def test_a_part_that_exits_becomes_off_and_the_producer_sees_it(bus_root, real_trades):
    """T-3 through the bus: the address survives, and sending to it is refused.

    The consumer runs in a real child process, because the question is what happens
    when a process holding a bound socket goes away -- which cannot be asked of an
    object closed in the same interpreter without also deleting the socket file.
    """
    address = bus_root / "feed-gap-detector.market-data"
    ready_reader, ready_writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_reader)
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        bound.bind(str(address))
        os.write(ready_writer, b"bound")
        time.sleep(0.2)
        os._exit(0)

    os.close(ready_writer)
    assert os.read(ready_reader, len(b"bound")) == b"bound"
    os.close(ready_reader)

    producer = open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        )
    )
    try:
        producer.publish("market-data", real_trades[:1])
        while_on = producer.standing()["outputs"]["market-data"]["delivered"]

        os.waitpid(child, 0)
        assert address.exists(), "the socket file outlives the process, which is why binding unlinks"

        producer.publish("market-data", real_trades[:2])
        standing = producer.standing()["outputs"]["market-data"]
    finally:
        producer.close()

    assert while_on == 1
    # The first send after the part died finds out; the second is skipped until the
    # address is due a re-probe. Both mean off.
    assert standing["withheld_from_a_consumer_that_is_off"] == 1
    assert standing["skipped_a_consumer_known_to_be_off"] == 1
    assert standing["refused_by_a_full_buffer"] == 0


def test_a_consumer_counts_what_never_reached_it(bus_root, real_trades):
    """Loss is detected at the receiving end, by the gap in the producer's sequence."""
    consumer_wiring = wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",))
    with open_bus(consumer_wiring) as consumer:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        try:
            for sequence in (0, 1, 5, 6):  # 2, 3 and 4 never sent
                sender.sendto(
                    encode_frame(
                        data_type="market-data",
                        producer_part_id="venue-trade-stream-reader",
                        sequence=sequence,
                        published_at_ns=time.time_ns(),
                        payload=real_trades[0],
                        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
                    ),
                    str(consumer_wiring.inboxes["market-data"]),
                )
        finally:
            sender.close()

        received = consumer.reader("market-data")()
        standing = consumer.standing()["inputs"]["market-data"]
        loss = consumer.input_loss()

    assert len(received) == 4
    assert standing["messages_lost"] == 3
    assert standing["gaps_seen"] == 1
    assert standing["last_gap_producer_part_id"] == "venue-trade-stream-reader"
    assert loss == {"market-data": 3}


def test_two_producers_of_one_type_are_tracked_apart(bus_root, real_trades):
    """25 of 275 types have more than one producer; a gap belongs to one of them."""
    consumer_wiring = wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",))
    with open_bus(consumer_wiring) as consumer:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        address = str(consumer_wiring.inboxes["market-data"])
        try:
            for producer_part_id, sequences in (
                ("venue-trade-stream-reader", (0, 1, 2)),
                ("ccxt-venue-reader", (0, 4)),
            ):
                for sequence in sequences:
                    sender.sendto(
                        encode_frame(
                            data_type="market-data",
                            producer_part_id=producer_part_id,
                            sequence=sequence,
                            published_at_ns=time.time_ns(),
                            payload=real_trades[0],
                            maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
                        ),
                        address,
                    )
        finally:
            sender.close()
        consumer.reader("market-data")()
        standing = consumer.standing()["inputs"]["market-data"]

    assert standing["messages_lost"] == 3, "only the second producer skipped 1, 2 and 3"
    assert standing["last_gap_producer_part_id"] == "ccxt-venue-reader"


def test_a_frame_of_an_unknown_codec_is_refused_and_never_deserialised(bus_root, real_trades):
    consumer_wiring = wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",))
    with open_bus(consumer_wiring) as consumer:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        good = encode_frame(
            data_type="market-data",
            producer_part_id="venue-trade-stream-reader",
            sequence=0,
            published_at_ns=time.time_ns(),
            payload=real_trades[0],
            maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
        )
        from_the_future = bytes([CODEC_VERSION + 1]) + good[1:]
        try:
            sender.sendto(from_the_future, str(consumer_wiring.inboxes["market-data"]))
            sender.sendto(good, str(consumer_wiring.inboxes["market-data"]))
        finally:
            sender.close()
        received = consumer.reader("market-data")()
        standing = consumer.standing()["inputs"]["market-data"]

    assert len(received) == 1, "the refused frame must not reach the part"
    assert standing["frames_refused"] == 1
    assert standing["messages_received"] == 1


def test_a_truncated_frame_is_refused_rather_than_guessed():
    with pytest.raises(FrameRefused):
        decode_frame(bytes([CODEC_VERSION]))
    with pytest.raises(FrameRefused):
        decode_frame(bytes([CODEC_VERSION]) + b"not a pickle")


def test_a_payload_too_large_is_refused_and_counted_not_raised(bus_root):
    producer = open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        )
    )
    try:
        producer.publish("market-data", ["x" * (MAXIMUM_MESSAGE_BYTES + 1)])
        standing = producer.standing()["outputs"]["market-data"]
    finally:
        producer.close()

    assert standing["refused_too_large"] == 1
    assert standing["delivered"] == 0
    assert "state store" in producer._publisher.standing["market-data"].last_failure


def test_encode_refuses_a_payload_over_the_ceiling():
    with pytest.raises(MessageTooLarge):
        encode_frame(
            data_type="market-data",
            producer_part_id="venue-trade-stream-reader",
            sequence=0,
            published_at_ns=0,
            payload="x" * (MAXIMUM_MESSAGE_BYTES + 1),
            maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
        )


def test_a_part_cannot_read_or_publish_a_type_it_did_not_declare(bus_root):
    with open_bus(
        wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            consumes=("venue-standing",),
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        )
    ) as bus:
        with pytest.raises(KeyError):
            bus.reader("fill")
        with pytest.raises(KeyError):
            bus.publisher_for("fill")
        with pytest.raises(KeyError):
            bus.publish("fill", [])


def test_publishing_a_type_nothing_consumes_is_counted_not_lost_silently(bus_root, real_trades):
    producer = open_bus(
        wiring_for(bus_root, "venue-trade-stream-reader", produces=("market-data",), sends_to={})
    )
    try:
        producer.publish("market-data", real_trades[:2])
        standing = producer.standing()["outputs"]["market-data"]
    finally:
        producer.close()

    assert standing["published_with_no_listener"] == 2
    assert standing["delivered"] == 0


def test_the_bus_exposes_the_descriptors_a_part_waits_on(bus_root):
    with open_bus(
        wiring_for(bus_root, "feed-gap-detector", consumes=("market-data", "venue-standing"))
    ) as bus:
        assert len(bus.input_descriptors) == 2
        assert all(isinstance(descriptor, int) for descriptor in bus.input_descriptors)


def test_binding_unlinks_an_address_a_previous_run_left_behind(bus_root):
    """The socket file outlives its process, so a restart must not fail on EADDRINUSE."""
    address = bus_root / "feed-gap-detector.market-data"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    stale.bind(str(address))
    stale.close()  # leaves the file behind, as a crashed part would
    assert address.exists()

    with Inbox(
        part_id="feed-gap-detector",
        data_type="market-data",
        address=address,
        receive_buffer_bytes=DEFAULT_RECEIVE_BUFFER_BYTES,
    ) as inbox:
        assert inbox.drain() == ()


def test_a_bus_that_fails_half_way_through_binding_leaks_nothing(bus_root):
    """Six phase 0 tasks were sent back for leaking a handle; an inbox is worse."""
    too_long = "p" * 200
    wiring = PartWiring(
        part_id="feed-gap-detector",
        declaration=declaration_for("feed-gap-detector", ("market-data", too_long), ()),
        inboxes={
            "market-data": bus_root / "feed-gap-detector.market-data",
            too_long: bus_root / f"feed-gap-detector.{too_long}",
        },
        outbound={},
    )
    with pytest.raises(OSError):
        open_bus(wiring)


def test_a_message_reports_how_stale_it_is():
    message = Message(
        data_type="market-data",
        producer_part_id="venue-trade-stream-reader",
        sequence=0,
        published_at_ns=1_000_000_000,
        payload={},
    )
    assert message.staleness_seconds(now_ns=2_000_000_000) == pytest.approx(1.0)


def test_a_publisher_holds_one_descriptor_whatever_the_fan_out(bus_root):
    """The reason inboxes were chosen over a socketpair per edge."""
    many = tuple(f"consumer-{index}" for index in range(65))
    publisher = Publisher(
        part_id="venue-trade-stream-reader",
        outbound={"market-data": tuple(bus_root / f"{name}.market-data" for name in many)},
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    try:
        assert publisher._socket.fileno() > 0
    finally:
        publisher.close()


def test_an_address_that_was_off_is_probed_again_and_picked_up(bus_root, real_trades):
    """A part switched on must start receiving, or skipping absent consumers would
    turn a temporary absence into a permanent one."""
    consumer_wiring = wiring_for(bus_root, "feed-gap-detector", consumes=("market-data",))
    producer = PartBus(
        wiring=wiring_for(
            bus_root,
            "venue-trade-stream-reader",
            produces=("market-data",),
            sends_to={"market-data": ("feed-gap-detector",)},
        ),
        inbox_receive_buffer_bytes=DEFAULT_RECEIVE_BUFFER_BYTES,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
        absent_recheck_interval_seconds=0.0,  # due immediately, so the test is not a sleep
    )
    try:
        producer.publish("market-data", real_trades[:2])
        while_off = producer.standing()["outputs"]["market-data"]
        assert while_off["withheld_from_a_consumer_that_is_off"] >= 1

        with open_bus(consumer_wiring) as consumer:
            producer.publish("market-data", real_trades[:2])
            received = consumer.reader("market-data")()
    finally:
        producer.close()

    assert len(received) == 2, "a part that came on was never probed again"
