"""One identity for a closed trade, so every decoder names the same trade.

A ClosedTrade on the bus carries no id of its own: it is the round trip from
first open to flat, and the recorders, excursion tracker and decoders each
saw it by venue, symbol and opening time. Those three are what make two
closed trades distinct, so they are the id -- derived the same way by every
part that needs one rather than by twenty parts each inventing a key.
"""

from __future__ import annotations


def closed_trade_id(trade) -> str:
    return f"{trade.venue_id}:{trade.symbol}:{trade.opened_at_ns}"


__all__ = ["closed_trade_id"]
