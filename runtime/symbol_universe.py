"""`symbol-universe`: the symbols a venue lists that we have chosen to capture.

The data type `symbol-catalogue-reader` produces and `stream-budget-planner`
consumes. It lives here rather than in either part because it is data: under T-4
a part names data and never another part, so a planner that imported the reader
to learn this shape would be wired to the reader itself rather than to what it
produces.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapturableSymbol:
    """One symbol chosen for capture, with the figure that chose it.

    `quote_volume_24h` is None when the venue listed the symbol but its ticker
    did not price it. Unknown is not zero, and it is carried rather than filled
    in so that a venue which stopped pricing half its symbols is visible instead
    of merely quiet.

    `contract_type` is the venue's own word for what the contract is -- Binance's
    `TRADIFI_PERPETUAL` for a tokenised equity, Bybit's `LinearPerpetual`. It
    travels with the symbol so a later phase can separate contract types without
    re-reading the venue (spec 1.1, the user's ruling of 2026-08-21).
    """

    venue_id: str
    symbol: str
    contract_type: str
    quote_volume_24h: float | None
    price_increment: float | None
