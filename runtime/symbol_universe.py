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
class MarginTier:
    """One step of a venue's maintenance margin schedule, as the venue published it.

    Lives here rather than in the part that uses it because it is data (T-4), and
    because two things need it that may not import each other: the adapters that
    parse it out of a venue response, and `liquidation-cluster-mapper`, which
    turns it into the distance a position at a given leverage is liquidated at.

    `notional_floor` is where this tier starts. A venue states the ladder as
    "up to this size, this rate", so the tier that applies to a position is the
    highest floor at or below its notional.
    """

    notional_floor: float
    maintenance_margin_rate: float
    maximum_leverage: float


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
    # The rest of the venue's own funding formula -- what `funding-rate-forecaster`
    # needs to compute a settlement the way the venue does. None where the venue
    # did not state one for this symbol on this read, same reasoning as the pair
    # above: a defaulted cap or interest rate is wrong for exactly the symbols
    # whose venue-stated figure differs from the default, and wrong invisibly.
    funding_rate_cap: float | None = None
    funding_rate_floor: float | None = None
    funding_interest_rate_per_interval: float | None = None
    # The 24-hour high-low range as a fraction of last price, from the same
    # ticker response quote_volume_24h comes from. None where the venue did not
    # price the symbol on this read -- the same absence, not a zero range.
    volatility_24h: float | None = None
    # This contract's maintenance margin ladder, as the venue published it.
    # Empty when the venue's schedule could not be read -- Binance serves its
    # brackets from a signed endpoint, so an empty tuple there means no API key
    # is configured, not that the contract has no maintenance margin. Read the
    # reader's own standing for which of the two it is; an empty ladder must
    # never be treated as a zero rate, because a zero maintenance margin puts
    # every liquidation price at the entry.
    margin_tiers: tuple = ()
    # Where the ladder came from, or why it is empty. Same contract as
    # funding_source: a number a position is priced against carries its
    # provenance (RL-061).
    margin_source: str | None = None
