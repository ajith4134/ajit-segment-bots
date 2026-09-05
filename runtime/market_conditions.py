"""The hard channel's payload types: what the exchange itself says about
whether an instrument may be traded, what a corporate action does to a price
series, and which session the market is in.

Nothing here is scored. A restriction is a fact the exchange published, and a
part reading one refuses rather than weighs -- which is why these types are
separate from the news signal types entirely (D-N4,
docs/superpowers/specs/2026-09-02-stock-market-news-data-design.md). One wire
carrying both a halt and a sentiment score is the shape that crashed a reader
of trades on 2026-08-25, one layer along.

NSE states dates two ways in the same day's data: the ban file writes
02-SEP-2026 and every JSON endpoint writes 02-Sep-2026 (both captured
2026-09-02). One parser reads both; a string in neither shape -- NSE writes
"-" where it has no date -- raises rather than defaulting to today, because a
corporate action silently dated today would adjust a price series that was
never adjusted.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass

NSE_DATE_FORMAT = "%d-%b-%Y"


def read_nse_date(stated: str) -> datetime.date:
    """One of NSE's own date strings, in either casing it publishes."""
    return datetime.datetime.strptime(stated.strip().title(), NSE_DATE_FORMAT).date()


class RestrictionKind(enum.StrEnum):
    """What the exchange said, in its own vocabulary."""

    FNO_BAN = "fno-ban"
    ASM_SHORT_TERM = "asm-short-term"
    ASM_LONG_TERM = "asm-long-term"


class SessionKind(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    HOLIDAY = "holiday"


@dataclass(frozen=True)
class InstrumentRestrictionReport:
    """One source's statement that one instrument is restricted."""

    symbol: str
    kind: RestrictionKind
    source: str
    stated_for: datetime.date
    detail: str
    observed_at_ns: int


@dataclass(frozen=True)
class InstrumentRestriction:
    """Every restriction standing on one instrument right now, merged.

    `may_close_existing_position` is True for every kind NSE publishes here:
    a ban stops fresh positions, not exits, and an ASM stage raises margin
    without closing the door. A restriction that blocked exits would trap
    capital the exchange never trapped.
    """

    symbol: str
    kinds: tuple[RestrictionKind, ...]
    sources: tuple[str, ...]
    stated_for: datetime.date
    observed_at_ns: int

    @property
    def may_open_new_position(self) -> bool:
        return not self.kinds

    @property
    def may_close_existing_position(self) -> bool:
        return True


@dataclass(frozen=True)
class CorporateActionReport:
    """A corporate action as NSE published it, subject text carried verbatim."""

    symbol: str
    series: str
    isin: str
    subject: str
    ex_date: datetime.date
    record_date: datetime.date | None
    face_value: float | None
    observed_at_ns: int


@dataclass(frozen=True)
class CorporateAction:
    """What an action does to a price series, from its ex-date on.

    `price_factor` is what a pre-ex price is multiplied by to sit beside a
    post-ex one; `quantity_factor` is what a pre-ex quantity is multiplied by.
    A 1:1 bonus doubles the shares and halves the price: 0.5 and 2.0.
    """

    symbol: str
    kind: str
    price_factor: float
    quantity_factor: float
    ex_date: datetime.date
    stated_from: str
    observed_at_ns: int


# The exchange every segment here trades on. A string rather than a setting: it
# is a property of NSE and BSE, not a dial -- an operator who changed it would be
# saying this project trades a different country's market.
EXCHANGE_TIMEZONE = "Asia/Kolkata"


def read_clock_time(stated: str) -> datetime.time:
    """"09:15" as NSE publishes it, from a setting rather than a literal.

    Lives here rather than in market-session-calendar because a second part now
    needs the session's own close: pre-expiry-position-closer decides how long
    before it a contract expiring today must be out. A part may not import
    another part (T-4), and the two must read the same clock or one would close
    positions against a session boundary the other does not agree on.
    """
    hour, _, minute = stated.partition(":")
    return datetime.time(int(hour), int(minute))


@dataclass(frozen=True)
class MarketSessionState:
    """Which session one exchange segment is in right now."""

    segment: str
    kind: SessionKind
    as_of_date: datetime.date
    reason: str
    observed_at_ns: int

    @property
    def is_tradeable(self) -> bool:
        return self.kind is SessionKind.OPEN


__all__ = [
    "EXCHANGE_TIMEZONE",
    "NSE_DATE_FORMAT",
    "CorporateAction",
    "CorporateActionReport",
    "InstrumentRestriction",
    "read_clock_time",
    "InstrumentRestrictionReport",
    "MarketSessionState",
    "RestrictionKind",
    "SessionKind",
    "read_nse_date",
]
