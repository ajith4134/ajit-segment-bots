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

import time
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

    **A key carries when it was last observed, and may carry a bound on how old
    that is allowed to be.** Without the bound a key holds its last value forever,
    which is right for a level that is true until it changes and wrong for one the
    market moves: on the live run of 2026-08-23 `position-sizer` priced an ENAUSDT
    order at a reference price fifty-six minutes stale, because input loss stopped
    that symbol's messages and this shape had no way to say so. With the bound, a
    key too old to believe is **absent** rather than old -- which makes every
    reader's existing "I have no value for this symbol" refusal the thing that
    fires, instead of asking 81 parts each to grow an age check of their own.

    The bound is opt-in per assembly because what counts as old belongs to the data
    type rather than to the shape: a hardware fact does not go stale in a minute and
    a price does. Where it is set it comes from a named setting with provenance,
    never a literal (RL-061).
    """

    read: Callable[[], tuple[Message, ...]]
    key_of: Callable[[object], Hashable]
    maximum_age_seconds: float | None = None
    _by_key: dict = None  # type: ignore[assignment]
    _observed_at_ns_by_key: dict = None  # type: ignore[assignment]
    _messages_seen: int = 0
    _fresh_keys: int = 0
    _stale_keys: int = 0

    def __post_init__(self) -> None:
        if self._by_key is None:
            self._by_key = {}
        if self._observed_at_ns_by_key is None:
            self._observed_at_ns_by_key = {}
        if self.maximum_age_seconds is not None and not self.maximum_age_seconds > 0:
            # A bound of zero expires everything including the message that just
            # arrived, and a negative one expires nothing; both read as "staleness
            # is handled" while doing the opposite, so neither is accepted.
            raise ValueError(
                "maximum_age_seconds must be a positive number of seconds, or None for a "
                f"level that never expires; got {self.maximum_age_seconds!r}"
            )

    def _take_in_what_arrived(self) -> None:
        for message in self.read():
            key = self.key_of(message.payload)
            self._by_key[key] = message.payload
            # The observation time is the arriving message's, so value and age
            # always describe the same message -- the same rule LatestValue keeps.
            self._observed_at_ns_by_key[key] = message.published_at_ns
            self._messages_seen += 1

    def mapping(self, now_ns: int | None = None) -> dict:
        """Every key's current value, after taking in whatever just arrived.

        With a bound set, a key whose value is older than it is left out. It is not
        forgotten: the symbol went quiet, it did not cease to exist, so the value
        comes back the moment a message for it does.
        """
        self._take_in_what_arrived()
        if self.maximum_age_seconds is None:
            self._fresh_keys = len(self._by_key)
            self._stale_keys = 0
            return dict(self._by_key)

        at = time.time_ns() if now_ns is None else now_ns
        oldest_believable_ns = at - int(self.maximum_age_seconds * 1e9)
        fresh = {
            key: payload
            for key, payload in self._by_key.items()
            if self._observed_at_ns_by_key.get(key, 0) >= oldest_believable_ns
        }
        self._fresh_keys = len(fresh)
        self._stale_keys = len(self._by_key) - len(fresh)
        return fresh

    def values(self, now_ns: int | None = None) -> tuple:
        return tuple(self.mapping(now_ns=now_ns).values())

    def observed_at_ns(self, key: Hashable) -> int | None:
        """When this key's current value was published, or None if never seen."""
        return self._observed_at_ns_by_key.get(key)

    def age_seconds(self, key: Hashable, now_ns: int | None = None) -> float | None:
        """How old this key's value is, or None if nothing has ever arrived for it.

        None is never zero on purpose: never seen and just seen are opposite facts,
        and a reader that treated the first as the second would be most confident
        exactly where it knows least.
        """
        observed = self._observed_at_ns_by_key.get(key)
        if observed is None:
            return None
        at = time.time_ns() if now_ns is None else now_ns
        return (at - observed) / 1e9

    def forget(self, key: Hashable) -> None:
        """Drop a key whose subject is gone -- a part that was switched off.

        Explicit because the alternative is a map that only grows: the governor
        would keep weighing the usage of parts that stopped existing.
        """
        self._by_key.pop(key, None)
        self._observed_at_ns_by_key.pop(key, None)

    @property
    def keys_seen(self) -> int:
        return len(self._by_key)

    @property
    def fresh_keys(self) -> int:
        """How many keys the last mapping() believed."""
        return self._fresh_keys

    @property
    def stale_keys(self) -> int:
        """How many keys the last mapping() withheld as too old to believe.

        Counted rather than dropped quietly: a refusal nobody can see is
        indistinguishable from an input that never came.
        """
        return self._stale_keys

    @property
    def messages_seen(self) -> int:
        return self._messages_seen


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
