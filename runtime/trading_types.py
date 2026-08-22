"""The trading vocabulary shared by parts that reason about positions and fills.

Data, not a part. Under T-4 a part names data and never another part, so these
live here rather than in whichever part happened to define one first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUY = "buy"
SELL = "sell"
LONG = "long"
SHORT = "short"
FLAT = "flat"


def order_side_for(position_side: str) -> str:
    """The side an order is placed on to open a position of this side.

    Two vocabularies meet here and both are correct in their own half: a position
    is long or short, an order is bought or sold. The brain speaks the first and
    the venue speaks the second, and a part that compared one against the other
    would read a long intent as a sell -- which puts the stop on the wrong side of
    the entry and refuses every trade for a reason that is about vocabulary.

    Passes an order side through unchanged, so the translation is safe to apply
    wherever the two meet without knowing which one arrived.
    """
    if position_side in (LONG, BUY):
        return BUY
    if position_side in (SHORT, SELL):
        return SELL
    raise ValueError(
        f"{position_side!r} is neither a position side ({LONG}/{SHORT}) nor an order "
        f"side ({BUY}/{SELL}), and guessing which was meant would place a trade the "
        f"wrong way round"
    )


@dataclass(frozen=True)
class Fill:
    """One execution, as the venue reported it."""

    fill_id: str
    venue_id: str
    symbol: str
    side: str
    price: float
    quantity: float
    fee: float
    filled_at_ns: int
    order_id: str | None = None
    is_paper: bool = True

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == BUY else -self.quantity


@dataclass(frozen=True)
class Position:
    """What is held in one symbol right now, and what it cost to get there."""

    venue_id: str
    symbol: str
    quantity: float
    average_entry_price: float
    realised_pnl: float
    fees_paid: float
    opened_at_ns: int
    updated_at_ns: int

    @property
    def direction(self) -> str:
        if self.quantity > 0:
            return LONG
        if self.quantity < 0:
            return SHORT
        return FLAT

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


@dataclass(frozen=True)
class ClosedTrade:
    """One round trip, from first open to flat."""

    venue_id: str
    symbol: str
    direction: str
    quantity: float
    entry_price: float
    exit_price: float
    realised_pnl: float
    fees_paid: float
    opened_at_ns: int
    closed_at_ns: int
    best_unrealised: float | None = None
    worst_unrealised: float | None = None

    @property
    def holding_seconds(self) -> float:
        return (self.closed_at_ns - self.opened_at_ns) / 1e9


@dataclass(frozen=True)
class Lot:
    """One parcel of a position, kept so partial closes resolve oldest-first."""

    quantity: float
    price: float
    opened_at_ns: int
    fee: float = 0.0


@dataclass
class LotBook:
    """The open lots of one symbol, in the order they were opened.

    Oldest first (FIFO). Which lot a closing fill is matched against decides the
    realised profit of every partial close, so it is a stated rule rather than
    whatever order a dictionary happened to hold.
    """

    lots: list[Lot] = field(default_factory=list)

    def add(self, lot: Lot) -> None:
        self.lots.append(lot)

    def take(self, quantity: float) -> list[tuple[Lot, float]]:
        """Consume `quantity` from the oldest lots, returning what each gave up."""
        taken: list[tuple[Lot, float]] = []
        remaining = quantity
        while remaining > 0 and self.lots:
            lot = self.lots[0]
            used = min(lot.quantity, remaining)
            taken.append((lot, used))
            remaining -= used
            if used >= lot.quantity:
                self.lots.pop(0)
            else:
                self.lots[0] = Lot(lot.quantity - used, lot.price, lot.opened_at_ns, lot.fee)
        return taken

    @property
    def total_quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def average_price(self) -> float | None:
        total = self.total_quantity
        if total <= 0:
            return None
        return sum(lot.quantity * lot.price for lot in self.lots) / total
