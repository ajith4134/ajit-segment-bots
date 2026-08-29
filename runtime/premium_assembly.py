"""Turning what a venue said about a premium into what a symbol's premium is.

The same problem `quote_assembly` solves, on the other half of the same Bybit
message. A premium is mark price against index price, and Bybit's `tickers`
stream amends rather than restates: a delta can name a mark price and no index,
or a funding rate and neither. The adapter reports the absent field as None
rather than inventing it (see `VenuePremium`), which leaves one question -- who
remembers the rest -- and this is the answer.

**A merged premium is as old as its stalest half.** Mark and index are two
separate statements by the venue, and the premium between them is only as
current as the older one. Taking the newer stamp would make a premium computed
against a forty-second-old index look like a fresh measurement, and the whole
point of a premium is that it is a measurement of *now*.

**Missing is never zero.** A premium of zero says the perpetual is trading
exactly at its index, which is a claim about the market. A symbol whose index has
never arrived has no premium at all, and those are different facts -- so this
returns None rather than a premium with a hole in it.

Nothing here decides whether a premium is fresh enough to forecast from. That
bound belongs to the part that forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.venues.venue_adapter import VenuePremium


@dataclass
class _FieldState:
    """One number the venue stated about one symbol, and when it said it."""

    value: float
    said_at_ns: int


class PremiumAssembler:
    """Every symbol's last complete premium on one venue, kept across amending messages.

    One assembler per venue, because a perpetual is marked differently on each and
    the two premiums are different facts. It holds no clock and asks nothing of
    the world: given the same messages in the same order it produces the same
    premiums, which is what makes it testable against a captured stream.
    """

    def __init__(self, venue_id: str) -> None:
        self._venue_id = venue_id
        self._marks: dict[str, _FieldState] = {}
        self._indexes: dict[str, _FieldState] = {}
        # The two that ride along. Kept without a stamp of their own: they are the
        # venue's statement about a settlement in the future, not a measurement of
        # now, so their age does not date the premium.
        self._declared_rates: dict[str, float] = {}
        self._settlements: dict[str, int] = {}

        # Messages that named neither mark nor index. Most of Bybit's tickers
        # traffic is a quote amend and touches no premium at all.
        self.messages_naming_no_premium = 0
        # Named one and the other has never been seen. Only possible before a
        # symbol's first snapshot.
        self.messages_still_incomplete = 0
        self.premiums_completed = 0
        # An index of zero or less, which would make the premium fraction a
        # division by zero or a sign flip. Counted rather than raised: one bad
        # field must not stop a feed.
        self.unusable_index_prices = 0

    def apply(self, premium: VenuePremium) -> VenuePremium | None:
        """Fold one message into what is known, and return the premium if it is whole.

        None means this message did not leave a complete premium for that symbol.
        It never means the premium is unchanged -- an unchanged premium is still
        returned, because a reader asking what it is now is entitled to an answer
        whether or not the venue moved it.
        """
        symbol = premium.symbol
        named_a_side = False

        if premium.mark_price is not None:
            named_a_side = True
            self._marks[symbol] = _FieldState(premium.mark_price, premium.venue_time_ns)
        if premium.index_price is not None:
            if premium.index_price <= 0:
                self.unusable_index_prices += 1
            else:
                named_a_side = True
                self._indexes[symbol] = _FieldState(
                    premium.index_price, premium.venue_time_ns
                )

        if premium.declared_funding_rate is not None:
            self._declared_rates[symbol] = premium.declared_funding_rate
        if premium.next_settlement_at_ns is not None:
            self._settlements[symbol] = premium.next_settlement_at_ns

        if not named_a_side:
            self.messages_naming_no_premium += 1
            return None

        mark = self._marks.get(symbol)
        index = self._indexes.get(symbol)
        if mark is None or index is None:
            self.messages_still_incomplete += 1
            return None

        self.premiums_completed += 1
        return VenuePremium(
            venue_id=self._venue_id,
            symbol=symbol,
            mark_price=mark.value,
            index_price=index.value,
            declared_funding_rate=self._declared_rates.get(symbol),
            next_settlement_at_ns=self._settlements.get(symbol),
            # The stalest half. See this module's docstring.
            venue_time_ns=min(mark.said_at_ns, index.said_at_ns),
        )

    def symbols_held(self) -> int:
        """How many symbols have both halves, so a premium can be answered for them."""
        return len(self._marks.keys() & self._indexes.keys())

    def describe(self) -> dict:
        """What this assembler has done, for the part's standing."""
        return {
            "symbols_held": self.symbols_held(),
            "premiums_completed": self.premiums_completed,
            "messages_naming_no_premium": self.messages_naming_no_premium,
            "messages_still_incomplete": self.messages_still_incomplete,
            "unusable_index_prices": self.unusable_index_prices,
        }


__all__ = ["PremiumAssembler"]
