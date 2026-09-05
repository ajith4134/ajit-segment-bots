"""tail-crowding-detector: whether everyone is already in the trade we are joining.

A tailgater is by construction late, and its one structural risk is that the
thing making a move visible is the crowd that has already taken it. Crowding is
not a small negative on a good setup; it inverts the setup, because the exit that
ends a crowded move is the same exit for everyone in it.

Two independent readings, from sources that crowd differently:

- **The book.** A one-sided book with thin resting size behind the move is a
  crowd that has already lifted the other side. It shows crowding first and is
  the noisiest.
- **Order flow.** How lopsided today's buy vs. sell quantity is across an
  underlying's option chain (`broker-open-interest`) is the price of
  consensus in a market that doesn't charge one directly -- the honest
  Indian analogue of funding rate's role, not a guess (2026-09-01,
  options-segment-bots conversion). It lags the book and is harder to fake.

**Sentiment retired with no replacement** (2026-09-01) -- no Indian
sentiment data source exists yet, and inventing one would be exactly the
fabricated-to-fit-a-checker move this project refuses elsewhere.

**Any one of them extreme is a refusal, not half of one.** Averaging two
crowding readings is how a crowded trade gets taken on the strength of the
source that had not caught up yet. The reading names which source tripped,
because they have different remedies: a crowded book clears in minutes, and
crowded order flow does not.

**"Not measured" is its own state**, never a low reading (Rule 8). A symbol
nobody could read is not an uncrowded symbol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CROWDED, CROWDING_NOT_MEASURED as NOT_MEASURED, FROM_ORDER_FLOW,
    FROM_THE_BOOK, NOT_CROWDED, CrowdingReading,
)
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-crowding-detector"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-crowding-detector",
    consumes=(
        "follow-candidate", "order-book-snapshot", "broker-subscribed-instrument-listing",
        "broker-open-interest",
    ),
    produces=("crowding-reading", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

@dataclass
class DetectorStanding:
    readings: int = 0
    crowded: int = 0
    not_measured: int = 0
    by_tripping_source: dict = field(default_factory=dict)
    sources_unavailable: dict = field(default_factory=dict)


class TailCrowdingDetector:
    """Reads crowding from two sources and refuses on either of them."""

    def __init__(
        self,
        book_imbalance_threshold: float,
        order_flow_deviation_threshold: float,
        minimum_observations: int,
        half_life_observations: float,
        minimum_sources: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < book_imbalance_threshold <= 1.0:
            raise ValueError("book imbalance is in [-1, 1]; its threshold must be inside (0, 1]")
        if minimum_sources < 1:
            raise ValueError(
                "a reading from no source is not a reading; at least one must be available"
            )
        self._book_threshold = book_imbalance_threshold
        self._order_flow_threshold = order_flow_deviation_threshold
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._minimum_sources = minimum_sources
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], tuple] = {}
        self._order_flow: dict[tuple[str, str], float] = {}
        self._order_flow_moments: dict[tuple[str, str], RunningMoments] = {}
        self.standing = DetectorStanding()

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        self._books[(venue_id, symbol)] = (tuple(bids), tuple(asks))

    def observe_order_flow(self, venue_id: str, symbol: str, imbalance: float) -> None:
        """Buy quantity less sell quantity over their total, for one underlying's
        option chain -- the price of consensus in a market with no funding rate."""
        key = (venue_id, symbol)
        self._order_flow[key] = imbalance
        self._moments(self._order_flow_moments, key).observe(imbalance)

    def read(self, candidate) -> CrowdingReading:
        self.standing.readings += 1
        key = (candidate.venue_id, candidate.symbol)
        long_move = candidate.direction == "long"

        readings: dict[str, float] = {}
        tripped: list[str] = []
        unavailable: list[str] = []

        book = self._book_crowding(key, long_move)
        if book is None:
            unavailable.append(FROM_THE_BOOK)
        else:
            readings[FROM_THE_BOOK] = book
            if book > self._book_threshold:
                tripped.append(FROM_THE_BOOK)

        order_flow = self._order_flow_crowding(key, long_move)
        if order_flow is None:
            unavailable.append(FROM_ORDER_FLOW)
        else:
            readings[FROM_ORDER_FLOW] = order_flow
            if order_flow > self._order_flow_threshold:
                tripped.append(FROM_ORDER_FLOW)

        for source in unavailable:
            self.standing.sources_unavailable[source] = (
                self.standing.sources_unavailable.get(source, 0) + 1
            )

        if len(readings) < self._minimum_sources:
            self.standing.not_measured += 1
            return self._reading(
                candidate, NOT_MEASURED, (), readings, unavailable,
                f"only {len(readings)} of two crowding sources could be read, below the "
                f"{self._minimum_sources} needed. A symbol nobody could read is not an "
                f"uncrowded symbol, so this reports as unmeasured rather than as clear",
            )

        if tripped:
            self.standing.crowded += 1
            for source in tripped:
                self.standing.by_tripping_source[source] = (
                    self.standing.by_tripping_source.get(source, 0) + 1
                )
            return self._reading(
                candidate, CROWDED, tuple(tripped), readings, unavailable,
                f"crowded on {len(tripped)} source(s): "
                + "; ".join(f"{source} at {readings[source]:.2f}" for source in tripped)
                + ". Either is a refusal rather than half of one -- averaging two "
                "crowding readings is how a crowded trade gets taken on the strength of the "
                "source that had not caught up yet",
            )

        return self._reading(
            candidate, NOT_CROWDED, (), readings, unavailable,
            "no source is extreme: "
            + "; ".join(f"{source} at {value:.2f}" for source, value in sorted(readings.items())),
        )

    def _book_crowding(self, key, long_move: bool) -> float | None:
        """How one-sided the book is in the direction of the move, in [0, 1].

        A move up into a book with nothing left on the offer is a crowd that has
        already lifted it.
        """
        book = self._books.get(key)
        if book is None:
            return None
        bids, asks = book
        bid_size = sum(size for _, size in bids)
        ask_size = sum(size for _, size in asks)
        total = bid_size + ask_size
        if total <= 0:
            return None
        imbalance = (bid_size - ask_size) / total
        return max(0.0, imbalance if long_move else -imbalance)

    def _order_flow_crowding(self, key, long_move: bool) -> float | None:
        """How far order flow sits from its own normal, in the direction that means consensus."""
        imbalance = self._order_flow.get(key)
        moments = self._order_flow_moments.get(key)
        if imbalance is None or moments is None:
            return None
        standardised = moments.standardise(imbalance, self._minimum)
        if standardised is None:
            return None
        return max(0.0, standardised if long_move else -standardised)

    def _moments(self, table, key) -> RunningMoments:
        moments = table.get(key)
        if moments is None:
            moments = RunningMoments(half_life_observations=self._half_life)
            table[key] = moments
        return moments

    def _reading(self, candidate, state, tripped, readings, unavailable, reason) -> CrowdingReading:
        return CrowdingReading(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            direction=candidate.direction,
            state=state,
            tripped_by=tripped,
            readings=dict(readings),
            sources_measured=len(readings),
            sources_unavailable=tuple(unavailable),
            reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_crowding(detector: TailCrowdingDetector) -> dict:
    return {
        "part_id": PART_ID,
        "readings": detector.standing.readings,
        "crowded": detector.standing.crowded,
        "not_measured": detector.standing.not_measured,
        "tripped_by_source": dict(sorted(detector.standing.by_tripping_source.items())),
        "sources_unavailable": dict(sorted(detector.standing.sources_unavailable.items())),
        "symbols_with_an_order_flow_normal": len(detector._order_flow_moments),
    }


def run_tail_crowding_detector(
    detector: TailCrowdingDetector, control_socket, read_candidates_and_sources,
    publish_readings, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = read_candidates_and_sources(detector)
        publish_readings(tuple(detector.read(candidate) for candidate in candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_crowding(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Open interest arrives per option contract (broker-open-interest); the
    aggregator resolves each contract to its underlying via
    broker-instrument-listing and sums the chain, the same as the bull/bear
    feature builders (runtime.underlying_open_interest).
    """
    from runtime.input_assembly import Batch
    from runtime.order_book import OrderBookSnapshot
    from runtime.underlying_open_interest import UnderlyingOpenInterestAggregator

    candidates = Batch(read=context.bus.reader("follow-candidate"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    open_interest = Batch(read=context.bus.reader("broker-open-interest"))
    publish_readings = context.bus.publisher_for("crowding-reading")
    detector = TailCrowdingDetector(
        book_imbalance_threshold=context.number("tail_crowding_book_imbalance_threshold"),
        order_flow_deviation_threshold=context.number("tail_crowding_order_flow_deviation_threshold"),
        minimum_observations=int(context.number("learning_minimum_observations")),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_sources=int(context.number("tail_crowding_minimum_sources")),
    )
    oi_aggregator = UnderlyingOpenInterestAggregator()

    def read_candidates_and_sources(_detector):
        for book in books.payloads():
            if isinstance(book, OrderBookSnapshot):
                detector.observe_book(book.venue_id, book.symbol, book.bids, book.asks)
        for listing in listings.payloads():
            oi_aggregator.observe_listing(listing)
        for reading in open_interest.payloads():
            oi_aggregator.observe_open_interest(reading)
        pending = tuple(candidates.payloads())
        for candidate in pending:
            totals = oi_aggregator.totals_for(candidate.symbol)
            if totals is not None:
                total = totals.total_buy_quantity + totals.total_sell_quantity
                if total > 0:
                    imbalance = (totals.total_buy_quantity - totals.total_sell_quantity) / total
                    detector.observe_order_flow(candidate.venue_id, candidate.symbol, imbalance)
        return pending

    def publish(items) -> None:
        if items:
            publish_readings(items)

    return run_tail_crowding_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_candidates_and_sources=read_candidates_and_sources,
        publish_readings=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
