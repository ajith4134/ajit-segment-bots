"""An update that arrived before its instrument listing, held until it resolves.

Three parts join a broker feed update to the instrument master before they can
republish it: `broker-market-data-bridge`, `broker-candle-bridge` and
`broker-order-book-bridge` each look a `trading_symbol` up by `instrument_key`
and hand back `None` when the key is not known yet. All three then dropped that
update and never saw it again.

That is not a rare corner. The listings arrive on a restatement conveyor --
`subscribed-instrument-listing-filter` emits the subscribed subset a slice at a
time so ten consumers are not drained at the full 102,940-row rate -- while the
feed delivers its whole snapshot the moment the socket connects. The snapshot
therefore always arrives first, against an empty map.

Measured on the live spine, 2026-09-06, twelve minutes after start:

    broker-market-data-bridge   received 2,000 broker-market-data   published 0 market-data
    broker-candle-bridge        received 1,878 broker-candle        published 0 candle
    broker-order-book-bridge    received 1,999 broker-order-book-snapshot  published 0

with `instruments_resolved` at 2,000 in all three -- every key was resolvable by
the time anyone looked, and every update that needed it had already been thrown
away. All 1,157 instrument keys on that day's tape resolve in the instrument
master, so nothing was unresolvable; they were merely early. On a moving market
the next tick replaces what was lost, which is why this never showed up as a
fault; on a quiet one, and across every instrument that ticks rarely, the loss
is permanent.

**Newest per key, not a queue.** All three of these updates are levels: an LTP
restates the last print, a depth update carries full depth rather than deltas,
and Upstox restates the forming bar continuously. Holding the newest and
discarding the one it replaced is what a level means; a queue would release a
burst of stale prices at resolution time and every one of them would read as a
real new print (`runtime/level_publishing.py` draws the same distinction on the
publish side).

**Bounded, because the bound is what makes it safe to hold anything.** A feed
can name an instrument the master has not listed at all -- a newly introduced
contract, a segment the master fetch missed -- and without a bound those keys
accumulate for the life of the process. The bound is the caller's, from a named
setting, never a literal here (RL-061); when it is reached the oldest-held key is
dropped and counted, so "we are holding updates for instruments nobody can name"
is a number on the part's own standing rather than a silent leak.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Iterator, TypeVar

Update = TypeVar("Update")


class UpdatesAwaitingInstrumentListing:
    """Holds the newest unresolved update per instrument key, bounded.

    The caller stays in charge of what "resolved" means: it hands in the same
    predicate it already uses to look a listing up, so this holder knows nothing
    about instrument masters, trading symbols or the bus (T-4).
    """

    def __init__(self, *, held_instrument_limit: int) -> None:
        if held_instrument_limit < 1:
            raise ValueError(
                "held_instrument_limit must be at least 1 -- a holder that can hold "
                f"nothing is the dropping behaviour it exists to replace (got {held_instrument_limit})"
            )
        self._held_instrument_limit = held_instrument_limit
        self._newest_by_key: OrderedDict[str, Update] = OrderedDict()
        # Counted rather than logged: a part's standing is where this belongs,
        # so "held 1,900 updates for instruments nobody ever listed" is visible
        # on the board instead of only in a journal nobody reads (Rule 8).
        self.updates_held = 0
        self.updates_replaced_while_held = 0
        self.updates_released = 0
        self.instruments_dropped_at_limit = 0

    def hold(self, instrument_key: str, update: Update) -> None:
        """Remember this update as the newest unresolved one for its instrument."""
        if instrument_key in self._newest_by_key:
            self.updates_replaced_while_held += 1
            self._newest_by_key.move_to_end(instrument_key)
        self._newest_by_key[instrument_key] = update
        self.updates_held += 1
        while len(self._newest_by_key) > self._held_instrument_limit:
            self._newest_by_key.popitem(last=False)
            self.instruments_dropped_at_limit += 1

    def release_resolvable(self, is_resolvable: Callable[[str], bool]) -> Iterator[Update]:
        """Yield each held update whose instrument can now be resolved.

        Released updates leave the holder, so an update is republished once and
        an instrument that never becomes resolvable never blocks the ones that do.
        """
        resolved_keys = [key for key in self._newest_by_key if is_resolvable(key)]
        for key in resolved_keys:
            update = self._newest_by_key.pop(key)
            self.updates_released += 1
            yield update

    @property
    def instruments_awaiting_listing(self) -> int:
        return len(self._newest_by_key)

    def describe(self) -> dict[str, float]:
        """The four numbers a part puts on its own standing."""
        return {
            "updates_awaiting_listing": float(self.instruments_awaiting_listing),
            "updates_released_after_listing": float(self.updates_released),
            "updates_replaced_while_awaiting": float(self.updates_replaced_while_held),
            "instruments_dropped_at_hold_limit": float(self.instruments_dropped_at_limit),
        }


__all__ = ["UpdatesAwaitingInstrumentListing"]
