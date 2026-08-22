"""Turning a stream of messages into the shape a part's logic expects.

The bus delivers messages; a part's logic wants a state of the world. Those are not
the same thing, and the gap between them is where a part would otherwise grow its
own quiet cache with its own quiet bugs.

Three shapes cover what the parts in this blueprint actually ask for:

    LatestValue    one current reading -- hardware capacity, a memory forecast
    LatestByKey    the current reading per key -- usage per part, priority per part
    Batch          everything that arrived since the last tick -- trades, fills

The distinction is not cosmetic. A part that treats a level (the machine has 12
cores) as an event will act once and then forget; a part that treats an event (a
fill happened) as a level will act on the same fill forever. Which one a data type
is belongs with the part that reads it, so it is stated at the point of assembly.

**Never seen is not the same as empty**, and every shape here keeps them apart:
`LatestValue.value()` returns None until something arrives, and the planner it
feeds refuses to plan on None rather than planning against a zero.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass

from runtime.bus import Message


@dataclass
class LatestValue:
    """The most recent payload of one data type, kept across ticks.

    For a level: something that is true until it changes. The value survives a tick
    in which nothing arrived, because the machine still has the cores it had.
    """

    read: Callable[[], tuple[Message, ...]]
    _payload: object | None = None
    _observed_at_ns: int | None = None
    _messages_seen: int = 0

    def value(self) -> object | None:
        """The current reading, or None if nothing has ever arrived."""
        for message in self.read():
            self._payload = message.payload
            self._observed_at_ns = message.published_at_ns
            self._messages_seen += 1
        return self._payload

    @property
    def has_been_seen(self) -> bool:
        return self._messages_seen > 0

    @property
    def observed_at_ns(self) -> int | None:
        """When the current value was published, so a reader can judge staleness."""
        return self._observed_at_ns


@dataclass
class LatestByKey:
    """The most recent payload per key -- usage per part, priority per part.

    key_of names the field that identifies which thing a message is about. A part
    keyed by part_id is not naming a peer: it is grouping measurements it was sent,
    and it learns the keys from the messages rather than from the blueprint.
    """

    read: Callable[[], tuple[Message, ...]]
    key_of: Callable[[object], Hashable]
    _by_key: dict = None  # type: ignore[assignment]
    _messages_seen: int = 0

    def __post_init__(self) -> None:
        if self._by_key is None:
            self._by_key = {}

    def mapping(self) -> dict:
        """Every key's current value, after taking in whatever just arrived."""
        for message in self.read():
            self._by_key[self.key_of(message.payload)] = message.payload
            self._messages_seen += 1
        return dict(self._by_key)

    def values(self) -> tuple:
        return tuple(self.mapping().values())

    def forget(self, key: Hashable) -> None:
        """Drop a key whose subject is gone -- a part that was switched off.

        Explicit because the alternative is a map that only grows: the governor
        would keep weighing the usage of parts that stopped existing.
        """
        self._by_key.pop(key, None)

    @property
    def keys_seen(self) -> int:
        return len(self._by_key)


@dataclass
class Batch:
    """Everything that arrived since the last tick, and nothing older.

    For an event: something that happened once. Returning it twice would make a
    part act on the same fill again, which is the failure this shape exists to
    prevent -- so the batch is emptied by reading it.
    """

    read: Callable[[], tuple[Message, ...]]
    _messages_seen: int = 0

    def payloads(self) -> tuple:
        messages = self.read()
        self._messages_seen += len(messages)
        return tuple(message.payload for message in messages)

    def messages(self) -> tuple[Message, ...]:
        """The batch with its envelopes, for a part that needs the producer or the age."""
        messages = self.read()
        self._messages_seen += len(messages)
        return messages

    @property
    def messages_seen(self) -> int:
        return self._messages_seen
