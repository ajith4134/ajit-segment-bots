"""Measure the data-plane transport candidate: an AF_UNIX SOCK_DGRAM socketpair.

The wiring phase has to pick one transport for the data plane. The runtime spec
(section 3) already measured serialisation -- 1.53 us for a pickled bar over a
pipe, 0.78 us through a shared-memory ring -- and stated the rule that the ring is
for parts that are genuinely hot, because choosing it everywhere would be
optimisation with no measurement behind it.

This script measures the four properties that decide whether a datagram socketpair
can carry this system's real traffic:

  1. Message boundaries survive, and how large one message may be.
  2. A full receiver makes the sender drop rather than wait (RL-066: scarcity is
     answered instantly, never by a queue).
  3. What one publish costs, and what fanning one message out to N consumers costs
     -- market-data has 65 consumers in the blueprint.
  4. What it costs to select over a part's input fds; the widest part in the
     blueprint consumes 20 data types.

Run:  .venv/bin/python measurements/2026-08-22-part-wiring/measure_datagram_channel.py
"""

from __future__ import annotations

import json
import os
import pickle
import selectors
import socket
import statistics
import threading
import time
from dataclasses import dataclass

# The blueprint's own shape, measured from docs/features.json on 2026-08-22.
WIDEST_FAN_OUT_CONSUMERS = 65      # market-data
WIDEST_PART_INPUT_TYPES = 20       # the part that consumes the most types
MEASURED_TAPE_MESSAGES_PER_SECOND = 285.3

REPEATS = 20_000
SELECT_REPEATS = 20_000
GROWTH_STEP_BYTES = 4096
MAX_PROBE_BYTES = 8 << 20


@dataclass(frozen=True)
class NormalisedTrade:
    """A realistic payload: what a stream reader publishes per message."""

    venue_id: str
    symbol: str
    price: float
    quantity: float
    side: str
    venue_timestamp_ns: int
    received_at_ns: int
    sequence: int


def build_sample_payload() -> NormalisedTrade:
    return NormalisedTrade(
        venue_id="binance-usdm",
        symbol="BTCUSDT",
        price=64_123.5,
        quantity=0.037,
        side="buy",
        venue_timestamp_ns=time.time_ns(),
        received_at_ns=time.time_ns(),
        sequence=9_876_543_210,
    )


def measure_largest_deliverable_datagram() -> dict:
    """Find the largest single message the pair carries, before and after growing buffers."""

    def largest_for(send_buffer_bytes: int | None) -> dict:
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with sender, receiver:
            if send_buffer_bytes is not None:
                sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer_bytes)
                receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, send_buffer_bytes)
            applied_send = sender.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            applied_receive = receiver.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            largest = 0
            size = GROWTH_STEP_BYTES
            while size <= MAX_PROBE_BYTES:
                try:
                    sender.send(b"\0" * size)
                except OSError:
                    break
                delivered = receiver.recv(size + 1)
                if len(delivered) != size:
                    break
                largest = size
                size *= 2
            return {
                "so_sndbuf": applied_send,
                "so_rcvbuf": applied_receive,
                "largest_delivered_bytes": largest,
            }

    return {"default": largest_for(None), "grown": largest_for(MAX_PROBE_BYTES)}


def measure_overflow_is_a_drop_not_a_wait() -> dict:
    """A sender whose consumer stopped reading must fail immediately, never block."""
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with sender, receiver:
        sender.setblocking(False)
        payload = pickle.dumps(build_sample_payload(), protocol=pickle.HIGHEST_PROTOCOL)
        accepted = 0
        refusal = None
        started = time.perf_counter()
        while accepted < 1_000_000:
            try:
                sender.send(payload)
            except BlockingIOError as blocked:
                refusal = f"{type(blocked).__name__}: errno {blocked.errno}"
                break
            except OSError as failure:
                refusal = f"{type(failure).__name__}: errno {failure.errno}"
                break
            accepted += 1
        seconds_to_fill = time.perf_counter() - started
        # How long the refusal itself takes -- this is the number that decides
        # whether a blocked consumer can stall a producer.
        refusal_costs = []
        for _ in range(1000):
            at = time.perf_counter()
            try:
                sender.send(payload)
            except OSError:
                pass
            refusal_costs.append(time.perf_counter() - at)
        return {
            "payload_bytes": len(payload),
            "messages_buffered_before_refusal": accepted,
            "refusal": refusal,
            "seconds_to_fill": round(seconds_to_fill, 6),
            "refusal_microseconds_median": round(statistics.median(refusal_costs) * 1e6, 3),
            "refusal_microseconds_p99": round(sorted(refusal_costs)[int(len(refusal_costs) * 0.99)] * 1e6, 3),
        }


def measure_publish_cost() -> dict:
    """One publish: serialise once, send once, receive once."""
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with sender, receiver:
        payload = build_sample_payload()
        blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        size = len(blob) + 1

        started = time.perf_counter()
        for _ in range(REPEATS):
            sender.send(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
            pickle.loads(receiver.recv(size))
        round_trip = (time.perf_counter() - started) / REPEATS

        started = time.perf_counter()
        for _ in range(REPEATS):
            pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        serialise = (time.perf_counter() - started) / REPEATS

        return {
            "payload_bytes": len(blob),
            "publish_and_consume_microseconds": round(round_trip * 1e6, 3),
            "serialise_microseconds": round(serialise * 1e6, 3),
            "messages_per_second": round(1 / round_trip),
        }


def measure_fan_out_cost() -> dict:
    """Serialise once, send to every consumer of the type -- the widest is 65."""
    pairs = [socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM) for _ in range(WIDEST_FAN_OUT_CONSUMERS)]
    senders = [pair[0] for pair in pairs]
    receivers = [pair[1] for pair in pairs]
    try:
        payload = build_sample_payload()
        blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        repeats = REPEATS // 10
        started = time.perf_counter()
        for _ in range(repeats):
            one = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
            for sender in senders:
                sender.send(one)
            for receiver in receivers:
                receiver.recv(len(blob) + 1)
        per_message = (time.perf_counter() - started) / repeats
        return {
            "consumers": WIDEST_FAN_OUT_CONSUMERS,
            "fan_out_microseconds_per_message": round(per_message * 1e6, 3),
            "messages_per_second": round(1 / per_message),
            "measured_tape_rate_messages_per_second": MEASURED_TAPE_MESSAGES_PER_SECOND,
            "headroom_multiple": round((1 / per_message) / MEASURED_TAPE_MESSAGES_PER_SECOND, 1),
        }
    finally:
        for sender, receiver in pairs:
            sender.close()
            receiver.close()


def measure_select_cost() -> dict:
    """What a part pays to wait on its inputs. The widest consumes 20 types."""
    pairs = [socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM) for _ in range(WIDEST_PART_INPUT_TYPES)]
    selector = selectors.DefaultSelector()
    try:
        for index, (_, receiver) in enumerate(pairs):
            selector.register(receiver, selectors.EVENT_READ, index)
        blob = pickle.dumps(build_sample_payload(), protocol=pickle.HIGHEST_PROTOCOL)
        started = time.perf_counter()
        for turn in range(SELECT_REPEATS):
            sender = pairs[turn % WIDEST_PART_INPUT_TYPES][0]
            sender.send(blob)
            for key, _events in selector.select(timeout=1.0):
                key.fileobj.recv(len(blob) + 1)
        per_turn = (time.perf_counter() - started) / SELECT_REPEATS
        return {
            "input_types": WIDEST_PART_INPUT_TYPES,
            "selector": type(selector).__name__,
            "select_and_read_microseconds": round(per_turn * 1e6, 3),
        }
    finally:
        selector.close()
        for sender, receiver in pairs:
            sender.close()
            receiver.close()




def measure_sender_only_fan_out() -> dict:
    """What the producer alone pays to fan one message out to 65 consumers.

    The consumers must be separate *processes*, not threads: a drain thread in the
    producer's own interpreter contends for the GIL and reports a producer cost
    that no real producer pays. Measured both ways here, an in-process drain thread
    put the producer at 531 us per message against 91 us with the drain in a forked
    child -- the difference is contention this system does not have, because every
    part is its own process (section 1 of the runtime spec).

    Three numbers, because they bound the answer:
      - drained: consumers keeping up, every send delivered. The real case.
      - stalled: no consumer reading, so every send after the buffer fills is a
        refusal. Cheaper per call, and the case where data is being lost.
      - refusals counted, so a "fast" result that was fast because it delivered
        nothing cannot be mistaken for a good one.
    """
    payload = build_sample_payload()
    blob_bytes = len(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
    repeats = REPEATS // 10

    def fan_out_once(senders: list[socket.socket]) -> int:
        one = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        refused = 0
        for sender in senders:
            try:
                sender.send(one)
            except BlockingIOError:
                refused += 1
        return refused

    pairs = [socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM) for _ in range(WIDEST_FAN_OUT_CONSUMERS)]
    drain_child = os.fork()
    if drain_child == 0:  # the consumers: one process doing nothing but reading
        for sender, _receiver in pairs:
            sender.close()
        selector = selectors.DefaultSelector()
        for _sender, receiver in pairs:
            receiver.setblocking(False)
            selector.register(receiver, selectors.EVENT_READ)
        seen = 0
        try:
            while seen < repeats * WIDEST_FAN_OUT_CONSUMERS:
                for key, _events in selector.select(timeout=5.0):
                    try:
                        while True:
                            key.fileobj.recv(blob_bytes + 1)
                            seen += 1
                    except BlockingIOError:
                        pass
        finally:
            os._exit(0)

    senders = []
    for sender, receiver in pairs:
        receiver.close()
        sender.setblocking(False)
        senders.append(sender)
    refused_while_drained = 0
    started = time.perf_counter()
    for _ in range(repeats):
        refused_while_drained += fan_out_once(senders)
    drained_per_message = (time.perf_counter() - started) / repeats
    for sender in senders:
        sender.close()
    os.waitpid(drain_child, 0)

    stalled_pairs = [socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM) for _ in range(WIDEST_FAN_OUT_CONSUMERS)]
    stalled_senders = []
    for sender, _receiver in stalled_pairs:
        sender.setblocking(False)
        stalled_senders.append(sender)
    refused_while_stalled = 0
    started = time.perf_counter()
    for _ in range(repeats):
        refused_while_stalled += fan_out_once(stalled_senders)
    stalled_per_message = (time.perf_counter() - started) / repeats
    for sender, receiver in stalled_pairs:
        sender.close()
        receiver.close()

    sends_attempted = repeats * WIDEST_FAN_OUT_CONSUMERS
    return {
        "consumers": WIDEST_FAN_OUT_CONSUMERS,
        "sends_attempted": sends_attempted,
        "producer_microseconds_per_message": round(drained_per_message * 1e6, 3),
        "producer_microseconds_per_send": round(drained_per_message * 1e6 / WIDEST_FAN_OUT_CONSUMERS, 3),
        "producer_messages_per_second": round(1 / drained_per_message),
        "headroom_multiple": round((1 / drained_per_message) / MEASURED_TAPE_MESSAGES_PER_SECOND, 1),
        "sends_refused_while_drained": refused_while_drained,
        "producer_microseconds_when_every_consumer_stalled": round(stalled_per_message * 1e6, 3),
        "sends_refused_while_stalled": refused_while_stalled,
    }


def main() -> None:
    findings = {
        "largest_datagram": measure_largest_deliverable_datagram(),
        "overflow_behaviour": measure_overflow_is_a_drop_not_a_wait(),
        "publish_cost": measure_publish_cost(),
        "fan_out_cost": measure_fan_out_cost(),
        "sender_only_fan_out": measure_sender_only_fan_out(),
        "select_cost": measure_select_cost(),
    }
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
