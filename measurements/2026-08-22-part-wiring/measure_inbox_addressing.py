"""Should a data-plane channel be a socketpair per edge, or an inbox per part?

The blueprint has 4 995 edges. Wiring each as its own socketpair costs 9 990
descriptors and gives the widest parts 342 of them each -- the eleven parts that
consume part-health would hold one receiving end per producing part, which is 321
descriptors to read one low-rate data type.

The alternative: each part binds one *inbox* per data type it consumes, at an
abstract-namespace address derived from its part id and that type. A producer holds
a single unconnected datagram socket and sends to the addresses of that type's
consumers. Descriptors then follow what a part *declares* -- at most 20 inboxes,
median 3 -- instead of following how popular its inputs happen to be.

Five questions decide it, and the last two are the interesting ones:

  1. Does an abstract-namespace datagram address carry messages at all, intact?
  2. Does a full destination still refuse instantly rather than block the producer?
  3. What does sendto cost against a connected send -- one socket doing 65 sends to
     65 addresses, against 65 sockets doing one each?
  4. Does the address disappear when the part's process exits? T-3 says off is
     genuinely off, and an address that outlived its process would be a channel
     that swallows data addressed to a part that is not there.
  5. What does a producer see when it publishes to a part that is off? That answer
     is the difference between "the governor turned it off" and "the data plane is
     broken", and the bus has to be able to tell them apart.

Run:  .venv/bin/python measurements/2026-08-22-part-wiring/measure_inbox_addressing.py
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import pickle
import socket
import statistics
import time

WIDEST_FAN_OUT_CONSUMERS = 65
REPEATS = 20_000
DRAIN_IDLE_TIMEOUT_SECONDS = 0.25
IDLE_TURNS_BEFORE_DRAIN_STOPS = 4
ABSTRACT_PREFIX = "\0ajit-segment-bots/"


def inbox_address(part_id: str, data_type: str) -> str:
    """Where a part receives one data type. Abstract namespace: no file, and the
    kernel releases the name when the last descriptor holding it closes."""
    return f"{ABSTRACT_PREFIX}{part_id}/{data_type}"


def open_inbox(part_id: str, data_type: str) -> socket.socket:
    inbox = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    inbox.bind(inbox_address(part_id, data_type))
    return inbox


def build_sample_message() -> bytes:
    return pickle.dumps(
        {
            "venue_id": "binance-usdm",
            "symbol": "BTCUSDT",
            "price": 64_123.5,
            "quantity": 0.037,
            "side": "buy",
            "venue_timestamp_ns": time.time_ns(),
            "sequence": 9_876_543_210,
        },
        protocol=pickle.HIGHEST_PROTOCOL,
    )


def measure_delivery_is_intact() -> dict:
    inbox = open_inbox("feed-gap-detector", "market-data")
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    with inbox, producer:
        blob = build_sample_message()
        producer.sendto(blob, inbox_address("feed-gap-detector", "market-data"))
        delivered = inbox.recv(len(blob) * 2)
        return {
            "bytes_sent": len(blob),
            "bytes_delivered": len(delivered),
            "identical": delivered == blob,
            "address_is_abstract": True,
        }


def measure_full_inbox_refuses_instantly() -> dict:
    inbox = open_inbox("feed-gap-detector", "market-data")
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    producer.setblocking(False)
    with inbox, producer:
        blob = build_sample_message()
        address = inbox_address("feed-gap-detector", "market-data")
        accepted = 0
        refusal = None
        while accepted < 100_000:
            try:
                producer.sendto(blob, address)
            except OSError as failure:
                refusal = f"{errno.errorcode.get(failure.errno, failure.errno)}"
                break
            accepted += 1
        costs = []
        for _ in range(1000):
            at = time.perf_counter()
            try:
                producer.sendto(blob, address)
            except OSError:
                pass
            costs.append(time.perf_counter() - at)
        return {
            "message_bytes": len(blob),
            "messages_accepted_before_refusal": accepted,
            "refusal": refusal,
            "refusal_microseconds_median": round(statistics.median(costs) * 1e6, 3),
        }


def measure_sendto_against_connected_send() -> dict:
    """One socket sending to 65 addresses, against 65 connected sockets."""
    inboxes = [open_inbox(f"consumer-{index}", "market-data") for index in range(WIDEST_FAN_OUT_CONSUMERS)]
    addresses = [inbox_address(f"consumer-{index}", "market-data") for index in range(WIDEST_FAN_OUT_CONSUMERS)]
    blob = build_sample_message()
    repeats = REPEATS // 10

    drain_child = os.fork()
    if drain_child == 0:
        import selectors

        selector = selectors.DefaultSelector()
        for inbox in inboxes:
            inbox.setblocking(False)
            selector.register(inbox, selectors.EVENT_READ)
        # Stop when the producer goes quiet, never on a message count: a count
        # assumes nothing was refused, and a drain that hangs waiting for a message
        # that was dropped would turn a finding into a hung measurement.
        idle_turns = 0
        try:
            while idle_turns < IDLE_TURNS_BEFORE_DRAIN_STOPS:
                ready = selector.select(timeout=DRAIN_IDLE_TIMEOUT_SECONDS)
                if not ready:
                    idle_turns += 1
                    continue
                idle_turns = 0
                for key, _events in ready:
                    try:
                        while True:
                            key.fileobj.recv(len(blob) * 2)
                    except BlockingIOError:
                        pass
        finally:
            os._exit(0)

    for inbox in inboxes:
        inbox.close()

    one_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    one_socket.setblocking(False)
    refused_sendto = 0
    started = time.perf_counter()
    for _ in range(repeats):
        for address in addresses:
            try:
                one_socket.sendto(blob, address)
            except OSError:
                refused_sendto += 1
    sendto_cost = (time.perf_counter() - started) / repeats
    one_socket.close()

    connected = []
    for address in addresses:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.connect(address)
        sender.setblocking(False)
        connected.append(sender)
    refused_connected = 0
    started = time.perf_counter()
    for _ in range(repeats):
        for sender in connected:
            try:
                sender.send(blob)
            except OSError:
                refused_connected += 1
    connected_cost = (time.perf_counter() - started) / repeats
    for sender in connected:
        sender.close()

    os.waitpid(drain_child, 0)
    return {
        "consumers": WIDEST_FAN_OUT_CONSUMERS,
        "descriptors_the_producer_held_for_sendto": 1,
        "descriptors_the_producer_held_when_connected": WIDEST_FAN_OUT_CONSUMERS,
        "sendto_microseconds_per_message": round(sendto_cost * 1e6, 3),
        "connected_send_microseconds_per_message": round(connected_cost * 1e6, 3),
        "sendto_messages_per_second": round(1 / sendto_cost),
        "sends_refused_sendto": refused_sendto,
        "sends_refused_connected": refused_connected,
    }


def measure_address_dies_with_the_part() -> dict:
    """T-3: when a part's process exits, its inbox must stop existing."""
    part_id = "off-state-verifier"
    data_type = "market-data"
    address = inbox_address(part_id, data_type)

    child = os.fork()
    if child == 0:
        inbox = open_inbox(part_id, data_type)
        try:
            time.sleep(0.5)
        finally:
            os._exit(0)

    time.sleep(0.2)
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    producer.setblocking(False)
    blob = build_sample_message()
    while_on = None
    try:
        producer.sendto(blob, address)
        while_on = "delivered"
    except OSError as failure:
        while_on = errno.errorcode.get(failure.errno, str(failure.errno))

    os.waitpid(child, 0)
    time.sleep(0.1)

    while_off = None
    try:
        producer.sendto(blob, address)
        while_off = "delivered"
    except OSError as failure:
        while_off = errno.errorcode.get(failure.errno, str(failure.errno))

    rebound = False
    try:
        rebind = open_inbox(part_id, data_type)
        rebound = True
        rebind.close()
    except OSError:
        rebound = False
    producer.close()

    return {
        "publish_while_the_part_is_on": while_on,
        "publish_after_the_part_exited": while_off,
        "address_reusable_after_exit": rebound,
    }


def measure_cost_of_publishing_to_an_off_part() -> dict:
    """A part that is off is the normal case, not an error. It must be cheap."""
    address = inbox_address("a-part-that-is-off", "market-data")
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    producer.setblocking(False)
    blob = build_sample_message()
    costs = []
    seen = None
    for _ in range(REPEATS // 10):
        at = time.perf_counter()
        try:
            producer.sendto(blob, address)
        except OSError as failure:
            seen = errno.errorcode.get(failure.errno, str(failure.errno))
        costs.append(time.perf_counter() - at)
    producer.close()
    return {
        "refusal": seen,
        "microseconds_median": round(statistics.median(costs) * 1e6, 3),
        "microseconds_p99": round(sorted(costs)[int(len(costs) * 0.99)] * 1e6, 3),
    }


def measure_filesystem_addressing() -> dict:
    """The same inbox, addressed by a socket file in a 0700 directory instead.

    The abstract namespace has no permissions at all: any process in the same
    network namespace can send to an abstract address, and this box has a second
    human user (uid 1000). Every message on this bus is deserialised by the part
    that receives it, so an address anyone can write to is an address anyone can
    hand a payload to. A socket file under XDG_RUNTIME_DIR carries the directory's
    mode, which is 0700 and enforced by the kernel.

    What that costs is cleanup: an abstract address disappears with its process,
    a socket file does not. So the two questions here are whether the off signal
    survives -- ECONNREFUSED from a file whose owner is gone -- and what a part
    must do to bind an address a previous run left behind.
    """
    runtime_directory = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "ajit-segment-bots" / "inboxes"
    runtime_directory.mkdir(parents=True, exist_ok=True)
    runtime_directory.chmod(0o700)
    address = str(runtime_directory / "off-state-verifier.market-data")
    blob = build_sample_message()

    child = os.fork()
    if child == 0:
        inbox = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        inbox.bind(address)
        try:
            time.sleep(0.5)
        finally:
            os._exit(0)

    time.sleep(0.2)
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    producer.setblocking(False)

    def publish() -> str:
        try:
            producer.sendto(blob, address)
            return "delivered"
        except OSError as failure:
            return errno.errorcode.get(failure.errno, str(failure.errno))

    while_on = publish()
    os.waitpid(child, 0)
    time.sleep(0.1)
    while_off = publish()
    file_left_behind = pathlib.Path(address).exists()

    rebind_without_unlink = None
    try:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        stale.bind(address)
        stale.close()
        rebind_without_unlink = "bound"
    except OSError as failure:
        rebind_without_unlink = errno.errorcode.get(failure.errno, str(failure.errno))

    pathlib.Path(address).unlink(missing_ok=True)
    rebind_after_unlink = None
    try:
        fresh = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        fresh.bind(address)
        while_on_again = publish()
        fresh.close()
        rebind_after_unlink = "bound"
    except OSError as failure:
        rebind_after_unlink = errno.errorcode.get(failure.errno, str(failure.errno))
        while_on_again = None

    costs = []
    for _ in range(REPEATS // 10):
        at = time.perf_counter()
        publish()
        costs.append(time.perf_counter() - at)
    producer.close()
    pathlib.Path(address).unlink(missing_ok=True)

    return {
        "directory": str(runtime_directory),
        "directory_mode": oct(runtime_directory.stat().st_mode & 0o777),
        "directory_filesystem": "tmpfs (a rendezvous point, not state)",
        "publish_while_the_part_is_on": while_on,
        "publish_after_the_part_exited": while_off,
        "socket_file_left_behind": file_left_behind,
        "rebind_without_unlink": rebind_without_unlink,
        "rebind_after_unlink": rebind_after_unlink,
        "publish_after_rebind": while_on_again,
        "publish_to_a_dead_address_microseconds_median": round(statistics.median(costs) * 1e6, 3),
    }


def main() -> None:
    print(
        json.dumps(
            {
                "delivery": measure_delivery_is_intact(),
                "full_inbox": measure_full_inbox_refuses_instantly(),
                "sendto_against_connected": measure_sendto_against_connected_send(),
                "address_lifetime": measure_address_dies_with_the_part(),
                "publishing_to_an_off_part": measure_cost_of_publishing_to_an_off_part(),
                "filesystem_addressing": measure_filesystem_addressing(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
