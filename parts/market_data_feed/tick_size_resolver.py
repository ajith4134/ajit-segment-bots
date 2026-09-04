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


def increment_verdict_of(increments) -> tuple:
    """What makes an increment a different increment, with the noticing left out.

    A `PriceIncrement` carries two fields that restate when the resolver last
    looked rather than what it found: `resolved_at_ns`, and an `observations`
    count that climbs with every book seen. Compared whole, two statements of one
    unchanging tick size are never equal, so nothing would ever be skipped and the
    storm would survive the fix while the skip counter claimed otherwise -- the
    failure `runtime/level_publishing.py` documents and this part would have
    walked straight into, because it publishes one increment per symbol per tick.

    What a reader acts on is which symbol, what the increment is, and whether it
    was declared or inferred. `position-sizer` rounds an order to it and refuses
    without one; none of that depends on how many spacings agreed.
    """
    return tuple(
        (item.venue_id, item.symbol, item.increment, item.source) for item in increments
    )


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

    def observe_book(self, venue_id: str, symbol: str, bids, asks) -> None:
        """Record the spacings between adjacent levels on both sides.

        A level is `(price, quantity)`, which is what BookUpdate carries and what
        this read as a bare price until 2026-08-25 -- so the first real book took
        this part off the air subtracting one tuple from another. The quantity is
        deliberately dropped: a tick size is about where prices may sit, and a
        level with no size on it sits on a tick exactly like a level with size.
        """
        self.standing.books_seen += 1
        self._known_symbols.add((venue_id, symbol))
        prices_of = lambda levels: [
            float(level[0]) if isinstance(level, (tuple, list)) else float(level)
            for level in levels
        ]
        spacings = []
        for side in (sorted(prices_of(bids), reverse=True), sorted(prices_of(asks))):
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


def describe_increments(resolver: TickSizeResolver, levels=None) -> dict:
    """This part's standing, and what its level publisher actually did.

    `unchanged_increments_skipped` is on health because a change check whose skip
    count reads zero is a change check doing nothing, and it looks exactly like one
    that works. Here it should be very nearly everything: a tick size does not move.
    """
    standing = resolver.standing
    level_standing = {} if levels is None else {
        "increments_published": levels.standing.publishes,
        "unchanged_increments_skipped": levels.standing.unchanged_publishes_skipped,
        "increment_refreshes": levels.standing.refreshes,
        "increment_changes": levels.standing.changes,
        "symbols_held_as_levels": levels.keys_held,
    }
    return {
        **level_standing,
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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    levels=None,
) -> int:
    """`publish_increments(key, increments)` -- keyed, because the level is per symbol."""
    def tick() -> None:
        for venue_id, symbol, bids, asks in read_books():
            resolver.observe_book(venue_id, symbol, bids, asks)
        for increment in resolver.resolve_all():
            publish_increments((increment.venue_id, increment.symbol), (increment,))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_increments(resolver, levels),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The venue's declared increment where the catalogue gives one, and an increment
    inferred from live bid/ask spacing where it does not. Both paths are declared
    inputs: `symbol-universe` carries what the venue said, `order-book-snapshot`
    carries what it is doing.

    In the first runs no book reader is on, so every increment comes from the
    catalogue -- which is the better source anyway, and the inference exists for the
    symbols a venue lists without one.
    """
    from runtime.input_assembly import Batch

    from runtime.level_publishing import LevelPublisherByKey

    universe = Batch(read=context.bus.reader("symbol-universe"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    # One level per symbol. Until 2026-09-04 this part published `resolve_all()`
    # unconditionally on every tick -- an increment for every symbol it had ever
    # seen, 1,204 messages a second and 13% of the spine, for 1,156 symbols of
    # which 1,006 said UNKNOWN. A tick size is the most level-like thing on this
    # bus: it comes from the catalogue and does not move.
    increment_levels = LevelPublisherByKey(
        publish=context.bus.publisher_for("price-increment"),
        refresh_interval_seconds=context.number("price_increment_refresh_interval_seconds"),
        identity_of=increment_verdict_of,
    )

    def read_books():
        for entry in universe.payloads():
            resolver.declare_from_catalogue(entry.venue_id, entry.symbol, entry.price_increment)
        return tuple(
            (book.venue_id, book.symbol, book.bids, book.asks) for book in books.payloads()
        )

    resolver = TickSizeResolver(
        minimum_observations=int(context.number("tick_size_minimum_observations"))
    )
    return run_tick_size_resolver(
        resolver=resolver,
        control_socket=context.control_socket,
        read_books=read_books,
        levels=increment_levels,
        publish_increments=increment_levels.publish_level,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
