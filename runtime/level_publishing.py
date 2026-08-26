"""Publishing a level: on the bus only when it has actually changed.

`runtime.input_assembly` states the distinction on the read side -- a level is
something that is true until it changes, an event is something that happened --
and gives a reader a shape for each. This is the missing half of that pair. A
part holding a level had no shape at all for saying so, and so 23 of them ended
a tick with an unconditional `publish(thing.read_all())`: the level went onto
the bus again on every tick, unchanged, forever.

**Measured, 2026-08-26, on the live spine.** Every part publishes health once a
second, so a part consuming `part-health` is woken 327 times a second. Each of
those wakes ran a full tick, and each tick republished a level nobody had
changed:

    failing-part-detector    26,575 msg/s published from    283 msg/s received
    part-restart-budgeter    17,106 msg/s published from  6,963 msg/s received
    signal-excursion-profiler 2,311 msg/s published from      0 msg/s received

89,747 messages a second across the spine, load average 28.7 on twelve cores,
and seven parts silent because they could not get the CPU to send the heartbeat
that would have proved they were alive. The fault storm was self-sustaining:
starved parts miss heartbeats, the detector republishes the fault every tick,
the warden escalates every fault, the budgeter republishes every budget, and all
of that is CPU the starved part needed to report itself healthy.

**The publish is skipped, never the work.** A part still ticks, still observes
its inputs, still recomputes its level. What this refuses is putting an answer
onto the bus that is byte-for-byte the answer already there. Nothing downstream
can tell the difference between a level it was told twice and a level it was
told once, which is precisely why sending it twice is free to be dropped.

**A level is still refreshed on a timer, and that is not a contradiction.** Two
readers need it. One that starts after the level last changed has never seen it
and would wait forever for a change that already happened; and a reader bounding
its inputs by `maximum_age_seconds` -- which every `LatestByKey` here should --
would age out a level that is still true. So an unchanged level is republished
once per refresh interval. Change-triggered with a keepalive, not pure edge:
pure edge is how a restarted consumer starves, and this project has already paid
for that shape once, in a window that started empty because nothing told it what
it had missed.

**Comparison is on the encoded bytes, not on the objects.** Payloads here are
frozen dataclasses, tuples of them, and dicts; `==` is not defined usefully
across all of them and `is` is defined uselessly for all of them. The bus
already pickles every payload, so encoding once to compare costs what the bus
was about to spend anyway, and only the digest is retained -- holding the last
payload itself would double a part's memory for the largest thing it publishes.

**What counts as the same level is the caller's to say, through `identity_of`.**
Most payloads compare correctly whole, and that is the default. Some carry a
field that restates when the part noticed rather than what it found: a `PartFault`
embeds `detected_at_ns`, an `observations` count, and a `detail` string reading
"5 tick(s) with no error of any kind" whose number climbs on every tick. Compared
whole, no two of those are ever equal and nothing is ever skipped -- the storm
survives the fix while the counters claim it did not. So a part whose payload
mixes the finding with the noticing states which fields are the finding. The
narrower identity is the risk to weigh: a field left out of it is a field whose
change will not be published until the refresh interval comes round, so leave out
only what genuinely says "still true", never anything a reader acts on.
"""

from __future__ import annotations

import hashlib
import pickle
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

# Digest width. Sixteen bytes of BLAKE2b to decide "is this the same level as
# last time". A collision publishes nothing when it should have published once,
# and the refresh interval publishes it anyway within the second -- so the cost
# of a collision is bounded at one interval of staleness, not at a lost update.
DIGEST_BYTES = 16


def digest_of(items: object) -> bytes:
    """A stable fingerprint of what is about to be published.

    Pickled with the same protocol the bus uses, so two payloads that encode
    identically on the wire fingerprint identically here. A payload that cannot
    be pickled cannot be published either, so the failure surfaces at the same
    place it always would.
    """
    encoded = pickle.dumps(items, protocol=pickle.HIGHEST_PROTOCOL)
    return hashlib.blake2b(encoded, digest_size=DIGEST_BYTES).digest()


def whole_payload(items):
    """The default identity: two levels are the same when they encode the same.

    Named rather than inlined as a lambda so a part reading its own construction
    can see which identity it chose, and so the two publishers here share one
    definition of the default instead of two that could drift.
    """
    return items


@dataclass
class LevelStanding:
    """What a level publisher did, so a part's health can carry it.

    `unchanged_publishes_skipped` is the whole point of this class and belongs on
    health: a part whose skip count is not climbing is a part whose level really
    is changing every tick, which is a finding about that part rather than a
    fault in this one.
    """

    publishes: int = 0
    unchanged_publishes_skipped: int = 0
    refreshes: int = 0
    changes: int = 0


@dataclass
class LevelPublisher:
    """Publishes a level when it changes, and once per refresh interval regardless.

    Wraps the publisher a part already has, so a part adopting this changes one
    line and keeps its produces contract exactly as the blueprint declares it
    (R-01): what goes onto the wire is unchanged, only how often.

    Empty is a value like any other. A part whose level becomes "nothing" must
    say so once -- a reader holding the last non-empty answer would otherwise act
    on a level that has been withdrawn -- so the first empty publish goes out and
    subsequent identical empties do not.
    """

    publish: Callable[[Iterable[object]], None]
    refresh_interval_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    identity_of: Callable[[object], object] = whole_payload
    standing: LevelStanding = field(default_factory=LevelStanding)
    _last_digest: bytes | None = None
    _last_published_at: float | None = None

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds <= 0.0:
            raise ValueError(
                f"refresh_interval_seconds must be positive -- it is how long an unchanged "
                f"level may go unsaid before it is said again -- got "
                f"{self.refresh_interval_seconds!r}. Zero or negative republishes on every "
                f"tick, which is the defect this class exists to remove, and it would do it "
                f"while claiming to have removed it."
            )

    def publish_level(self, items) -> bool:
        """Put this level on the bus if it is new or due. Returns whether it went.

        The return value is for the caller's own standing, not for control flow:
        a part that behaves differently depending on whether its unchanged level
        was republished has made the refresh interval part of its logic, which is
        the level/event confusion again one layer up.
        """
        items = tuple(items)
        now = self.monotonic()
        digest = digest_of(self.identity_of(items))

        has_changed = digest != self._last_digest
        is_due = (
            self._last_published_at is None
            or now - self._last_published_at >= self.refresh_interval_seconds
        )

        if not has_changed and not is_due:
            self.standing.unchanged_publishes_skipped += 1
            return False

        self.publish(items)
        self.standing.publishes += 1
        if has_changed:
            self.standing.changes += 1
        else:
            self.standing.refreshes += 1
        self._last_digest = digest
        self._last_published_at = now
        return True


def describe_level_publishing(publishers: dict[str, LevelPublisher]) -> dict:
    """The level counters, flattened for a part's standing.

    Named by data type so a part publishing two levels can be read apart, and
    summed as well so a part's total skip rate is one number on a board.
    """
    described: dict[str, float] = {}
    total_skipped = 0
    total_published = 0
    for data_type, publisher in sorted(publishers.items()):
        described[f"{data_type}_published"] = float(publisher.standing.publishes)
        described[f"{data_type}_unchanged_skipped"] = float(
            publisher.standing.unchanged_publishes_skipped
        )
        total_skipped += publisher.standing.unchanged_publishes_skipped
        total_published += publisher.standing.publishes
    described["levels_published"] = float(total_published)
    described["unchanged_publishes_skipped"] = float(total_skipped)
    return described


@dataclass
class LevelPublisherByKey:
    """One level per key, each published when its own value changes or is due.

    For a part whose output is one message per subject rather than one message
    describing everything: a fault per part, a budget per part, an outage per
    venue. Keying matters because the alternative is a single digest over the
    whole set, and then one part changing state republishes every other part's
    level too -- which is the same storm with one more step in it.

    Each key keeps its own refresh clock, so the refreshes of many keys spread
    themselves across the interval instead of arriving as one burst: the keys are
    first said at the moments their subjects first differ, and nothing here ever
    lines them up again.

    A key that stops being offered simply stops being published. `forget` is for
    a subject that is gone for good -- a part switched off, a venue removed --
    and exists so the dictionary does not grow with the run: without it a key is
    remembered forever, which is the unbounded-structure shape this system has
    already been bitten by more than once.
    """

    publish: Callable[[Iterable[object]], None]
    refresh_interval_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    identity_of: Callable[[object], object] = whole_payload
    standing: LevelStanding = field(default_factory=LevelStanding)
    _by_key: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds <= 0.0:
            raise ValueError(
                f"refresh_interval_seconds must be positive -- it is how long an unchanged "
                f"level may go unsaid before it is said again -- got "
                f"{self.refresh_interval_seconds!r}. Zero or negative republishes on every "
                f"tick, which is the defect this class exists to remove, and it would do it "
                f"while claiming to have removed it."
            )

    def publish_level(self, key, items) -> bool:
        """Put this key's level on the bus if it is new or due. Returns whether it went."""
        publisher = self._by_key.get(key)
        if publisher is None:
            publisher = LevelPublisher(
                publish=self.publish,
                refresh_interval_seconds=self.refresh_interval_seconds,
                monotonic=self.monotonic,
                identity_of=self.identity_of,
                standing=self.standing,
            )
            self._by_key[key] = publisher
        return publisher.publish_level(items)

    def forget(self, key) -> None:
        """This subject is gone; stop remembering what was last said about it."""
        self._by_key.pop(key, None)

    @property
    def keys_held(self) -> int:
        return len(self._by_key)


@dataclass
class PacedPublisher:
    """A snapshot published on a cadence, not on a tick.

    The third shape, and the one a change check cannot serve. Some levels carry
    measurements that really do differ every time they are read: a heartbeat table
    holds each part's message counts and the age of its last report, so every
    field in it moves on every tick and `LevelPublisher` would correctly find a
    change and correctly publish it, every tick, forever.

    What such a level needs is a rate, stated once. A snapshot is worth having
    often enough to be current and no oftener; the reader of a heartbeat table
    cannot use a table taken a millisecond after the last one for anything the
    last one did not already answer.

    Measured on the live spine at 10:14 on 2026-08-26: `heartbeat-collector` was
    the busiest process on the machine at 73% of a core, because its tick woke on
    every one of 327 parts' health messages and each wake rebuilt a 327-row table
    (4.2 ms), rendered it (1.3 ms) and wrote 209 KB of JSON to disk (4.2 ms). Just
    under ten milliseconds of work, seventeen times a second, to restate a table
    whose consumers read it once a second.

    This is deliberately not a queue and deliberately not a buffer: a snapshot
    that arrives late is worse than one that was never taken, so what falls
    between two publishes is dropped rather than held. That is RL-066 -- scarcity
    is never answered by a queue.
    """

    publish: Callable[[Iterable[object]], None]
    interval_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    standing: LevelStanding = field(default_factory=LevelStanding)
    _last_published_at: float | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0.0:
            raise ValueError(
                f"interval_seconds must be positive -- it is how often a snapshot is "
                f"taken -- got {self.interval_seconds!r}. Zero or negative publishes on "
                f"every tick, which is the defect this class exists to remove."
            )

    def is_due(self) -> bool:
        """Whether a snapshot is due, so a caller can skip building one that is not.

        Separate from `publish_snapshot` because building the snapshot is the
        expensive half: the heartbeat table costs 4.2 ms to assemble and 1.3 ms to
        render, and a publisher that only refused to send it would still pay both.
        """
        return (
            self._last_published_at is None
            or self.monotonic() - self._last_published_at >= self.interval_seconds
        )

    def publish_snapshot(self, items) -> bool:
        """Put this snapshot on the bus if one is due. Returns whether it went."""
        if not self.is_due():
            self.standing.unchanged_publishes_skipped += 1
            return False
        self.publish(tuple(items))
        self.standing.publishes += 1
        self.standing.refreshes += 1
        self._last_published_at = self.monotonic()
        return True
