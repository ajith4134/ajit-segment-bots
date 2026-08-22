"""tick-size-resolver: the venue's declared price increment, or one inferred."""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tick-size-resolver"

PART_DECLARATION = PartDeclaration(
    part_id="tick-size-resolver",
    consumes=("order-book-snapshot", "symbol-universe"),
    produces=("price-increment", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DECLARED = "declared-by-venue"
INFERRED = "inferred-from-book"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class PriceIncrement:
    """One symbol's tick, and how it was arrived at."""

    venue_id: str
    symbol: str
    increment: float | None
    source: str
    observations: int
    resolved_at_ns: int


@dataclass
class ResolverStanding:
    symbols_declared: int = 0
    symbols_inferred: int = 0
    symbols_unknown: int = 0
    books_seen: int = 0
    disagreements: int = 0


class TickSizeResolver:
    """Prefers what the venue declared; infers from live level spacing otherwise.

    An inference is only published once enough distinct spacings agree, because
    a book that happens to be one tick wide at every level for a moment would
    otherwise fix the wrong increment permanently. Where a declared increment and
    an observed spacing disagree the declared one wins and the disagreement is
    counted -- the venue rejects orders, not our arithmetic.
    """

    def __init__(self, minimum_observations: int, now_ns=time.time_ns) -> None:
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        self._declared: dict[tuple[str, str], float] = {}
        self._observed: dict[tuple[str, str], list[float]] = {}
        self._known_symbols: set[tuple[str, str]] = set()
        self.standing = ResolverStanding()

    def declare_from_catalogue(self, venue_id: str, symbol: str, increment: float | None) -> None:
        self._known_symbols.add((venue_id, symbol))
        if increment and increment > 0:
            self._declared[(venue_id, symbol)] = increment

    def observe_book(self, venue_id: str, symbol: str, bids: list[float], asks: list[float]) -> None:
        """Record the spacings between adjacent levels on both sides."""
        self.standing.books_seen += 1
        self._known_symbols.add((venue_id, symbol))
        spacings = []
        for side in (sorted(bids, reverse=True), sorted(asks)):
            for nearer, further in zip(side, side[1:]):
                spacing = abs(round(further - nearer, 12))
                if spacing > 0:
                    spacings.append(spacing)
        if spacings:
            self._observed.setdefault((venue_id, symbol), []).extend(spacings)

    def resolve(self, venue_id: str, symbol: str) -> PriceIncrement:
        key = (venue_id, symbol)
        declared = self._declared.get(key)
        observed = self._observed.get(key, [])
        inferred = min(observed) if len(observed) >= self._minimum_observations else None

        if declared is not None:
            if inferred is not None and abs(inferred - declared) > declared / 2:
                self.standing.disagreements += 1
            return self._result(venue_id, symbol, declared, DECLARED, len(observed))
        if inferred is not None:
            return self._result(venue_id, symbol, inferred, INFERRED, len(observed))
        return self._result(venue_id, symbol, None, UNKNOWN, len(observed))

    def resolve_all(self) -> tuple[PriceIncrement, ...]:
        self.standing.symbols_declared = 0
        self.standing.symbols_inferred = 0
        self.standing.symbols_unknown = 0
        results = []
        for venue_id, symbol in sorted(self._known_symbols):
            result = self.resolve(venue_id, symbol)
            if result.source == DECLARED:
                self.standing.symbols_declared += 1
            elif result.source == INFERRED:
                self.standing.symbols_inferred += 1
            else:
                self.standing.symbols_unknown += 1
            results.append(result)
        return tuple(results)

    def _result(self, venue_id, symbol, increment, source, observations) -> PriceIncrement:
        return PriceIncrement(
            venue_id=venue_id,
            symbol=symbol,
            increment=increment,
            source=source,
            observations=observations,
            resolved_at_ns=self._now_ns(),
        )


def describe_increments(resolver: TickSizeResolver) -> dict:
    standing = resolver.standing
    return {
        "part_id": PART_ID,
        "books_seen": standing.books_seen,
        "symbols_declared": standing.symbols_declared,
        "symbols_inferred": standing.symbols_inferred,
        "symbols_unknown": standing.symbols_unknown,
        "declared_inferred_disagreements": standing.disagreements,
    }


def run_tick_size_resolver(
    resolver: TickSizeResolver, control_socket, read_books, publish_increments,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for venue_id, symbol, bids, asks in read_books():
            resolver.observe_book(venue_id, symbol, bids, asks)
        publish_increments(resolver.resolve_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
