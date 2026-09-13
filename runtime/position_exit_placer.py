"""Turning "these positions must go" into market exits that never oversell.

Two parts need this and they differ only in *why* a position must go:
`position-flattener` because a human wrote `close-positions`, and
`pre-expiry-position-closer` because the contract is about to stop existing.
The mechanics of getting to flat without overshooting are identical, and they
are not obvious -- they are the residue of a live incident, so they are written
once here rather than reproduced in the second part.

**A position of size Q never has more than Q asked for at one time.** That
invariant was missing from `position-flattener` until 2026-08-30 and its absence
turned that part into the opposite of what it is for. The repeat fired on a
timer alone, with no account of what was already asked and unanswered, and every
repeat carried a fresh `client_order_id`, so `paper-fill-simulator`'s duplicate
guard -- which keys on that id -- could never refuse one. The repeats did not
replace each other, they accumulated: `exits_placed` reached 426 with
`exits_repeated` 411 against `open_positions` 2, and the book held 2,199 of them
in flight. They then filled, all of them:

    flatten sell TURBOUSDT  13,020,303  against ~8,500,000 held
                                        -> a NEW 4,457,934 short
    flatten sell VETUSDT     5,915,542  against    731,927 held
    1,048,525 USDT of flatten notional against a 190,900 USDT book

A close that overshoots does not stop at flat, it reverses.

**The bound is outstanding quantity, not a repeat timer, and not cancel-replace
either.** Withdrawing the previous ask would also bound it, and that was the
first fix written -- but it only holds while cancels land, and the live book
reported `cancels_for_an_unknown_order` 360 times against market orders it had
already passed on. A bound that depends on a cancel arriving is not a bound. So

    allowance = what is held now - what is already asked and unanswered

and an ask is only ever answered by evidence: the position getting smaller,
which is the only way a fill reaches a part that produces `order-request` and
consumes `position`. The timer decides *when* it is worth looking again, never
*how much*.

The consequence is deliberate: an exit that is genuinely lost is not re-sent. A
position still open with its allowance spent is a fault to report --
`positions_at_the_cap` -- rather than something to act on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.trading_types import (
    BUY, LIVE_VENUE, LONG, MARKET, PAPER_BOOK, ROUTED, SELL, OrderRequest,
)

PAPER = "paper"
LIVE = "live"


@dataclass
class ExitPlacerStanding:
    """What the placer has asked for, and what has answered."""

    open_positions: int = 0
    exits_placed: int = 0
    exits_repeated: int = 0
    waiting_for_a_fill: int = 0
    refused_no_money_mode: int = 0
    # Positions still open whose allowance is spent -- the whole of what they
    # were seen holding has already been asked for. A fault, not progress:
    # something downstream is not filling or not reporting, and asking again
    # would sell a position the bot no longer has.
    positions_at_the_cap: int = 0
    # How much more has been asked for than was ever held. It must stay at zero.
    # Reported rather than asserted, because the whole reason it exists is that
    # it did not stay at zero and nothing anywhere said so.
    quantity_asked_beyond_the_position: float = 0.0
    # How long the oldest unfilled exit has been asked for. An exit is repeated
    # for as long as the position is open, which is right for an order lost in
    # transit and wrong to leave silent: measured 2026-08-27, STORJUSDT was
    # refused 114 times for having no price, because the contract had settled on
    # the venue the day before and no market existed to close it into. A number
    # that climbs says that; a repeat counter alone reads like progress.
    longest_wait_seconds: float = 0.0
    # Positions that reached flat after an exit had been asked for under this
    # campaign. On the standing rather than derived, so `forget` clears it with
    # everything else the campaign remembered.
    positions_closed_since_forgetting: int = 0


@dataclass(frozen=True)
class HeldPosition:
    """One open position, as much of it as closing it needs."""

    venue_id: str
    symbol: str
    quantity: float
    direction: str
    # Whose money it is, so the exit is sent where that segment's money mode says
    # and the fill that closes it is charged to the right account (2026-09-05).
    segment: str = ""


class PositionExitPlacer:
    """Holds what is open, and places bounded market exits for what must go."""

    def __init__(
        self,
        repeat_after_seconds: float,
        quantity_increment: float,
        client_order_prefix: str,
        now_ns=time.time_ns,
    ) -> None:
        if repeat_after_seconds <= 0:
            raise ValueError(
                "an exit repeated after no wait at all is two closes racing for one position"
            )
        if quantity_increment <= 0:
            raise ValueError(
                "a quantity step of zero leaves no floor under the unsold remainder, so a "
                "position rounded to nothing would be asked for forever"
            )
        if not client_order_prefix:
            raise ValueError(
                "an exit's client_order_id names why it was placed; without a prefix two "
                "parts closing the same position produce indistinguishable orders"
            )
        self._repeat_after_ns = int(repeat_after_seconds * 1_000_000_000)
        self._quantity_increment = quantity_increment
        self._prefix = client_order_prefix
        self._now_ns = now_ns
        self._held: dict[tuple[str, str], HeldPosition] = {}
        # One money mode per segment (2026-09-05). Three segment bots share one
        # spine and an exit is placed long after the decision that opened the
        # position, so where it is sent is decided by the segment the position
        # itself carries. The empty-string key is what a spine trading one
        # segment produced before positions carried a segment at all.
        self._money_mode_by_segment: dict[str, str] = {}
        self._sent_at_ns: dict[tuple[str, str], int] = {}
        # When this position's exit was first asked for, kept apart from the last
        # ask so a repeat does not reset the wait.
        self._first_asked_at_ns: dict[tuple[str, str], int] = {}
        self._outstanding: dict[tuple[str, str], float] = {}
        self._last_seen_quantity: dict[tuple[str, str], float] = {}
        self._quantity_asked_for: dict[tuple[str, str], float] = {}
        self._quantity_seen_filled: dict[tuple[str, str], float] = {}
        self._closed_since_forgetting: set[tuple[str, str]] = set()
        self._sequence = 0
        self.standing = ExitPlacerStanding()

    # ---- what is open ------------------------------------------------------

    def observe_money_mode(self, mode: str | None, segment: str = "") -> None:
        """Where an exit is sent, for one segment. Never defaulted: paper and
        live are not interchangeable, and guessing one is how a paper instruction
        reaches a venue or a live book is closed on a simulator that owns
        nothing."""
        if mode in (PAPER, LIVE):
            self._money_mode_by_segment[segment] = mode

    def money_mode_for(self, segment: str) -> str | None:
        """The mode for one segment, or the only one there is.

        A position whose segment has a mode uses it and no other. One naming no
        segment takes the only mode set, and only when there is exactly one --
        the rule `runtime.input_assembly.level_for_segment` states, applied to
        modes a caller pushed in rather than a level read off the bus. A
        position with no mode has no exit: this returns None and `exits_for`
        refuses it by name.
        """
        if segment in self._money_mode_by_segment:
            return self._money_mode_by_segment[segment]
        if not segment and len(self._money_mode_by_segment) == 1:
            return next(iter(self._money_mode_by_segment.values()))
        return self._money_mode_by_segment.get("")

    @property
    def money_mode(self) -> str | None:
        """What a caller naming no segment set, kept for the parts and tests that
        deal with one segment."""
        return self._money_mode_by_segment.get("")

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        # A position that shrank was filled into, and that fill answered part of
        # what is outstanding. Growing is a fresh entry, not an answer, and
        # leaves the outstanding ask exactly where it was.
        #
        # Counted before the flat case returns, never inside the branch below.
        # The last fill on a position is the one that closes it, and skipping
        # that one leaves the whole closed quantity looking like quantity nobody
        # accounted for: measured live 2026-08-30, 35 positions closed correctly
        # while `quantity_asked_beyond_the_position` read 1,904,948 -- a counter
        # calling a clean flatten an overshoot. A number that reads as a fault
        # when there is none costs the same as one that reads healthy when there
        # is (Rule 8).
        was = self._last_seen_quantity.get(key)
        now_held = abs(position.quantity)
        if was is not None and now_held < was:
            filled = was - now_held
            self._quantity_seen_filled[key] = (
                self._quantity_seen_filled.get(key, 0.0) + filled
            )
            self._outstanding[key] = max(0.0, self._outstanding.get(key, 0.0) - filled)
        self._last_seen_quantity[key] = now_held
        if position.is_flat:
            if key in self._held:
                del self._held[key]
                if key in self._sent_at_ns:
                    self._closed_since_forgetting.add(key)
                    self.standing.positions_closed_since_forgetting = len(
                        self._closed_since_forgetting
                    )
            self.standing.open_positions = len(self._held)
            return
        self._held[key] = HeldPosition(
            venue_id=position.venue_id,
            symbol=position.symbol,
            quantity=position.quantity,
            direction=position.direction,
            segment=getattr(position, "segment", ""),
        )
        self.standing.open_positions = len(self._held)

    @property
    def held(self) -> dict:
        """What is open, by (venue_id, symbol). Read to decide what must go."""
        return dict(self._held)

    @property
    def positions_closed_since_forgetting(self) -> int:
        return self.standing.positions_closed_since_forgetting

    def forget(self) -> None:
        """End one campaign: what was sent under it is no longer remembered.

        A part that remembered across campaigns would refuse to close a book it
        had closed once before.
        """
        self._sent_at_ns.clear()
        self._first_asked_at_ns.clear()
        self._closed_since_forgetting.clear()
        self._outstanding.clear()
        self._last_seen_quantity.clear()
        self._quantity_asked_for.clear()
        self._quantity_seen_filled.clear()
        self.standing.waiting_for_a_fill = 0
        self.standing.longest_wait_seconds = 0.0
        self.standing.positions_at_the_cap = 0
        self.standing.quantity_asked_beyond_the_position = 0.0
        self.standing.positions_closed_since_forgetting = 0

    # ---- what to send ------------------------------------------------------

    def exits_for(self, keys, reason_for) -> tuple:
        """One bounded market exit per named position, or nothing at all.

        Nothing at all is the answer on an ordinary tick: nothing due, or
        everything due already asked for and unanswered. `reason_for` is called
        with the held position and the quantity, and returns the sentence that
        goes in the journal -- it is the caller's, because why a trade ended is
        the raw material every learner in this system trains on, and "a human
        said close" and "the contract expires today" must never read the same.
        """
        now = self._now_ns()
        due = sorted(key for key in keys if key in self._held)
        exits = []
        at_the_cap = 0
        for key in due:
            held = self._held[key]
            # Refused per position, not per tick: on a spine trading three
            # segments one segment's mode being unreadable must not stop the
            # other two closing what they hold.
            if self.money_mode_for(held.segment) is None:
                self.standing.refused_no_money_mode += 1
                continue
            outstanding = self._outstanding.get(key, 0.0)
            allowance = abs(held.quantity) - outstanding
            # Below one order step there is nothing an order could sell, so an
            # allowance that small is spent rather than nearly spent.
            if allowance < self._quantity_increment:
                if outstanding > 0:
                    at_the_cap += 1
                continue
            sent_at = self._sent_at_ns.get(key)
            if sent_at is not None and now - sent_at < self._repeat_after_ns:
                continue
            quantity = allowance  # only what is not already asked for
            if sent_at is not None:
                self.standing.exits_repeated += 1
            self._sent_at_ns[key] = now
            self._first_asked_at_ns.setdefault(key, now)
            self._quantity_asked_for[key] = (
                self._quantity_asked_for.get(key, 0.0) + quantity
            )
            self._outstanding[key] = outstanding + quantity
            self.standing.exits_placed += 1
            exits.append(
                self._exit_for(
                    held,
                    quantity=quantity,
                    reason=reason_for(held, quantity, sent_at is not None),
                    at_ns=now,
                )
            )
        self.standing.positions_at_the_cap = at_the_cap
        self._recount(now)
        return tuple(exits)

    def _recount(self, now: int) -> None:
        # What has been asked for beyond what was ever there to sell: everything
        # asked, less everything a fill accounted for, less what each position
        # still holds.
        self.standing.quantity_asked_beyond_the_position = sum(
            max(
                0.0,
                asked
                - self._quantity_seen_filled.get(key, 0.0)
                - abs(self._held[key].quantity if key in self._held else 0.0),
            )
            for key, asked in self._quantity_asked_for.items()
        )
        waiting = [key for key in self._sent_at_ns if key in self._held]
        self.standing.waiting_for_a_fill = len(waiting)
        self.standing.longest_wait_seconds = (
            (now - min(self._first_asked_at_ns[key] for key in waiting)) / 1_000_000_000
            if waiting
            else 0.0
        )

    def _exit_for(
        self, held: HeldPosition, quantity: float, reason: str, at_ns: int
    ) -> OrderRequest:
        self._sequence += 1
        return OrderRequest(
            # `at_ns` is in the id since 2026-09-13: `_sequence` restarts at zero
            # with the part, so an exit asked for after a restart could reuse an id
            # already spent, and the idempotency stamper and the fill ids built from
            # it would both treat a new order as an old one.
            client_order_id=f"{self._prefix}-{held.venue_id}-{held.symbol}-{at_ns}-{self._sequence}",
            destination=(
                PAPER_BOOK
                if self.money_mode_for(held.segment) == PAPER
                else LIVE_VENUE
            ),
            venue_id=held.venue_id,
            symbol=held.symbol,
            side=SELL if held.direction == LONG else BUY,
            quantity=quantity,
            # A market order carries neither: an instruction to close now is not
            # an instruction to wait for a price, and a limit here would be a
            # position left open at the first tick that did not reach it.
            limit_price=0.0,
            stop_price=0.0,
            order_type=MARKET,
            # The exit belongs to the segment the position does, so the fill that
            # closes it reaches that segment's account.
            segment=held.segment,
            slice_sequence=1,
            slice_count=1,
            at_second=0.0,
            outcome=ROUTED,
            reason=reason,
            routed_at_ns=at_ns,
            # An exit returns whatever the position committed, which the account
            # already knows -- the same reading stop-order-manager takes.
            leverage=1.0,
        )


__all__ = [
    "ExitPlacerStanding",
    "HeldPosition",
    "LIVE",
    "PAPER",
    "PositionExitPlacer",
]
