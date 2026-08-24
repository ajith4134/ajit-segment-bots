"""Reading `symbol-quote-frame` back into the quotes a part works with.

The sibling of `runtime.price_frames`, and it keeps the same property for the same
reason: **a level carries the moment the venue stamped it, never the moment the
frame carrying it was published.** A frame published now holds quotes from
whenever each symbol was last quoted, and a reader taking the frame's own
timestamp would recreate one layer up the defect that priced an ENAUSDT order
fifty-six minutes late on 2026-08-23.

A quote reaches a part as a mid price with an age, because that is what sizing a
position needs. The two sides and their sizes stay on the level for a part that
wants to know how thin the market is -- `spread` and the quantities are there --
but nothing is obliged to look, and the mid is what a price-shaped caller reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class VenueSymbolQuote:
    """One symbol's latest resting bid and ask on one venue, and when it was said."""

    venue_id: str
    symbol: str
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    observed_at_ns: int

    @property
    def mid_price(self) -> float:
        """The price to size against: halfway between what is bid and what is asked."""
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_fraction(self) -> float:
        """The spread as a fraction of the mid, which is what makes it comparable."""
        mid = self.mid_price
        return self.spread / mid if mid > 0 else 0.0

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


def quote_levels_in(frames: Iterable) -> Iterator[VenueSymbolQuote]:
    """Every quote in every frame, with its venue attached.

    A split frame reads as the quotes it carries, and anything that is not a quote
    frame is skipped rather than raised on -- an inbox carries what the wiring
    delivers, and a part that died on an unexpected shape would be a part the
    wiring could kill. Both rules are `price_frames`', deliberately: two readers
    of two frames that behaved differently on the same edge would be a difference
    nobody chose.
    """
    for frame in frames:
        levels = getattr(frame, "levels", None)
        venue_id = getattr(frame, "venue_id", None)
        if levels is None or venue_id is None:
            continue
        for level in levels:
            if not hasattr(level, "bid_price") or not hasattr(level, "ask_price"):
                continue  # a price level, not a quote level: a different frame's shape
            yield VenueSymbolQuote(
                venue_id=venue_id,
                symbol=level.symbol,
                bid_price=level.bid_price,
                bid_quantity=level.bid_quantity,
                ask_price=level.ask_price,
                ask_quantity=level.ask_quantity,
                observed_at_ns=level.observed_at_ns,
            )
