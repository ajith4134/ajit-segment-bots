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
    # The contract type translated into this system's words, from
    # trading_types -- what decides whether holding this costs funding, basis or
    # nothing. None where the adapter did not recognise the venue's spelling, and
    # a reader must treat that as "unknown kind" rather than as any kind.
    instrument_kind: str | None = None
    # What the venue says holding this contract costs: the funding rate it last
    # charged, and how many times a day it charges one. Both None on a contract
    # that pays no funding, and both None when the venue quoted a rate this read
    # did not reach -- unknown is not zero here either, and a part that priced a
    # missing rate as free would make a perpetual look cheaper than it is by the
    # largest recurring cost of holding one.
    funding_rate_per_settlement: float | None = None
    funding_settlements_per_day: float | None = None
    # Which endpoint and field each of the two above was read from. Carried with
    # them because a carry cost is a number a position is priced against, and
    # RL-061 does not stop at the venue boundary.
    funding_source: str | None = None
