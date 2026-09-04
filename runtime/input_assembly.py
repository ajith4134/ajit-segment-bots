"""Turning a stream of messages into the shape a part's logic expects.

The bus delivers messages; a part's logic wants a state of the world. Those are not
the same thing, and the gap between them is where a part would otherwise grow its
own quiet cache with its own quiet bugs.

Four shapes cover what the parts in this blueprint actually ask for:

    LatestValue    one current reading -- hardware capacity, a memory forecast
    LatestByKey    the current reading per key -- usage per part, priority per part
    LatestStatementBySource
                   the newest complete set a source published -- the risk limits
                   one limiter holds at once, where an entry it stops making has
                   been withdrawn rather than left unrepeated
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

    def take_in_what_arrived(self) -> None:
        """Drain whatever is on the bus into this level, and nothing else.

        Public and separate from `mapping()` because draining the bus and
        snapshotting the table are different costs, and a part that can only pay
        the second one ends up pacing the first. `broker-market-feed-reader`
        read its listings only through `values()`, which copies the whole table
        (up to 101,393 entries), so it paced that call to once every 60 seconds
        -- and thereby paced the drain too. `broker-instrument-catalogue-reader`
        restates all 102,940 listings repeatedly, so the bounded bus buffer
        overflowed between drains: measured 2026-09-04, the feed reader had
        received 1,067 listings of 102,940, and the three index underlyings the
        segment is entirely about were not among them.

        No age bound is applied here. A key too old for `mapping()` is still
        taken in, so it is current again the moment it is restated rather than
        having been dropped on the floor.
        """
        self._take_in_what_arrived()

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
class LatestStatementBySource:
    """Every entry of the newest complete statement each source made.

    For a source that speaks in *sets* rather than in single values. One tick of
    `event-risk-limiter` is one risk limit for whatever affects the whole book plus
    one per symbol with an event in force, and that whole set is its current word:
    an entry it stops making has been withdrawn, not merely left unrepeated.

    `LatestByKey` can hold one half of that or the other, never both, and both
    halves were live defects on 2026-08-26:

        keyed by the source        the last entry of a statement erases the rest,
                                   so one limiter's set collapses to one limit
        keyed by source and entry  an entry the source stops making is never
                                   replaced by anything and binds forever --
                                   halt-enforcer's scoped zero outliving the
                                   all-clear it publishes with no scope at all,
                                   which is a different key

    So a statement is identified by its own stamp rather than by its entries. An
    arrival stamped later than what a source has said replaces that source's whole
    set; one stamped the same joins it; one stamped earlier is dropped, which is
    what makes the shape safe against a statement split across two reads or
    arriving out of order. The bus sends one datagram per item (`Publisher.publish`
    loops over `items`), so a set published in one call is not a set that arrives
    in one read, and a shape that assumed otherwise would silently hold half a
    statement.

    **The stamp is the source's own decision time, not the arrival time.** Two
    entries decided together carry one stamp because the part that decided them
    stamped them once; arrival times differ per datagram and would split every
    statement into as many statements as it has entries.

    `maximum_age_seconds` bounds the hold exactly as `LatestByKey` does, and for
    the same reason: a source that dies stops having a current word. It is measured
    against the newest message that carried the statement.
    """

    read: Callable[[], tuple[Message, ...]]
    source_of: Callable[[object], Hashable]
    stamp_of: Callable[[object], int]
    entry_of: Callable[[object], Hashable]
    maximum_age_seconds: float | None = None
    _entries_by_source: dict = None  # type: ignore[assignment]
    _stamp_by_source: dict = None  # type: ignore[assignment]
    _observed_at_ns_by_source: dict = None  # type: ignore[assignment]
    _messages_seen: int = 0
    _statements_replaced: int = 0
    _arrivals_already_superseded: int = 0
    _fresh_sources: int = 0
    _stale_sources: int = 0

    def __post_init__(self) -> None:
        if self._entries_by_source is None:
            self._entries_by_source = {}
        if self._stamp_by_source is None:
            self._stamp_by_source = {}
        if self._observed_at_ns_by_source is None:
            self._observed_at_ns_by_source = {}
        if self.maximum_age_seconds is not None and not self.maximum_age_seconds > 0:
            # The same refusal LatestByKey makes: a bound of zero expires the
            # message that just arrived and a negative one expires nothing, and
            # both read as "staleness is handled" while doing the opposite.
            raise ValueError(
                "maximum_age_seconds must be a positive number of seconds, or None for a "
                f"statement that never expires; got {self.maximum_age_seconds!r}"
            )

    def _take_in_what_arrived(self) -> None:
        for message in self.read():
            payload = message.payload
            source = self.source_of(payload)
            stamp = self.stamp_of(payload)
            standing_stamp = self._stamp_by_source.get(source)
            if standing_stamp is not None and stamp < standing_stamp:
                # A datagram from a statement this source has already superseded.
                # Counted rather than applied: putting it back would resurrect an
                # entry the source has withdrawn.
                self._arrivals_already_superseded += 1
                self._messages_seen += 1
                continue
            if standing_stamp is None or stamp > standing_stamp:
                self._entries_by_source[source] = {}
                self._stamp_by_source[source] = stamp
                # A new statement is observed now, not as recently as the one it
                # replaced: taking the later of the two would let a statement
                # published under a clock that stepped backwards inherit the age
                # of the one before it, which is the staleness bound reading its
                # own history instead of the message in front of it.
                self._observed_at_ns_by_source[source] = message.published_at_ns
                if standing_stamp is not None:
                    self._statements_replaced += 1
            else:
                # Another entry of the statement already held. The statement is as
                # fresh as its newest datagram.
                self._observed_at_ns_by_source[source] = max(
                    message.published_at_ns, self._observed_at_ns_by_source.get(source, 0)
                )
            self._entries_by_source[source][self.entry_of(payload)] = payload
            self._messages_seen += 1

    def mapping(self, now_ns: int | None = None) -> dict:
        """Every entry of every source's current statement, keyed by source and entry.

        With a bound set, a source whose statement is older than it is left out
        whole -- half a statement is not a statement, and a source that has gone
        quiet has no current word rather than an old one.
        """
        self._take_in_what_arrived()
        sources = self._entries_by_source
        if self.maximum_age_seconds is not None:
            at = time.time_ns() if now_ns is None else now_ns
            oldest_believable_ns = at - int(self.maximum_age_seconds * 1e9)
            sources = {
                source: entries
                for source, entries in self._entries_by_source.items()
                if self._observed_at_ns_by_source.get(source, 0) >= oldest_believable_ns
            }
        self._fresh_sources = len(sources)
        self._stale_sources = len(self._entries_by_source) - len(sources)
        return {
            (source, entry): payload
            for source, entries in sources.items()
            for entry, payload in entries.items()
        }

    def values(self, now_ns: int | None = None) -> tuple:
        return tuple(self.mapping(now_ns=now_ns).values())

    def statement_of(self, source: Hashable) -> tuple:
        """One source's current statement, in the order its entries arrived."""
        return tuple(self._entries_by_source.get(source, {}).values())

    def observed_at_ns(self, source: Hashable) -> int | None:
        """When the newest message of this source's statement was published."""
        return self._observed_at_ns_by_source.get(source)

    def forget(self, source: Hashable) -> None:
        """Drop a source that is gone, so the map does not only grow."""
        self._entries_by_source.pop(source, None)
        self._stamp_by_source.pop(source, None)
        self._observed_at_ns_by_source.pop(source, None)

    @property
    def sources_seen(self) -> int:
        return len(self._entries_by_source)

    @property
    def fresh_sources(self) -> int:
        """Sources whose statement was believed on the last mapping()."""
        return self._fresh_sources

    @property
    def stale_sources(self) -> int:
        """Sources whose statement was too old to believe on the last mapping()."""
        return self._stale_sources

    @property
    def statements_replaced(self) -> int:
        return self._statements_replaced

    @property
    def arrivals_already_superseded(self) -> int:
        """Messages dropped for belonging to a statement their source has replaced."""
        return self._arrivals_already_superseded

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
