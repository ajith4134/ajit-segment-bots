"""feed-gap-detector: a symbol gone silent, or a sequence that broke."""

from __future__ import annotations

import time
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
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._adapter = adapter
        self._threshold = feed_gap_threshold_seconds
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

    def check_for_silence(self) -> tuple[FeedGap, ...]:
        """Every tracked stream that has said nothing past the threshold."""
        now = self._monotonic()
        gaps = []
        for (symbol, stream_kind), state in self._streams.items():
            silent_for = now - state.last_seen_monotonic
            if silent_for <= self._threshold:
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


def describe_all_gaps(detectors: dict) -> dict:
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
    detectors = {
        adapter.venue_id: FeedGapDetector(adapter=adapter, feed_gap_threshold_seconds=threshold)
        for adapter in load_captured_venue_adapters(context.settings[RUNTIME_SCOPE])
    }

    def tick() -> None:
        found = []
        for trade in trades.payloads():
            detector = detectors.get(trade.venue_id)
            if detector is None:
                continue  # a venue the operator has not turned on; not this part's call
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
        read_standing=lambda: describe_all_gaps(detectors),
    )
