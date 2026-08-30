"""position-flattener: close every open position at market when a human says close.

`close-positions` has been an instruction `human-override-reader` understands
since the door was built, and until 2026-08-27 nothing acted on it. The reading
reached `trading-halt-decider`, which raised a halt carrying `may_close_positions`,
and `halt-enforcer`, which turned it into a zero risk limit -- so the instruction
stopped the bot opening anything new and never closed anything at all. An
operator who wrote it and walked away would come back to the same book, held by a
system that had recorded their instruction and obeyed half of it.

Exits happened one way before this part: `stop-order-manager`'s resting stops
firing. On 2026-08-27, ten of the seventeen open positions had no stop resting,
so for those there was no path to flat at all.

**A market order, not a tightened stop.** Moving each stop to the touch would
close the book with machinery that already exists, and it would be a lie in the
journal: the position would read as stopped out at its risk limit when what
actually happened is that a human asked for it to be closed. What the record says
about why a trade ended is the raw material every learner in this system trains
on.

**One instruction, one exit per position.** The order is placed once and repeated
only after `order_latency_maximum` -- the longest the paper book will hold an
order before answering -- because an exit not filled by then has genuinely not
been taken, while an exit repeated sooner is two closes racing for one position.

**Forgetting is scoped to one instruction.** What has been sent is remembered
until the override goes away, and a `close-positions` written a week later
flattens again. A part that remembered across instructions would refuse to close
a book it had closed once before. The flattening begins on the transition into
it rather than on the override's id, because an override written without an
`override_id` is named `override-{sequence}` by the reader and that sequence
increments on every read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import (
    BUY,
    LIVE_VENUE,
    LONG,
    MARKET,
    PAPER_BOOK,
    ROUTED,
    SELL,
    OrderRequest,
)

PART_ID = "position-flattener"

PART_DECLARATION = PartDeclaration(
    part_id="position-flattener",
    consumes=("human-override", "position", "money-mode"),
    produces=("order-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

# The one instruction this part acts on. `stop-everything` and `stop-trading`
# stop the bot deciding; only this one says the book itself must go. Reading
# them as one would close a book because a human wanted the machine quiet.
CLOSE_POSITIONS = "close-positions"

PAPER = "paper"
LIVE = "live"


@dataclass
class FlattenerStanding:
    overrides_read: int = 0
    # Ticks on which the door held nothing. Counted separately, because
    # `overrides_read` climbing once a second on an empty door would report that
    # an instruction was read when none was written -- and the two states must
    # not look the same on a board (Rule 8). With no override this is the counter
    # that moves and every other one holds still.
    reads_with_no_override: int = 0
    # Instructions this part deliberately did nothing about. Counted, because
    # "the override said stop-trading" and "no override arrived" are different
    # facts and a board that showed neither would look identical in both.
    instructions_not_about_the_book: int = 0
    open_positions: int = 0
    exits_placed: int = 0
    exits_repeated: int = 0
    waiting_for_a_fill: int = 0
    positions_closed_since_the_instruction: int = 0
    refused_no_money_mode: int = 0
    instructions_acted_on: int = 0
    # Positions still open whose allowance is spent -- the whole of what they
    # were seen holding has already been asked for. This is a fault and not
    # progress: something downstream is not filling or not reporting, and asking
    # again would sell a position the bot no longer has. Counted so it is
    # visible, because the alternative to counting it was overshooting.
    positions_at_the_cap: int = 0
    # How much more has been asked for than was ever held, summed over positions.
    # It must stay at zero. It is reported rather than asserted, because the
    # whole reason this counter exists is that it did not stay at zero and
    # nothing anywhere said so.
    quantity_asked_beyond_the_position: float = 0.0
    # How long the oldest unfilled exit has been asked for. An exit is repeated
    # for as long as the position is open, which is right for an order lost in
    # transit and wrong to leave silent: measured 2026-08-27, STORJUSDT was
    # refused 114 times for having no price, because the contract settled on the
    # venue the day before and no market exists to close it into. A number that
    # climbs says that; a repeat counter alone reads like progress.
    longest_wait_seconds: float = 0.0


@dataclass(frozen=True)
class HeldPosition:
    """One open position, as much of it as closing it needs."""

    venue_id: str
    symbol: str
    quantity: float
    direction: str


class PositionFlattener:
    """Turns a human's close-positions into one market exit per open position."""

    def __init__(
        self,
        repeat_after_seconds: float,
        quantity_increment: float,
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
        self._repeat_after_ns = int(repeat_after_seconds * 1_000_000_000)
        self._quantity_increment = quantity_increment
        self._now_ns = now_ns
        self._held: dict[tuple[str, str], HeldPosition] = {}
        self._money_mode: str | None = None
        # The instruction being acted on, and what has been sent under it.
        self._acting_on: str | None = None
        self._sent_at_ns: dict[tuple[str, str], int] = {}
        # When this position's exit was first asked for under this instruction,
        # kept apart from the last ask so a repeat does not reset the wait.
        self._first_asked_at_ns: dict[tuple[str, str], int] = {}
        self._closed_under_this_instruction: set[tuple[str, str]] = set()
        # How much of this position has been asked for and not yet answered, and
        # what it was last seen holding so a fill can be told from a fresh entry.
        # Together they are the only thing that stops a repeat from selling the
        # position twice -- see `exits_to_place`.
        self._outstanding: dict[tuple[str, str], float] = {}
        self._last_seen_quantity: dict[tuple[str, str], float] = {}
        self._quantity_asked_for: dict[tuple[str, str], float] = {}
        self._quantity_seen_filled: dict[tuple[str, str], float] = {}
        self._sequence = 0
        self.standing = FlattenerStanding()

    def observe_money_mode(self, mode: str | None) -> None:
        """Where an exit is sent. Never defaulted: paper and live are not
        interchangeable, and guessing one is how a paper instruction reaches a
        venue or a live book is closed on a simulator that owns nothing."""
        if mode in (PAPER, LIVE):
            self._money_mode = mode

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        # A position that shrank was filled into, and that fill answered part of
        # what is outstanding. This is the only evidence this part has that an
        # ask was taken -- it produces `order-request` and consumes `position`,
        # so a fill reaches it as the position getting smaller and in no other
        # way. Growing is a fresh entry, not an answer, and leaves the
        # outstanding ask exactly where it was.
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
                if self._acting_on is not None and key in self._sent_at_ns:
                    self._closed_under_this_instruction.add(key)
            self.standing.open_positions = len(self._held)
            self.standing.positions_closed_since_the_instruction = len(
                self._closed_under_this_instruction
            )
            return
        self._held[key] = HeldPosition(
            venue_id=position.venue_id,
            symbol=position.symbol,
            quantity=position.quantity,
            direction=position.direction,
        )
        self.standing.open_positions = len(self._held)

    def observe_override(self, override) -> None:
        """The instruction, as the reader publishes it.

        An override that is not active, has been revoked, or says something else
        ends the flattening: what was sent is forgotten so the next
        `close-positions` is acted on rather than treated as already done.
        """
        if override is None:
            self.standing.reads_with_no_override += 1
            self._forget_the_instruction()
            return
        self.standing.overrides_read += 1
        instruction = getattr(override, "instruction", None)
        is_active = bool(getattr(override, "is_active", False))
        if not is_active or instruction != CLOSE_POSITIONS:
            if instruction is not None and instruction != CLOSE_POSITIONS:
                self.standing.instructions_not_about_the_book += 1
            self._forget_the_instruction()
            return
        if self._acting_on is None:
            # A new flattening begins on the transition into it, never on a
            # change of override_id while it is already running.
            # `human-override-reader` defaults an unnamed override's id to
            # `override-{sequence}` and the sequence increments on every read --
            # once a second -- so an operator who wrote the file without an
            # override_id would look like a new instruction every tick, and this
            # part would place a fresh exit for every open position every second.
            self._acting_on = str(getattr(override, "override_id", CLOSE_POSITIONS))
            self.standing.instructions_acted_on += 1

    def _forget_the_instruction(self) -> None:
        self._acting_on = None
        self._sent_at_ns.clear()
        self._first_asked_at_ns.clear()
        self._closed_under_this_instruction.clear()
        self._outstanding.clear()
        self._last_seen_quantity.clear()
        self._quantity_asked_for.clear()
        self._quantity_seen_filled.clear()
        self.standing.waiting_for_a_fill = 0
        self.standing.longest_wait_seconds = 0.0
        self.standing.positions_closed_since_the_instruction = 0
        self.standing.positions_at_the_cap = 0
        self.standing.quantity_asked_beyond_the_position = 0.0

    @property
    def is_flattening(self) -> bool:
        return self._acting_on is not None

    def exits_to_place(self) -> tuple:
        """One market exit per open position, or nothing at all.

        Nothing at all is the answer in every ordinary tick: no instruction, or
        an instruction whose exits are already in flight.

        **A position of size Q never has more than Q asked for at one time.**
        That invariant was missing until 2026-08-30 and the absence of it turned
        this part into the opposite of what it is for. The repeat fired on a
        timer alone, with no account of what was already asked and unanswered,
        and every repeat carried a fresh `client_order_id` -- so
        `paper-fill-simulator`'s duplicate guard, which keys on that id, could
        never refuse one. The repeats did not replace each other, they
        accumulated: measured on the live spine, `exits_placed` reached 426 with
        `exits_repeated` 411 against `open_positions` 2, and the book held 2,199
        of them in flight. They then filled, all of them.

            flatten sell TURBOUSDT  13,020,303  against ~8,500,000 held
                                                -> a NEW 4,457,934 short
            flatten sell VETUSDT     5,915,542  against    731,927 held
            1,048,525 USDT of flatten notional against a 190,900 USDT book

        A close that overshoots does not stop at flat, it reverses -- the failure
        `OrderRequest.cancels_client_order_id` was written for, in its own words:
        "a stop that triggers on nothing opens the opposite position".

        **The bound is outstanding quantity, not a repeat timer, and not
        cancel-replace either.** Withdrawing the previous ask would also bound
        it, and that was the first fix written here -- but it only holds while
        cancels land, and the live book reported `cancels_for_an_unknown_order`
        360 times against market orders it had already passed on. A bound that
        depends on a cancel arriving is not a bound. So:

            allowance = what is held now - what is already asked and unanswered

        and an ask is only ever answered by evidence: the position getting
        smaller, which is the only way a fill reaches a part that produces
        `order-request` and consumes `position`. An unanswered ask therefore
        keeps its allowance spent for as long as it stays unanswered, and the
        timer decides *when* it is worth looking again, never *how much*.

        The consequence is deliberate: an exit that is genuinely lost is not
        re-sent. That is the right trade here, because the 411 repeats were not
        lost -- they were slow, and every one of them was eventually taken. A
        position still open with its allowance spent is a fault to report, and
        it is counted in `positions_at_the_cap` rather than acted on.
        """
        if self._acting_on is None:
            return ()
        if self._money_mode is None:
            self.standing.refused_no_money_mode += 1
            return ()

        now = self._now_ns()
        exits = []
        at_the_cap = 0
        for key, held in sorted(self._held.items()):
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
            # Only what is not already asked for. Never the whole position again.
            quantity = allowance
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
                    held, quantity=quantity, repeated=sent_at is not None, at_ns=now
                )
            )
        self.standing.positions_at_the_cap = at_the_cap
        # What has been asked for beyond what was ever there to sell: everything
        # asked, less everything a fill accounted for, less what each position
        # still holds. It must stay at zero, and it is reported rather than
        # asserted because the whole reason it exists is that it did not.
        self.standing.quantity_asked_beyond_the_position = sum(
            max(
                0.0,
                asked
                - self._quantity_seen_filled.get(key, 0.0)
                - abs(self._held[key].quantity if key in self._held else 0.0),
            )
            for key, asked in self._quantity_asked_for.items()
        )

        waiting = [self._sent_at_ns[key] for key in self._sent_at_ns if key in self._held]
        self.standing.waiting_for_a_fill = len(waiting)
        self.standing.longest_wait_seconds = (
            (now - min(self._first_asked_at_ns[key] for key in self._sent_at_ns if key in self._held))
            / 1_000_000_000
            if waiting
            else 0.0
        )
        return tuple(exits)

    def _exit_for(
        self, held: HeldPosition, quantity: float, repeated: bool, at_ns: int
    ) -> OrderRequest:
        self._sequence += 1
        side = SELL if held.direction == LONG else BUY
        again = " again" if repeated else ""
        return OrderRequest(
            client_order_id=f"flatten-{held.venue_id}-{held.symbol}-{self._sequence}",
            destination=PAPER_BOOK if self._money_mode == PAPER else LIVE_VENUE,
            venue_id=held.venue_id,
            symbol=held.symbol,
            side=side,
            quantity=quantity,
            # A market order carries neither: an instruction to close now is not
            # an instruction to wait for a price, and a limit here would be a
            # position left open at the first tick that did not reach it.
            limit_price=0.0,
            stop_price=0.0,
            order_type=MARKET,
            slice_sequence=1,
            slice_count=1,
            at_second=0.0,
            outcome=ROUTED,
            reason=(
                f"a human override said {CLOSE_POSITIONS}; closing {held.direction} "
                f"{quantity:g} of {abs(held.quantity):g} {held.symbol} at market{again}"
            ),
            routed_at_ns=at_ns,
            # An exit returns whatever the position committed, which the account
            # already knows -- the same reading stop-order-manager takes.
            leverage=1.0,
        )


def describe_flattening(flattener: PositionFlattener) -> dict:
    return {
        "part_id": PART_ID,
        "is_flattening": 1.0 if flattener.is_flattening else 0.0,
        "overrides_read": flattener.standing.overrides_read,
        "reads_with_no_override": flattener.standing.reads_with_no_override,
        "instructions_acted_on": flattener.standing.instructions_acted_on,
        "instructions_not_about_the_book": flattener.standing.instructions_not_about_the_book,
        "open_positions": flattener.standing.open_positions,
        "exits_placed": flattener.standing.exits_placed,
        "exits_repeated": flattener.standing.exits_repeated,
        "waiting_for_a_fill": flattener.standing.waiting_for_a_fill,
        "positions_at_the_cap": flattener.standing.positions_at_the_cap,
        "quantity_asked_beyond_the_position": (
            flattener.standing.quantity_asked_beyond_the_position
        ),
        "positions_closed_since_the_instruction": (
            flattener.standing.positions_closed_since_the_instruction
        ),
        "refused_no_money_mode": flattener.standing.refused_no_money_mode,
        "longest_wait_seconds": flattener.standing.longest_wait_seconds,
    }


def run_position_flattener(
    flattener: PositionFlattener, control_socket, read_inputs, publish_exits,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_inputs(flattener)
        exits = flattener.exits_to_place()
        if exits:
            publish_exits(exits)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_flattening(flattener),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The override is a level and so is a position: an instruction is true until it
    is revoked, and a position is held until it is closed. Both are read as the
    latest value rather than as a batch of events, because acting on the same
    instruction twice per tick would place an exit per tick.
    """
    from runtime.input_assembly import LatestByKey, LatestValue

    overrides = LatestValue(read=context.bus.reader("human-override"))
    # Bounded, because `human-override-reader` republishes the instruction on
    # every tick while the file exists and publishes **nothing at all** once it
    # is gone. Held as an unbounded level, a revoked instruction reads as still
    # standing: measured 2026-08-27, the override file was deleted and this part
    # went on placing exits, because nothing ever said the door was empty. The
    # bound is what turns "nobody has restated it" into "there is no override".
    override_maximum_age_ns = int(
        context.number("human_override_reading_maximum_age_seconds") * 1_000_000_000
    )
    positions = LatestByKey(
        read=context.bus.reader("position"),
        key_of=lambda payload: (payload.venue_id, payload.symbol),
    )
    modes = LatestValue(read=context.bus.reader("money-mode"))
    publish = context.bus.publisher_for("order-request")

    def read_inputs(flattener: PositionFlattener) -> None:
        mode = modes.value()
        flattener.observe_money_mode(getattr(mode, "mode", None))
        for position in positions.values():
            flattener.observe_position(position)
        override = overrides.value()
        observed_at_ns = overrides.observed_at_ns
        is_current = (
            override is not None
            and observed_at_ns is not None
            and time.time_ns() - observed_at_ns <= override_maximum_age_ns
        )
        # None is the honest reading of an empty door, and it ends a flattening
        # the same way a revoked or expired override does.
        flattener.observe_override(override if is_current else None)

    return run_position_flattener(
        flattener=PositionFlattener(
            # Derived rather than chosen: order_latency_maximum is the longest
            # the paper book will hold an order before it answers, so an exit
            # unfilled past it has genuinely not been taken. Anything shorter
            # repeats an order that is still in flight.
            repeat_after_seconds=context.number("order_latency_maximum"),
            # The same step an order is snapped to, so the floor under the
            # unsold remainder is the smallest thing an order could actually
            # sell. Read rather than chosen: a number picked here would be a
            # second opinion about what "nothing left to close" means, free to
            # disagree with the one the book rounds to (RL-061).
            quantity_increment=context.number("order_quantity_increment"),
        ),
        control_socket=context.control_socket,
        read_inputs=read_inputs,
        publish_exits=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
