"""Reading `symbol-price-frame` back into the levels a part works with.

Thirty-seven parts stopped being handed every trade on 2026-08-24 and started
being handed a frame of every symbol's latest price. What they do with a price did
not change, so this is the one place the translation happens rather than
thirty-seven places -- and the one place that has to keep the property the whole
sweep was about.

**A level carries the moment the market made it, never the moment the frame
carrying it was published.** A frame published now holds prices from whenever each
symbol last printed; a reader that took the frame's own timestamp would be
recreating, one layer up, exactly the defect that priced an ENAUSDT order
fifty-six minutes late.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class VenueSymbolLevel:
    """One symbol's latest price on one venue, and when the market made it."""

    venue_id: str
    symbol: str
    price: float
    observed_at_ns: int

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


def levels_in(frames: Iterable) -> Iterator[VenueSymbolLevel]:
    """Every level in every frame, with its venue attached.

    A split frame reads as the levels it carries: nothing downstream has to know
    the sampler had to divide the universe to fit the bus.

    Anything that is not a frame is skipped rather than raised on. An inbox
    carries what the wiring delivers, and a part that died on an unexpected shape
    would be a part the wiring could kill.
    """
    for frame in frames:
        levels = getattr(frame, "levels", None)
        venue_id = getattr(frame, "venue_id", None)
        if levels is None or venue_id is None:
            continue
        for level in levels:
            yield VenueSymbolLevel(
                venue_id=venue_id,
                symbol=level.symbol,
                price=level.price,
                observed_at_ns=level.observed_at_ns,
            )
