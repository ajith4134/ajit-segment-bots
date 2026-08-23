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

# What kind of contract a listing is, in this system's words rather than a
# venue's. Binance says PERPETUAL and TRADIFI_PERPETUAL and CURRENT_QUARTER,
# Bybit says LinearPerpetual and LinearFutures, and the difference decides how a
# position is charged for being held: a perpetual pays funding every settlement,
# a dated future pays its basis once as it converges, spot pays nothing.
#
# The translation from a venue's word to one of these belongs to that venue's
# adapter, the same way the trade-side translation above belongs here: a part
# that recognised a venue's spelling would be a part wired to a venue (T-4).
PERPETUAL_FUTURE = "perpetual-future"
DATED_FUTURE = "dated-future"
SPOT = "spot"
OPTION = "option"


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


# Where an order is addressed. The distinction that keeps paper money paper:
# every part that can send an order checks it, and a run that produced a
# LIVE_VENUE destination while the segment is on paper is the one failure this
# phase cannot recover from (RL-005).
PAPER_BOOK = "paper-book"
LIVE_VENUE = "live-venue"

# What an order is asking for. `ROUTED` is the only outcome that may be sent;
# the rest exist so a refusal travels with the order it refused rather than
# disappearing into a log.
ROUTED = "routed"
REFUSED_NO_MODE = "refused-money-mode-unknown"
REFUSED_UNSTAMPED = "refused-order-carries-no-client-id"

# What kind of order this is, stated rather than inferred from which price fields
# happen to be set. Inferring it was a live defect: an entry carries the stop
# price that will protect it once it fills, and a reader that treated any order
# with a stop price as a stop order turned every entry into a trigger waiting for
# the market to fall to it.
#
# **Entries and exits are market orders** (operator, 2026-08-23). A limit order
# fills only if the market comes back to the price the decision was made at, and
# a decision to be in the market is not a decision to be in it at one price. The
# two triggered types are market orders too -- they wait for a price and then
# take whatever the book gives, which is what a venue's stop-market does and what
# the slippage of a real stop actually costs.
MARKET = "market"
LIMIT = "limit"
STOP_MARKET = "stop-market"
TAKE_PROFIT_MARKET = "take-profit-market"

# The order types that wait for a trigger price before becoming market orders.
TRIGGERED_ORDER_TYPES = (STOP_MARKET, TAKE_PROFIT_MARKET)


@dataclass(frozen=True)
class OrderRequest:
    """One order, addressed to exactly one destination.

    Shared vocabulary rather than one part's private class. Two parts construct
    these -- `order-destination-router` for entries and `stop-order-manager` for
    the exits that close a position -- and a part importing another part's
    dataclass is a part wired to a part (T-4).

    `stop_price` and `limit_price` are 0.0 when absent, never None, because they
    arrive from settings and from venue fields that use zero for "not set", and a
    type that accepted both would need every reader to handle two spellings of
    nothing.

    `cancels_client_order_id` is how one order withdraws another, which is what a
    venue's cancel-replace actually is. It matters for exits specifically: a
    target that fills leaves a stop resting for a position that no longer exists,
    and a stop that triggers on nothing opens the opposite position.
    """

    client_order_id: str
    destination: str
    venue_id: str
    symbol: str
    side: str
    quantity: float
    limit_price: float
    stop_price: float
    slice_sequence: int
    slice_count: int
    at_second: float
    outcome: str
    reason: str
    routed_at_ns: int
    cancels_client_order_id: str | None = None
    order_type: str = MARKET

    @property
    def is_live_money(self) -> bool:
        return self.destination == LIVE_VENUE

    @property
    def may_be_sent(self) -> bool:
        return self.outcome == ROUTED and self.quantity > 0

    @property
    def waits_for_a_trigger(self) -> bool:
        """Whether this order sits until the market reaches `stop_price`.

        A stop and a take-profit are the same mechanism pointed in opposite
        directions -- a sell stop triggers below the market and a sell
        take-profit triggers above it -- so the type is what decides the test,
        never the sign of the price.
        """
        return self.order_type in TRIGGERED_ORDER_TYPES

    @property
    def trigger_price(self) -> float | None:
        return self.stop_price if self.waits_for_a_trigger else None


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
