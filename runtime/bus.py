"""The data plane: a part's inboxes, a part's publisher, and what a refusal means.

Addressed by data type, never by part (R-01, T-4): a part is handed callables over
this and cannot ask which parts are on the other end. The addresses come from
runtime.wiring_plan, which computes them from the blueprint.

Publishing never waits. Three outcomes, all measured on this box, none of which
blocks a producer:

    delivered       the message reached the consumer's queue     2.5 us
    EAGAIN          a buffer was full -- this message is lost    1.5 us
    ECONNREFUSED    the consumer is off; its process is gone     3.8 us
    ENOENT          the consumer is off; it never bound at all

That is RL-066 as arithmetic rather than as intent: a consumer that stops reading
cannot slow its producer, and cannot make a stream reader miss a tick on the wire.
It also means loss is real, so it is counted at both ends -- the producer counts
what it could not hand over, and the consumer detects what did not arrive, by the
gap in a per-producer sequence. The two are independent measurements of the same
event and are expected to agree.

**A producer cannot tell whose buffer was full.** Measured on this box: a burst of
4.7 million datagrams at 66 addresses returned EAGAIN for 96% of them while only 7
of those addresses were bound at all, so most were refused against the sending
socket's own buffer before any destination was resolved. The producer's counter is
therefore named for what it observed -- a full buffer -- and the consumer's sequence
gaps are what say whether that consumer lost anything.

What a consumer may do about loss is decided by the blueprint, not here: 223 parts
declare that a skipped tick corrupts their answer and 98 that it merely delays it.
A part in the first group reports its gaps through health rather than absorbing
them quietly (spec section 4).
"""

from __future__ import annotations

import errno
import os
import pickle
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from runtime.wiring_plan import PartWiring

# The framing version. A frame whose first byte is not this is refused and counted,
# never unpickled: a bus whose framing changed must say so rather than misread an
# old frame as a new one. A wire-format width, not a decision (RL-061).
CODEC_VERSION = 1
CODEC_VERSION_BYTES = 1
CODEC_BYTE_ORDER = "big"

# How long an address that refused is left alone before it is tried again. A
# default rather than a required argument because Publisher is also built directly
# in tests; the running system passes the operator's setting.
DEFAULT_ABSENT_RECHECK_SECONDS = 5.0

# Every part's own health is published under this type. It is a data type like any
# other -- same sockets, same refusals, same inboxes -- because a privileged health
# channel would be a second data plane and T-1 says there is one shape.
HEALTH_TYPE = "part-health"


class MessageTooLarge(ValueError):
    """A payload exceeded what the bus will carry.

    Not raised to be caught and worked around: anything this large belongs in a
    state store with a reference on the bus (runtime spec section 4). The bus does
    not grow a buffer to fit one message.
    """


class FrameRefused(ValueError):
    """A received frame could not be trusted, and was not deserialised."""


@dataclass(frozen=True)
class Message:
    """One item off the bus, with the facts a consumer needs about its journey."""

    data_type: str
    producer_part_id: str
    sequence: int
    published_at_ns: int
    payload: object

    def staleness_seconds(self, now_ns: int | None = None) -> float:
        at = time.time_ns() if now_ns is None else now_ns
        return (at - self.published_at_ns) / 1e9


@dataclass
class PublishStanding:
    """What one producer's sends actually did, per data type.

    Kept per type rather than in one total because the three outcomes mean
    different things and a single 'sent' number would hide all of it.
    """

    delivered: int = 0
    refused_by_a_full_buffer: int = 0
    withheld_from_a_consumer_that_is_off: int = 0
    skipped_a_consumer_known_to_be_off: int = 0
    refused_too_large: int = 0
    published_with_no_listener: int = 0
    last_failure: str | None = None

    def outcome_counts(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "refused_by_a_full_buffer": self.refused_by_a_full_buffer,
            "withheld_from_a_consumer_that_is_off": self.withheld_from_a_consumer_that_is_off,
            "skipped_a_consumer_known_to_be_off": self.skipped_a_consumer_known_to_be_off,
            "refused_too_large": self.refused_too_large,
            "published_with_no_listener": self.published_with_no_listener,
        }


@dataclass
class InputStanding:
    """What one inbox received, and what it can prove it did not receive."""

    messages_received: int = 0
    messages_lost: int = 0
    gaps_seen: int = 0
    frames_refused: int = 0
    last_gap_producer_part_id: str | None = None
    last_refusal: str | None = None
    highest_sequence_by_producer: dict[str, int] = field(default_factory=dict)


def encode_frame(
    data_type: str,
    producer_part_id: str,
    sequence: int,
    published_at_ns: int,
    payload: object,
    maximum_message_bytes: int,
) -> bytes:
    """One datagram: a version byte the reader checks before it trusts anything else."""
    body = pickle.dumps(
        (data_type, producer_part_id, sequence, published_at_ns, payload),
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    frame = CODEC_VERSION.to_bytes(CODEC_VERSION_BYTES, CODEC_BYTE_ORDER) + body
    if len(frame) > maximum_message_bytes:
        raise MessageTooLarge(
            f"a '{data_type}' message from '{producer_part_id}' is {len(frame)} bytes and the bus "
            f"carries at most {maximum_message_bytes}. A payload this large belongs in a state "
            f"store with a reference published in its place -- the bus does not grow to fit one "
            f"message, because the size that fits it is the size every inbox then reserves."
        )
    return frame


def decode_frame(frame: bytes) -> Message:
    """Read one datagram, refusing anything whose framing it does not recognise.

    This unpickles, and that is admissible only because of where the inbox lives:
    the address is a socket file under XDG_RUNTIME_DIR at mode 0700, so the set of
    processes that can send to it is the set of processes running as this user --
    which is the parts themselves. The codec and the address are one decision
    (spec section 2.3); an inbox that ever moves to the abstract namespace, or to a
    directory anyone else can write, must stop unpickling on the same commit.

    Measured alternatives were rejected on cost, not on principle: pickle
    serialises a normalised trade in 2.29 us against a bus carrying 285 messages a
    second, so nothing here is fast because it is unsafe.
    """
    if len(frame) <= CODEC_VERSION_BYTES:
        raise FrameRefused(f"a frame of {len(frame)} bytes carries no payload")
    version = int.from_bytes(frame[:CODEC_VERSION_BYTES], CODEC_BYTE_ORDER)
    if version != CODEC_VERSION:
        raise FrameRefused(
            f"frame codec version {version}, this bus speaks {CODEC_VERSION}. The frame was not "
            f"deserialised: a bus whose framing changed says so rather than misreading an old "
            f"frame as a new one."
        )
    try:
        data_type, producer_part_id, sequence, published_at_ns, payload = pickle.loads(
            frame[CODEC_VERSION_BYTES:]
        )
    except Exception as failure:  # a malformed frame is one message, not the part's life
        raise FrameRefused(f"{type(failure).__name__}: {failure}") from failure
    return Message(
        data_type=data_type,
        producer_part_id=producer_part_id,
        sequence=sequence,
        published_at_ns=published_at_ns,
        payload=payload,
    )


class Publisher:
    """One socket, whatever the fan-out.

    Measured: sending to 65 consumers from one unconnected socket costs 2.52 us per
    send against 2.10 us with 65 connected sockets -- 20% more, for a producer that
    holds one descriptor instead of 65 and a descriptor count that follows what a
    part declares rather than how popular its outputs happen to be.
    """

    def __init__(
        self,
        part_id: str,
        outbound: dict[str, tuple],
        maximum_message_bytes: int,
        absent_recheck_interval_seconds: float = DEFAULT_ABSENT_RECHECK_SECONDS,
        now_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        send_buffer_bytes: int | None = None,
    ) -> None:
        self._part_id = part_id
        self._outbound = {data_type: tuple(str(a) for a in addresses) for data_type, addresses in outbound.items()}
        self._maximum_message_bytes = maximum_message_bytes
        self._absent_recheck_interval_seconds = absent_recheck_interval_seconds
        self._now_ns = now_ns
        self._monotonic = monotonic
        # When each address that refused may be tried again. An off part is the
        # normal case, not an error -- most of a running system is off by design --
        # and a producer that kept paying a syscall per message per absent consumer
        # would spend most of its budget on parts that are not there. Measured live:
        # 59 of 66 market-data consumers were off, so 89% of every send was to
        # nobody, and the sends that mattered were refused against a full buffer.
        self._retry_absent_at: dict[str, float] = {}
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        # One socket fans out to every consumer, and every datagram in flight is
        # charged to this socket's send buffer until its consumer reads it. At the
        # kernel default (212992 bytes, about 250 datagrams) one consumer that is
        # 250 messages behind fills the buffer for all of them, and every further
        # send returns EAGAIN whichever consumer it was for. Measured on the live
        # spine of 2026-08-23: thirteen market-data consumers each lost the same
        # 250 messages a second, which is the signature of the sender's buffer,
        # not of thirteen equally slow readers. The kernel caps this at
        # net.core.wmem_max without CAP_NET_ADMIN; a larger request is clamped
        # silently, so the size actually granted is read back and kept.
        if send_buffer_bytes is not None:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(send_buffer_bytes))
        self.send_buffer_bytes = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        self._next_sequence: dict[str, int] = {data_type: 0 for data_type in self._outbound}
        self.standing: dict[str, PublishStanding] = {
            data_type: PublishStanding() for data_type in self._outbound
        }

    def publish(self, data_type: str, items: Iterable[object]) -> None:
        """Send each item to every consumer of the type. Never blocks, never raises
        because a consumer is off or behind -- both are facts about the consumer,
        counted here and read off the board, not exceptions for a producer to handle.
        """
        if data_type not in self._outbound:
            raise KeyError(
                f"'{self._part_id}' does not declare that it produces '{data_type}'. The blueprint "
                f"decides what a part publishes -- a design change is a blueprint edit first."
            )
        addresses = self._outbound[data_type]
        standing = self.standing[data_type]
        for item in items:
            sequence = self._next_sequence[data_type]
            self._next_sequence[data_type] = sequence + 1
            try:
                frame = encode_frame(
                    data_type=data_type,
                    producer_part_id=self._part_id,
                    sequence=sequence,
                    published_at_ns=self._now_ns(),
                    payload=item,
                    maximum_message_bytes=self._maximum_message_bytes,
                )
            except MessageTooLarge as refusal:
                standing.refused_too_large += 1
                standing.last_failure = str(refusal)
                continue
            if not addresses:
                standing.published_with_no_listener += 1
                continue
            now = self._monotonic()
            for address in addresses:
                self._send_one(frame, address, standing, now)

    def _send_one(self, frame: bytes, address: str, standing: PublishStanding, now: float) -> None:
        retry_at = self._retry_absent_at.get(address)
        if retry_at is not None:
            if now < retry_at:
                # Known off, and not yet due a re-probe. Counted, never silent: the
                # board must be able to tell a message nobody wanted from a message
                # nobody received.
                standing.skipped_a_consumer_known_to_be_off += 1
                return
            del self._retry_absent_at[address]
        try:
            self._socket.sendto(frame, address)
        except BlockingIOError:
            # EAGAIN, and the producer cannot tell which buffer was full. Measured
            # on this box: bursting 4.7 million datagrams at 66 addresses returned
            # EAGAIN for 96% of them while only 7 of those addresses were bound at
            # all -- so most were refused against this socket's own send buffer
            # before a destination was ever resolved, not against a consumer's
            # queue. The counter is named for what was observed rather than for a
            # cause that cannot be told apart from here; which it was is answered by
            # the consumer's own sequence gaps.
            standing.refused_by_a_full_buffer += 1
        except (FileNotFoundError, ConnectionRefusedError):
            # ENOENT: the part has never bound this address in this boot. ECONNREFUSED:
            # the socket file is there and its process is gone. Both mean off, not
            # broken -- the distinction the whole design rests on -- and both put the
            # address on the re-probe clock so a part switched on is picked up within
            # one interval rather than never.
            standing.withheld_from_a_consumer_that_is_off += 1
            self._retry_absent_at[address] = now + self._absent_recheck_interval_seconds
        except OSError as failure:
            standing.last_failure = f"{errno.errorcode.get(failure.errno, failure.errno)}: {failure}"
        else:
            standing.delivered += 1
            self._retry_absent_at.pop(address, None)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> Publisher:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()


class Inbox:
    """Where one part receives one data type, and how it knows what it missed.

    Binding is unlink-then-bind: the address is a socket file and outlives the
    process that bound it. A part unlinks only its own address, and only the
    launcher unlinks one for a part that is not running.
    """

    def __init__(self, part_id: str, data_type: str, address, receive_buffer_bytes: int) -> None:
        self.part_id = part_id
        self.data_type = data_type
        self.address = str(address)
        self.standing = InputStanding()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer_bytes)
        self._receive_size = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        try:
            try:
                os.unlink(self.address)
            except FileNotFoundError:
                pass
            self._socket.bind(self.address)
        except OSError:
            # An inbox that could not bind still holds a descriptor. It owns that
            # socket and nothing else will close it: PartBus can only close the
            # inboxes it has already been handed, and this one never got that far.
            self._socket.close()
            raise

    def fileno(self) -> int:
        return self._socket.fileno()

    def drain(self) -> tuple[Message, ...]:
        """Everything waiting, right now. Never blocks, never waits for more."""
        received = []
        while True:
            try:
                frame = self._socket.recv(self._receive_size)
            except BlockingIOError:
                break
            try:
                message = decode_frame(frame)
            except FrameRefused as refusal:
                self.standing.frames_refused += 1
                self.standing.last_refusal = str(refusal)
                continue
            self._account_for_sequence(message)
            self.standing.messages_received += 1
            received.append(message)
        return tuple(received)

    def _account_for_sequence(self, message: Message) -> None:
        """Detect what did not arrive.

        The producer's own EAGAIN count says how many sends it could not complete;
        this says how many messages never reached this inbox. They are independent
        measurements of the same loss, which is why both are kept.
        """
        highest = self.standing.highest_sequence_by_producer.get(message.producer_part_id)
        if highest is not None and message.sequence > highest + 1:
            self.standing.messages_lost += message.sequence - highest - 1
            self.standing.gaps_seen += 1
            self.standing.last_gap_producer_part_id = message.producer_part_id
        if highest is None or message.sequence > highest:
            self.standing.highest_sequence_by_producer[message.producer_part_id] = message.sequence

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> Inbox:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()


class PartBus:
    """Everything one part can reach, and nothing about who is at the other end.

    A part is handed readers and a publisher built over this. It cannot enumerate
    its peers, because nothing here will tell it: `reader` takes a data type, and
    `publish` takes a data type. That is R-01 and T-4 made structural rather than
    remembered.
    """

    def __init__(
        self,
        wiring: PartWiring,
        inbox_receive_buffer_bytes: int,
        maximum_message_bytes: int,
        absent_recheck_interval_seconds: float = DEFAULT_ABSENT_RECHECK_SECONDS,
        now_ns: Callable[[], int] = time.time_ns,
        publisher_send_buffer_bytes: int | None = None,
    ) -> None:
        self.part_id = wiring.part_id
        self._declaration = wiring.declaration
        self._inboxes: dict[str, Inbox] = {}
        try:
            for data_type, address in wiring.inboxes.items():
                self._inboxes[data_type] = Inbox(
                    part_id=wiring.part_id,
                    data_type=data_type,
                    address=address,
                    receive_buffer_bytes=inbox_receive_buffer_bytes,
                )
            self._publisher = Publisher(
                part_id=wiring.part_id,
                outbound=wiring.outbound,
                maximum_message_bytes=maximum_message_bytes,
                absent_recheck_interval_seconds=absent_recheck_interval_seconds,
                now_ns=now_ns,
                send_buffer_bytes=publisher_send_buffer_bytes,
            )
        except Exception:
            # A part that fails half-way through binding must not leave sockets open:
            # six phase 0 tasks were sent back for leaking a handle, and an inbox
            # leaked here would hold an address the part no longer serves.
            self.close()
            raise

    @property
    def input_descriptors(self) -> tuple[int, ...]:
        """The fds a part waits on, so it wakes on data rather than only on its timer."""
        return tuple(inbox.fileno() for inbox in self._inboxes.values())

    def reader(self, data_type: str) -> Callable[[], tuple[Message, ...]]:
        """A callable that drains one inbox. This is what a part's read_* is built on."""
        if data_type not in self._inboxes:
            raise KeyError(
                f"'{self.part_id}' does not declare that it consumes '{data_type}'. A part reads "
                f"what the blueprint says it reads -- there is no other way to reach the bus."
            )
        return self._inboxes[data_type].drain

    def payloads(self, data_type: str) -> tuple:
        """Just the payloads, for a part that does not care about the journey."""
        return tuple(message.payload for message in self.reader(data_type)())

    def publish(self, data_type: str, items: Iterable[object]) -> None:
        self._publisher.publish(data_type, items)

    def publisher_for(self, data_type: str) -> Callable[[Iterable[object]], None]:
        """A callable bound to one type. This is what a part's publish_* is built on."""
        if data_type not in self._publisher.standing:
            raise KeyError(f"'{self.part_id}' does not declare that it produces '{data_type}'")
        return lambda items: self._publisher.publish(data_type, items)

    def input_loss(self) -> dict[str, int]:
        """Per consumed type, how many messages this part can prove it never got.

        Reported through health, because a part that lost input and said nothing is
        a part reporting an answer it cannot support -- and 223 parts declare that a
        skipped tick corrupts theirs.
        """
        return {
            data_type: inbox.standing.messages_lost
            for data_type, inbox in self._inboxes.items()
            if inbox.standing.messages_lost
        }

    def standing(self) -> dict:
        """Everything the board needs about this part's wiring, all of it measured."""
        return {
            "part_id": self.part_id,
            "inputs": {
                data_type: {
                    "messages_received": inbox.standing.messages_received,
                    "messages_lost": inbox.standing.messages_lost,
                    "gaps_seen": inbox.standing.gaps_seen,
                    "frames_refused": inbox.standing.frames_refused,
                    "last_gap_producer_part_id": inbox.standing.last_gap_producer_part_id,
                }
                for data_type, inbox in self._inboxes.items()
            },
            "outputs": {
                data_type: standing.outcome_counts()
                for data_type, standing in self._publisher.standing.items()
            },
            "skipped_tick_effect": str(self._declaration.skipped_tick_effect),
        }

    def close(self) -> None:
        for inbox in getattr(self, "_inboxes", {}).values():
            inbox.close()
        publisher = getattr(self, "_publisher", None)
        if publisher is not None:
            publisher.close()

    def __enter__(self) -> PartBus:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()
