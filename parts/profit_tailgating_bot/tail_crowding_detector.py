"""tail-crowding-detector: whether everyone is already in the trade we are joining.

A tailgater is by construction late, and its one structural risk is that the
thing making a move visible is the crowd that has already taken it. Crowding is
not a small negative on a good setup; it inverts the setup, because the exit that
ends a crowded move is the same exit for everyone in it.

Three independent readings, from sources that crowd differently:

- **The book.** A one-sided book with thin resting size behind the move is a
  crowd that has already lifted the other side. It shows crowding first and is
  the noisiest.
- **Funding.** Perpetual funding is the price of consensus: everyone long pays,
  and an extreme rate is the crowd paying to stay in. It lags the book and is far
  harder to fake.
- **Sentiment.** Slowest, least reliable alone, and the only one that can see a
  crowd forming outside the venue.

**Any one of them extreme is a refusal, not a third of one.** Averaging three
crowding readings is how a crowded trade gets taken on the strength of the two
sources that had not caught up yet. The reading names which source tripped,
because they have different remedies: a crowded book clears in minutes, and
crowded funding does not.

**"Not measured" is its own state**, never a low reading (Rule 8). A symbol
nobody could read is not an uncrowded symbol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CROWDED, CROWDING_NOT_MEASURED as NOT_MEASURED, FROM_FUNDING, FROM_SENTIMENT,
    FROM_THE_BOOK, NOT_CROWDED, CrowdingReading,
)
from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-crowding-detector"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-crowding-detector",
    consumes=("follow-candidate", "order-book-snapshot", "funding-forecast", "sentiment-reading"),
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
    """Reads crowding from three sources and refuses on any one of them."""

    def __init__(
        self,
        book_imbalance_threshold: float,
        funding_deviation_threshold: float,
        sentiment_deviation_threshold: float,
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
        self._funding_threshold = funding_deviation_threshold
        self._sentiment_threshold = sentiment_deviation_threshold
        self._minimum = minimum_observations
        self._half_life = half_life_observations
        self._minimum_sources = minimum_sources
        self._now_ns = now_ns
        self._books: dict[tuple[str, str], tuple] = {}
        self._funding: dict[tuple[str, str], float] = {}
        self._sentiment: dict[tuple[str, str], float] = {}
        self._funding_moments: dict[tuple[str, str], RunningMoments] = {}
        self._sentiment_moments: dict[tuple[str, str], RunningMoments] = {}
        self.standing = DetectorStanding()

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        self._books[(venue_id, symbol)] = (tuple(bids), tuple(asks))

    def observe_funding(self, venue_id: str, symbol: str, rate: float) -> None:
        key = (venue_id, symbol)
        self._funding[key] = rate
        self._moments(self._funding_moments, key).observe(rate)

    def observe_sentiment(self, venue_id: str, symbol: str, reading: float) -> None:
        key = (venue_id, symbol)
        self._sentiment[key] = reading
        self._moments(self._sentiment_moments, key).observe(reading)

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

        funding = self._funding_crowding(key, long_move)
        if funding is None:
            unavailable.append(FROM_FUNDING)
        else:
            readings[FROM_FUNDING] = funding
            if funding > self._funding_threshold:
                tripped.append(FROM_FUNDING)

        sentiment = self._sentiment_crowding(key, long_move)
        if sentiment is None:
            unavailable.append(FROM_SENTIMENT)
        else:
            readings[FROM_SENTIMENT] = sentiment
            if sentiment > self._sentiment_threshold:
                tripped.append(FROM_SENTIMENT)

        for source in unavailable:
            self.standing.sources_unavailable[source] = (
                self.standing.sources_unavailable.get(source, 0) + 1
            )

        if len(readings) < self._minimum_sources:
            self.standing.not_measured += 1
            return self._reading(
                candidate, NOT_MEASURED, (), readings, unavailable,
                f"only {len(readings)} of three crowding sources could be read, below the "
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
                + ". Any one is a refusal rather than a third of one -- averaging three "
                "crowding readings is how a crowded trade gets taken on the strength of the "
                "sources that had not caught up yet",
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

    def _funding_crowding(self, key, long_move: bool) -> float | None:
        """How far funding sits from its own normal, in the direction that means consensus."""
        rate = self._funding.get(key)
        moments = self._funding_moments.get(key)
        if rate is None or moments is None:
            return None
        standardised = moments.standardise(rate, self._minimum)
        if standardised is None:
            return None
        return max(0.0, standardised if long_move else -standardised)

    def _sentiment_crowding(self, key, long_move: bool) -> float | None:
        reading = self._sentiment.get(key)
        moments = self._sentiment_moments.get(key)
        if reading is None or moments is None:
            return None
        standardised = moments.standardise(reading, self._minimum)
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
        "symbols_with_a_funding_normal": len(detector._funding_moments),
        "symbols_with_a_sentiment_normal": len(detector._sentiment_moments),
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
    )
