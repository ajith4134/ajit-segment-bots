"""A book kept from a venue's updates, and the snapshot handed to whoever reads it.

Two venues, two disciplines. Binance's partial-depth stream is a fresh top-N
every push, so the book *is* the last message. Bybit sends one snapshot and
then deltas, and a delta applied to a book that missed an earlier delta is a
book with wrong prices that nothing marks as wrong. So the keeper refuses a
delta whose update id does not follow the last one it applied, and says so,
and holds no book for that symbol until the venue sends a snapshot again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.venues.venue_adapter import BookUpdate


@dataclass(frozen=True)
class OrderBookSnapshot:
    """What the book looked like after one update, best price first."""

    venue_id: str
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    sequence: int
    venue_time_ns: int
    # Whether this came straight from a venue snapshot (True) or from deltas
    # applied to one. A consumer comparing books across a resync needs to know.
    is_from_snapshot: bool

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def is_crossed(self) -> bool:
        return bool(self.bids and self.asks and self.bids[0][0] >= self.asks[0][0])


@dataclass
class KeeperStanding:
    snapshots_taken: int = 0
    deltas_applied: int = 0
    deltas_refused_out_of_order: int = 0
    deltas_refused_without_a_book: int = 0
    books_held: int = 0


@dataclass
class _Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    sequence: int = 0
    is_from_snapshot: bool = True


class OrderBookKeeper:
    """Applies updates per (venue, symbol) and returns the book after each."""

    def __init__(self, depth_levels: int) -> None:
        if depth_levels <= 0:
            raise ValueError("a book of no levels is not a book")
        self._depth = depth_levels
        self._books: dict[tuple[str, str], _Book] = {}
        self.standing = KeeperStanding()

    def apply(self, update: BookUpdate) -> OrderBookSnapshot | None:
        """The book after this update, or None when the update could not be applied."""
        key = (update.venue_id, update.symbol)
        if update.is_snapshot:
            book = _Book(
                bids={price: quantity for price, quantity in update.bids if quantity > 0},
                asks={price: quantity for price, quantity in update.asks if quantity > 0},
                sequence=update.sequence,
                is_from_snapshot=True,
            )
            self._books[key] = book
            self.standing.snapshots_taken += 1
        else:
            book = self._books.get(key)
            if book is None:
                self.standing.deltas_refused_without_a_book += 1
                return None
            if update.sequence <= book.sequence:
                # Already applied, or older than the book: not an error, not applied.
                return None
            if update.sequence != book.sequence + 1:
                # A missed delta. The book is wrong from here and must not be
                # handed on; the venue's next snapshot starts it again.
                self.standing.deltas_refused_out_of_order += 1
                del self._books[key]
                self.standing.books_held = len(self._books)
                return None
            for side, levels in ((book.bids, update.bids), (book.asks, update.asks)):
                for price, quantity in levels:
                    if quantity > 0:
                        side[price] = quantity
                    else:
                        side.pop(price, None)
            book.sequence = update.sequence
            book.is_from_snapshot = False
            self.standing.deltas_applied += 1
        self.standing.books_held = len(self._books)
        return OrderBookSnapshot(
            venue_id=update.venue_id,
            symbol=update.symbol,
            bids=tuple(sorted(book.bids.items(), key=lambda level: -level[0])[: self._depth]),
            asks=tuple(sorted(book.asks.items(), key=lambda level: level[0])[: self._depth]),
            sequence=book.sequence,
            venue_time_ns=update.venue_time_ns,
            is_from_snapshot=book.is_from_snapshot,
        )


__all__ = ["KeeperStanding", "OrderBookKeeper", "OrderBookSnapshot"]
