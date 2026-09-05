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
from runtime.position_exit_placer import PositionExitPlacer

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


class FlattenerStanding:
    """What this part decided, and what the placer did about it.

    The placer's counters are forwarded rather than copied. They were this
    part's own fields until the placing was extracted for
    `pre-expiry-position-closer` to share, and they are read by the board, the
    part monitor and this part's own tests -- a counter that changes its name is
    a tile that goes blank without anything having gone wrong, and a counter
    copied on write is one free to disagree with the thing it describes.
    """

    def __init__(self, placer_standing) -> None:
        self._placer = placer_standing
        self.overrides_read = 0
        # Ticks on which the door held nothing. Counted separately, because
        # `overrides_read` climbing once a second on an empty door would report
        # that an instruction was read when none was written -- and the two
        # states must not look the same on a board (Rule 8).
        self.reads_with_no_override = 0
        # Instructions this part deliberately did nothing about. Counted,
        # because "the override said stop-trading" and "no override arrived" are
        # different facts and a board showing neither would look identical.
        self.instructions_not_about_the_book = 0
        self.instructions_acted_on = 0

    positions_closed_since_the_instruction = property(
        lambda self: self._placer.positions_closed_since_forgetting
    )
    open_positions = property(lambda self: self._placer.open_positions)
    exits_placed = property(lambda self: self._placer.exits_placed)
    exits_repeated = property(lambda self: self._placer.exits_repeated)
    waiting_for_a_fill = property(lambda self: self._placer.waiting_for_a_fill)
    positions_at_the_cap = property(lambda self: self._placer.positions_at_the_cap)
    refused_no_money_mode = property(lambda self: self._placer.refused_no_money_mode)
    longest_wait_seconds = property(lambda self: self._placer.longest_wait_seconds)
    quantity_asked_beyond_the_position = property(
        lambda self: self._placer.quantity_asked_beyond_the_position
    )


class PositionFlattener:
    """Turns a human's close-positions into one market exit per open position.

    The exits themselves are placed by `runtime.position_exit_placer`, which
    carries the bound that stops a repeat from selling a position twice. This
    class owns only the instruction's lifecycle: when a flattening starts, and
    when what was sent under it is forgotten.
    """

    def __init__(
        self,
        repeat_after_seconds: float,
        quantity_increment: float,
        now_ns=time.time_ns,
    ) -> None:
        self._placer = PositionExitPlacer(
            repeat_after_seconds=repeat_after_seconds,
            quantity_increment=quantity_increment,
            client_order_prefix="flatten",
            now_ns=now_ns,
        )
        self._acting_on: str | None = None
        self.standing = FlattenerStanding(self._placer.standing)

    @property
    def placer(self) -> PositionExitPlacer:
        return self._placer

    def observe_money_mode(self, mode: str | None) -> None:
        self._placer.observe_money_mode(mode)

    def observe_position(self, position) -> None:
        self._placer.observe_position(position)

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
        self._placer.forget()

    @property
    def is_flattening(self) -> bool:
        return self._acting_on is not None

    def exits_to_place(self) -> tuple:
        """One market exit per open position, or nothing at all.

        Nothing at all is the answer in every ordinary tick: no instruction, or
        an instruction whose exits are already in flight. The bound that keeps a
        repeat from overselling is the placer's, and its reasoning -- the
        2026-08-30 incident that put 1,048,525 USDT of flatten notional against a
        190,900 USDT book -- is written there.
        """
        if self._acting_on is None:
            return ()

        def why(held, quantity, repeated):
            again = " again" if repeated else ""
            return (
                f"a human override said {CLOSE_POSITIONS}; closing {held.direction} "
                f"{quantity:g} of {abs(held.quantity):g} {held.symbol} at market{again}"
            )

        return self._placer.exits_for(self._placer.held.keys(), reason_for=why)


def describe_flattening(flattener: PositionFlattener) -> dict:
    """The instruction's own counters, and the placer's beside them.

    Kept under the same names they had before the placer was extracted: these
    are what the board and the part monitor read, and a counter that changes its
    name is a tile that goes blank without anything having gone wrong.
    """
    placer = flattener.placer.standing
    return {
        "part_id": PART_ID,
        "is_flattening": 1.0 if flattener.is_flattening else 0.0,
        "overrides_read": flattener.standing.overrides_read,
        "reads_with_no_override": flattener.standing.reads_with_no_override,
        "instructions_acted_on": flattener.standing.instructions_acted_on,
        "instructions_not_about_the_book": flattener.standing.instructions_not_about_the_book,
        "open_positions": placer.open_positions,
        "exits_placed": placer.exits_placed,
        "exits_repeated": placer.exits_repeated,
        "waiting_for_a_fill": placer.waiting_for_a_fill,
        "positions_at_the_cap": placer.positions_at_the_cap,
        "quantity_asked_beyond_the_position": placer.quantity_asked_beyond_the_position,
        "positions_closed_since_the_instruction": (
            flattener.placer.positions_closed_since_forgetting
        ),
        "refused_no_money_mode": placer.refused_no_money_mode,
        "longest_wait_seconds": placer.longest_wait_seconds,
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
