"""Turning what a venue said about a quote into what a symbol's quote is.

A venue that restates both sides every message needs nothing from this file. A
venue that amends does: Bybit's `tickers` stream sends one snapshot per
subscription and then deltas carrying only what moved, so a message can name a
bid and no ask at all. The adapter reports the absent side as None rather than
inventing it (see `QuoteChange`), which leaves exactly one question -- who
remembers the other side -- and this is the answer.

**The age of a merged quote is the age of its stalest side.** If the bid moved a
millisecond ago and the ask has not been mentioned for forty seconds, the quote
is forty seconds old, because that is how old the worse half of it is. Taking the
newer stamp would make every merged quote look as fresh as its most active side,
which is the same lie as stamping a re-delivered price with the moment it was
re-delivered -- the failure of 2026-08-23, arrived at by a different road.

Nothing here decides whether a quote is fresh enough to act on. That bound is per
symbol and learned, and it lives in `runtime/price_staleness.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.venues.venue_adapter import NormalisedQuote, QuoteChange


@dataclass
class _SideState:
    """One side of one symbol's quote, and when the venue last said it."""

    price: float
    quantity: float
    said_at_ns: int


class QuoteAssembler:
    """Every symbol's last complete quote on one venue, kept across amending messages.

    One assembler per venue, because a symbol trades on both and the two quotes
    are different facts. It holds no clock and asks nothing of the world: given
    the same changes in the same order it produces the same quotes, which is what
    makes it testable against a captured stream rather than against a live one.
    """

    def __init__(self, venue_id: str, amends_rather_than_restates: bool) -> None:
        self._venue_id = venue_id
        self._amends = amends_rather_than_restates
        self._bids: dict[str, _SideState] = {}
        self._asks: dict[str, _SideState] = {}
        # A venue that declared it restates and then sent half a quote. Counted
        # rather than raised: the feed carrying on is worth more than the
        # assertion, and a number that is not zero says the adapter's declaration
        # is wrong far more usefully than a part that died once.
        self.restated_incompletely = 0
        # Changes that named neither side -- Bybit amends funding, open interest
        # and last price on the same stream, and most of its messages touch no
        # quote at all.
        self.changes_naming_no_side = 0
        self.quotes_completed = 0
        # Changes that could not complete a quote because the other side has never
        # been seen. Only possible before a symbol's first snapshot.
        self.changes_still_incomplete = 0

    def apply_change(self, change: QuoteChange) -> NormalisedQuote | None:
        """Fold one message into what is known, and return the quote if it is whole.

        None means this message did not leave a complete quote for that symbol --
        either it named no side at all, or it named one and the other has never
        been seen. It never means the quote is unchanged: an unchanged quote is
        still returned, because a reader asking "what is it now" is entitled to an
        answer whether or not the venue moved it.
        """
        if change.is_snapshot and not change.is_complete() and not self._amends:
            self.restated_incompletely += 1

        named_a_side = False
        if change.bid_price is not None and change.bid_quantity is not None:
            named_a_side = True
            self._bids[change.symbol] = _SideState(
                price=change.bid_price,
                quantity=change.bid_quantity,
                said_at_ns=change.venue_time_ns,
            )
        if change.ask_price is not None and change.ask_quantity is not None:
            named_a_side = True
            self._asks[change.symbol] = _SideState(
                price=change.ask_price,
                quantity=change.ask_quantity,
                said_at_ns=change.venue_time_ns,
            )

        if not named_a_side:
            self.changes_naming_no_side += 1
            return None

        bid = self._bids.get(change.symbol)
        ask = self._asks.get(change.symbol)
        if bid is None or ask is None:
            self.changes_still_incomplete += 1
            return None

        self.quotes_completed += 1
        return NormalisedQuote(
            venue_id=self._venue_id,
            symbol=change.symbol,
            bid_price=bid.price,
            bid_quantity=bid.quantity,
            ask_price=ask.price,
            ask_quantity=ask.quantity,
            # The stalest side. See this module's docstring: a quote is exactly as
            # old as the half of it that has not been mentioned for longest.
            venue_time_ns=min(bid.said_at_ns, ask.said_at_ns),
        )

    def symbols_held(self) -> int:
        """How many symbols have both sides, so a quote can be answered for them."""
        return len(self._bids.keys() & self._asks.keys())

    def describe(self) -> dict:
        """What this assembler has done, for the part's standing."""
        return {
            "symbols_held": self.symbols_held(),
            "quotes_completed": self.quotes_completed,
            "changes_naming_no_side": self.changes_naming_no_side,
            "changes_still_incomplete": self.changes_still_incomplete,
            "restated_incompletely": self.restated_incompletely,
        }
