"""feed-gap-detector: a symbol gone silent, or a sequence that broke.

**It watches the broker feed as well as the venue feeds, since 2026-09-06.**
Until that date `detectors` was built only from `load_captured_venue_adapters`,
and a message from any other venue hit a `continue` on the next line -- so every
Upstox print was dropped in silence. The part read IDLE with its whole standing
keyed by `binance-usdm` and `bybit-linear`: it was watching two streams that
stopped on 2026-09-01 and was not watching the feed the segment bots actually
trade. Nothing would have noticed the Indian feed going quiet.

Upstox numbers nothing on its LTP stream, so the sequence half does not apply to
it and `SequenceContinuity.NOT_NUMBERED` is exactly the case the vocabulary
already carries -- "the venue numbers nothing on this stream, so silence is the
only detector". `BrokerFeedContinuity` says that and nothing else.

**Silence is judged against each symbol's own rhythm, not one flat threshold.**
`feed_gap_threshold` is 60 s and its own note says why that cannot be applied to
a full universe: *"an illiquid perpetual is quiet for minutes at a time"*. The
Indian universe is exactly that case at scale -- 1,974 subscribed instruments,
most of them option contracts that print every few minutes. This project has
already paid for that mistake once: with a flat floor,
`anomaly_feed_silence_patience_multiple`'s own note records **3,282 of 3,289
anomalies** being "this-venue-has-stopped-updating" on NSE contracts that were
merely quiet, each one halting trading in that symbol.

So the bound per symbol is `max(feed_gap_threshold, patience x that symbol's own
p99 inter-print gap)`, estimated online from the gaps this detector has itself
observed, and the stated floor alone until enough gaps have been seen for the
estimate to mean anything. That is the same rule `RollingWindow._gap_bound_seconds`
already applies to a price window, carrying the same measured multiple.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import NOT_SENT, StreamKind
from runtime.venues.venue_adapter import MessageFacts, SequenceContinuity, VenueAdapter

PART_ID = "feed-gap-detector"

PART_DECLARATION = PartDeclaration(
    part_id="feed-gap-detector",
    consumes=("market-data",),
    produces=("feed-gap", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SILENCE = "silence"
SEQUENCE_BREAK = "sequence-break"

# How many of a stream's own gaps to keep for the p99 the patience bound uses.
# Bounded for the reason `_StreamState` states; the figure matches the window
# length the price detectors reason over, so the estimate describes about the
# same recent stretch of the session the rest of the system does.
GAPS_REMEMBERED_PER_STREAM = 256


@dataclass(frozen=True)
class FeedGap:
    """One gap, naming both sides so a reader can tell what was missed."""

    venue_id: str
    symbol: str
    stream_kind: str
    reason: str
    detected_at_ns: int
    silent_for_seconds: float | None = None
    expected_sequence: int | None = None
    observed_sequence: int | None = None


@dataclass
class _StreamState:
    last_seen_monotonic: float
    last_sequence: int | None = None
    # This stream's own observed gaps, bounded. The patience bound is a p99 over
    # them, so an unbounded list would let an early quiet hour set the patience
    # for the rest of the session -- and 1,974 subscribed instruments times an
    # unbounded list is the unbounded-structure shape this project has been
    # bitten by more than once.
    recent_gaps_seconds: deque = field(default_factory=lambda: deque(maxlen=GAPS_REMEMBERED_PER_STREAM))


class BrokerFeedContinuity:
    """A broker feed's continuity rule: it numbers nothing.

    Shaped like the one thing `FeedGapDetector` asks a `VenueAdapter` for, and
    deliberately not a `VenueAdapter` -- Upstox is a broker, it has no venue
    adapter, and inventing one to satisfy a type would claim this feed answers
    order books and premiums it does not. `sequence_continuity` is the whole of
    what the detector reads, so this is the whole of what it needs to be.
    """

    def __init__(self, venue_id: str) -> None:
        self.venue_id = venue_id

    def sequence_continuity(self, stream_kind) -> SequenceContinuity:
        return SequenceContinuity.NOT_NUMBERED

    def read_previous_sequence(self, payload: bytes) -> int | None:
        return None


@dataclass
class GapStanding:
    messages_seen: int = 0
    gaps_found: int = 0
    silence_gaps: int = 0
    sequence_gaps: int = 0
    resyncs_seen: int = 0
    tracked_streams: int = 0
    last_gap: FeedGap | None = None


class FeedGapDetector:
    """Watches one venue's messages for silence and for broken sequences.

    Silence catches the routed-path trap: a connection that stays open and
    delivers nothing. The sequence check catches the loss the venue will not
    report -- Bybit's own SDK never compares update ids at all.
    """

    def __init__(
        self,
        adapter: VenueAdapter,
        feed_gap_threshold_seconds: float,
        silence_patience_multiple: float | None = None,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._adapter = adapter
        self._threshold = feed_gap_threshold_seconds
        # None keeps the flat threshold exactly as it was, which is what the two
        # crypto venues were measured against; a value turns on the per-symbol
        # bound the Indian universe needs. Opt-in rather than always-on, so no
        # existing measurement is silently reinterpreted.
        self._silence_patience_multiple = silence_patience_multiple
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._streams: dict[tuple[str, StreamKind], _StreamState] = {}
        self.standing = GapStanding()

    def observe(self, facts: MessageFacts) -> FeedGap | None:
        """Record one message; return a gap if its sequence broke."""
        self.standing.messages_seen += 1
        key = (facts.symbol, facts.stream_kind)
        state = self._streams.get(key)
        now = self._monotonic()
        if state is None:
            # A snapshot counts as a resync even when it is the first thing seen:
            # it is the venue restarting the numbering, and whether we happened
            # to be watching before does not change that.
            if facts.resets_sequence:
                self.standing.resyncs_seen += 1
            self._streams[key] = _StreamState(last_seen_monotonic=now, last_sequence=self._sequence_of(facts))
            self.standing.tracked_streams = len(self._streams)
            return None

        gap = None
        if facts.resets_sequence:
            self.standing.resyncs_seen += 1
        else:
            gap = self._check_sequence(facts, state)
        # This stream's own rhythm, measured from the arrivals themselves rather
        # than assumed. Recorded before last_seen is moved on, because the gap is
        # the distance between this arrival and the previous one.
        #
        # **An outage does not teach patience.** A gap already past the bound was
        # reported as a gap, so feeding it back into the estimate would let every
        # outage widen the very bound that caught it. That is not hypothetical
        # here: this part runs through the night, and the closed market is one
        # enormous gap every day. Measured on the captured tape for 2026-09-04,
        # a quiet NIFTY contract's p99 inter-print gap is 4,742 s across the
        # whole tape and 1,239 s in-session alone -- so an estimate that swallows
        # the overnight silence sets a 3.7-hour bound on a 6.25-hour session and
        # the detector goes blind for most of the day it is meant to watch.
        # Only ordinary quiet is evidence about ordinary quiet.
        elapsed = now - state.last_seen_monotonic
        if elapsed <= self.silence_bound_seconds(state):
            state.recent_gaps_seconds.append(elapsed)
        state.last_seen_monotonic = now
        state.last_sequence = self._sequence_of(facts) or state.last_sequence
        return gap

    def _sequence_of(self, facts: MessageFacts) -> int | None:
        return None if facts.sequence == NOT_SENT else int(facts.sequence)

    def _check_sequence(self, facts: MessageFacts, state: _StreamState) -> FeedGap | None:
        continuity = self._adapter.sequence_continuity(facts.stream_kind)
        observed = self._sequence_of(facts)
        if continuity is SequenceContinuity.NOT_NUMBERED or observed is None or state.last_sequence is None:
            return None

        expected = None
        if continuity is SequenceContinuity.INCREMENTS_BY_ONE:
            expected = state.last_sequence + 1
            broken = observed != expected
        elif continuity is SequenceContinuity.NON_DECREASING:
            broken = observed < state.last_sequence
            expected = state.last_sequence
        else:
            expected = state.last_sequence
            broken = False
        if not broken:
            return None
        return self._record(
            facts,
            SEQUENCE_BREAK,
            expected_sequence=expected,
            observed_sequence=observed,
        )

    def silence_bound_seconds(self, state: _StreamState) -> float:
        """How long THIS stream may be quiet before the quiet is a gap.

        The stated threshold alone until the stream has shown enough of its own
        rhythm to be measured against it -- an estimate from a handful of gaps
        would let one early pause set the patience, which is the same reasoning
        `RollingWindow._gap_bound_seconds` carries and the same multiple.
        """
        if self._silence_patience_multiple is None:
            return self._threshold
        gaps = state.recent_gaps_seconds
        if len(gaps) < max(2, GAPS_REMEMBERED_PER_STREAM // 2):
            return self._threshold
        ordered = sorted(gaps)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return max(self._threshold, self._silence_patience_multiple * p99)

    def check_for_silence(self) -> tuple[FeedGap, ...]:
        """Every tracked stream that has said nothing past its own bound."""
        now = self._monotonic()
        gaps = []
        for (symbol, stream_kind), state in self._streams.items():
            silent_for = now - state.last_seen_monotonic
            if silent_for <= self.silence_bound_seconds(state):
                continue
            state.last_seen_monotonic = now
            gaps.append(
                self._record(
                    MessageFacts(stream_kind=stream_kind, symbol=symbol),
                    SILENCE,
                    silent_for_seconds=silent_for,
                )
            )
        return tuple(gaps)

    def check_chained_predecessor(self, payload: bytes, facts: MessageFacts) -> FeedGap | None:
        """For a venue that names its predecessor, compare against what it claims."""
        if self._adapter.sequence_continuity(facts.stream_kind) is not SequenceContinuity.CHAINED_TO_PREVIOUS:
            return None
        claimed = self._adapter.read_previous_sequence(payload)
        state = self._streams.get((facts.symbol, facts.stream_kind))
        if claimed is None or state is None or state.last_sequence is None:
            return None
        if claimed == state.last_sequence:
            return None
        return self._record(
            facts, SEQUENCE_BREAK, expected_sequence=state.last_sequence, observed_sequence=claimed
        )

    def _record(self, facts: MessageFacts, reason: str, **detail) -> FeedGap:
        gap = FeedGap(
            venue_id=self._adapter.venue_id,
            symbol=facts.symbol,
            stream_kind=facts.stream_kind.name,
            reason=reason,
            detected_at_ns=self._now_ns(),
            **detail,
        )
        self.standing.gaps_found += 1
        if reason == SILENCE:
            self.standing.silence_gaps += 1
        else:
            self.standing.sequence_gaps += 1
        self.standing.last_gap = gap
        return gap


def describe_gaps(detector: FeedGapDetector) -> dict:
    standing = detector.standing
    return {
        "part_id": PART_ID,
        "venue_id": detector._adapter.venue_id,
        "messages_seen": standing.messages_seen,
        "tracked_streams": standing.tracked_streams,
        "gaps_found": standing.gaps_found,
        "silence_gaps": standing.silence_gaps,
        "sequence_gaps": standing.sequence_gaps,
        "resyncs_seen": standing.resyncs_seen,
        "last_gap": standing.last_gap.__dict__ if standing.last_gap else None,
    }


def describe_all_gaps(detectors: dict, unwatched: dict[str, int] | None = None) -> dict:
    """One standing across every venue this part watches, keyed so all survive.

    Continuity is a venue's property -- Binance numbers aggregate trades and
    Bybit does not -- so the live part holds one detector per venue, and a
    standing built from any single one would report a fraction of what it saw.
    """
    merged: dict = {"part_id": PART_ID, "venues": len(detectors)}
    totals = {"messages_seen": 0, "gaps_found": 0, "silence_gaps": 0, "sequence_gaps": 0}
    for venue_id, detector in sorted(detectors.items()):
        one = describe_gaps(detector)
        merged[f"messages_seen.{venue_id}"] = one["messages_seen"]
        merged[f"gaps_found.{venue_id}"] = one["gaps_found"]
        merged[f"tracked_streams.{venue_id}"] = one["tracked_streams"]
        merged[f"resyncs_seen.{venue_id}"] = one["resyncs_seen"]
        for name in totals:
            totals[name] += one[name]
    merged.update(totals)
    # Messages this part threw away because no detector was keyed to their
    # venue. Zero is the healthy reading and any other number names the venue,
    # so the failure that hid the Upstox feed here for five days is now a figure
    # on the board instead of a `continue` nobody could see (Rule 8).
    for venue_id, dropped in sorted((unwatched or {}).items()):
        merged[f"dropped_no_detector_for.{venue_id}"] = dropped
    merged["dropped_no_detector_for_total"] = sum((unwatched or {}).values())
    return merged


def run_feed_gap_detector(
    detector: FeedGapDetector, control_socket, read_messages, publish_gap,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for facts in read_messages():
            gap = detector.observe(facts)
            if gap is not None:
                publish_gap(gap)
        for gap in detector.check_for_silence():
            publish_gap(gap)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_gaps(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    One detector per captured venue, because continuity is a venue's property:
    Binance numbers aggregate trades and Bybit does not, and a detector that
    compared one venue's sequence against the other's rule would report gaps
    that never happened. Each trade on the bus becomes the facts the detector
    reads -- symbol, venue time, sequence -- keyed to its venue.
    """
    from runtime.input_assembly import Batch
    from runtime.part_context import RUNTIME_SCOPE
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    trades = Batch(read=context.bus.reader("market-data"))
    publish_gaps = context.bus.publisher_for("feed-gap")
    threshold = context.number("feed_gap_threshold")
    patience = context.number("feed_gap_patience_multiple")
    detectors = {
        adapter.venue_id: FeedGapDetector(
            adapter=adapter,
            feed_gap_threshold_seconds=threshold,
            silence_patience_multiple=patience,
        )
        for adapter in load_captured_venue_adapters(context.settings[RUNTIME_SCOPE])
    }
    # The broker feed, which is what the segment bots actually trade on. Added
    # 2026-09-06: without it every Upstox print hit the `continue` below and this
    # part watched only two streams that had already stopped. Built from the same
    # setting `broker-underlying-price-frame-bridge` stamps its frames with, so
    # the id this keys on is the id the bridge really publishes.
    broker_venue = str(context.setting("broker_feed_venue_id").value)
    detectors[broker_venue] = FeedGapDetector(
        adapter=BrokerFeedContinuity(broker_venue),
        feed_gap_threshold_seconds=threshold,
        silence_patience_multiple=patience,
    )

    self_standing_unwatched: dict[str, int] = {}

    def tick() -> None:
        found = []
        for trade in trades.payloads():
            detector = detectors.get(trade.venue_id)
            if detector is None:
                # A venue the operator has not turned on; not this part's call.
                # Counted rather than merely skipped: this silent `continue` is
                # what hid the Upstox feed from this detector for five days, and
                # a drop nobody counts is indistinguishable from a feed nobody
                # is sending (Rule 8).
                self_standing_unwatched[trade.venue_id] = (
                    self_standing_unwatched.get(trade.venue_id, 0) + 1
                )
                continue
            gap = detector.observe(
                MessageFacts(
                    stream_kind=StreamKind.TRADE,
                    symbol=trade.symbol,
                    venue_time_ns=trade.venue_time_ns,
                    sequence=trade.sequence,
                )
            )
            if gap is not None:
                found.append(gap)
        for detector in detectors.values():
            found.extend(detector.check_for_silence())
        if found:
            publish_gaps(found)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_all_gaps(detectors, self_standing_unwatched),
    )
