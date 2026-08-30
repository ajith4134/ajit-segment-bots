"""`close-positions` reaches the book, or it is only half an instruction.

Until 2026-08-27 the override was understood by the reader, raised as a halt by
`trading-halt-decider` and turned into a zero risk limit by `halt-enforcer` --
and no part anywhere placed an exit. An operator who wrote it came back to the
same book. Ten of the seventeen positions open that day had no stop resting, so
for those there was no path to flat at all.
"""

from __future__ import annotations

import pytest

from parts.risk_capital_allocation.position_flattener import (
    CLOSE_POSITIONS,
    PositionFlattener,
)
from runtime.autonomy_types import HumanOverride
from runtime.trading_types import BUY, LIVE_VENUE, MARKET, PAPER_BOOK, SELL, Position

SECOND_NS = 1_000_000_000


class Clock:
    def __init__(self) -> None:
        self.now_ns = 1_000 * SECOND_NS

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * SECOND_NS)


def position(symbol: str, quantity: float, entry: float = 100.0) -> Position:
    return Position(
        venue_id="binance-usdm", symbol=symbol, quantity=quantity,
        average_entry_price=entry, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=1, updated_at_ns=2,
    )


def override(instruction: str = CLOSE_POSITIONS, override_id: str = "first", active: bool = True):
    return HumanOverride(
        override_id=override_id, instruction=instruction, scope="futures",
        is_active=active, issued_at_ns=1, expires_at_ns=None,
        source_reference="~/.config/ajit-segment-bots/override.toml",
    )


def flattener(
    clock: Clock, mode: str = "paper", quantity_increment: float = 0.001
) -> PositionFlattener:
    subject = PositionFlattener(
        repeat_after_seconds=5.0, quantity_increment=quantity_increment, now_ns=clock
    )
    subject.observe_money_mode(mode)
    return subject


def test_nothing_is_closed_without_an_instruction():
    """The ordinary tick. A part that flattens on its own is not a door."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    assert subject.exits_to_place() == ()
    assert not subject.is_flattening


def test_every_open_position_gets_one_market_exit():
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_position(position("ETHUSDT", -3.0))
    subject.observe_override(override())

    exits = subject.exits_to_place()
    assert len(exits) == 2
    by_symbol = {order.symbol: order for order in exits}
    assert by_symbol["BTCUSDT"].side == SELL
    assert by_symbol["ETHUSDT"].side == BUY, "a short is closed by buying it back"
    assert by_symbol["BTCUSDT"].quantity == pytest.approx(0.5)
    assert by_symbol["ETHUSDT"].quantity == pytest.approx(3.0), "quantity is never signed"
    assert all(order.order_type == MARKET for order in exits)
    assert all(order.stop_price == 0.0 and order.limit_price == 0.0 for order in exits)
    assert all(order.may_be_sent for order in exits)
    assert all(order.destination == PAPER_BOOK for order in exits)
    assert CLOSE_POSITIONS in by_symbol["BTCUSDT"].reason


def test_the_same_instruction_does_not_place_a_second_exit_on_the_next_tick():
    """A part woken by every position message would otherwise place an exit per
    tick, and the book would be closed many times over."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())

    assert len(subject.exits_to_place()) == 1
    clock.advance(1.0)
    assert subject.exits_to_place() == ()
    assert subject.standing.waiting_for_a_fill == 1


def test_an_exit_that_never_filled_is_not_asked_for_a_second_time():
    """The whole position has been asked for. A second ask sells it twice.

    This test asserted the opposite until 2026-08-30, and the behaviour it
    asserted is what emptied the book: past `order_latency_maximum` the exit was
    repeated on the timer alone, with a fresh `client_order_id` every time and
    nothing withdrawing the previous ask. Measured on the live spine, that
    reached 411 repeats against two open positions, `paper-fill-simulator` held
    2,199 of them in flight, and they filled -- 13,020,303 units of TURBOUSDT
    sold against roughly 8,500,000 held, leaving a new short.

    A book that has not answered is a fault to report, not a reason to ask for
    more than exists: the position stands at the cap and says so.
    """
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    first = subject.exits_to_place()
    assert len(first) == 1
    assert first[0].quantity == 0.5

    clock.advance(6.0)
    assert subject.exits_to_place() == ()
    assert subject.standing.positions_at_the_cap == 1
    assert subject.standing.quantity_asked_beyond_the_position == 0.0


def test_a_partly_filled_exit_is_not_asked_for_again_because_the_rest_is_still_working():
    """A partial fill answers part of the ask. The rest of that ask is still live.

    0.5 was asked for. 0.2 came back, so 0.3 remains both held and outstanding,
    and the original ask already covers it. Asking again here would be asking for
    0.3 the book is already working on -- which is the 411-repeat failure in
    miniature.
    """
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    first = subject.exits_to_place()
    assert first[0].quantity == 0.5

    subject.observe_position(position("BTCUSDT", 0.3))
    clock.advance(6.0)
    assert subject.exits_to_place() == ()
    assert subject.standing.positions_at_the_cap == 1
    assert subject.standing.quantity_asked_beyond_the_position == pytest.approx(0.0)


def test_quantity_added_after_the_instruction_gets_its_own_exit():
    """The one legitimate second ask: there is more of the position than before.

    A position that grew was entered into again, which is new quantity nobody has
    asked to close. Only the difference is asked for -- never the whole position
    a second time.
    """
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    assert subject.exits_to_place()[0].quantity == pytest.approx(0.5)

    subject.observe_position(position("BTCUSDT", 0.8))
    clock.advance(6.0)
    again = subject.exits_to_place()
    assert len(again) == 1
    assert again[0].quantity == pytest.approx(0.3), "only the quantity that is new"
    assert subject.standing.exits_repeated == 1
    assert "again" in again[0].reason


def test_the_total_asked_for_never_exceeds_what_the_position_ever_held():
    """The invariant the whole cap exists for, said directly.

    Whatever the book does -- answer late, not answer, answer twice -- the sum of
    what this part asked to sell cannot exceed what it saw the position holding.
    """
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())

    asked = 0.0
    for _ in range(50):
        for order in subject.exits_to_place():
            asked += order.quantity
        clock.advance(6.0)

    assert asked <= 0.5
    assert subject.standing.quantity_asked_beyond_the_position == 0.0


def test_a_closed_position_is_not_closed_twice():
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    subject.exits_to_place()

    subject.observe_position(position("BTCUSDT", 0.0))
    clock.advance(60.0)
    assert subject.exits_to_place() == ()
    assert subject.standing.positions_closed_since_the_instruction == 1
    assert subject.standing.open_positions == 0


def test_an_instruction_that_is_not_about_the_book_closes_nothing():
    """stop-trading says the machine must go quiet; only close-positions says the
    book must go. Reading them as one liquidates a book because a human wanted
    the bot to stop thinking."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override(instruction="stop-trading"))

    assert subject.exits_to_place() == ()
    assert subject.standing.instructions_not_about_the_book == 1
    assert subject.standing.instructions_acted_on == 0


def test_an_override_that_is_no_longer_active_stops_the_flattening():
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    subject.exits_to_place()

    subject.observe_override(override(active=False))
    clock.advance(60.0)
    assert subject.exits_to_place() == ()
    assert not subject.is_flattening


def test_a_second_instruction_flattens_a_second_time():
    """What has been sent is remembered against the instruction that caused it.
    Remembering across instructions would refuse to close a book it had closed
    once before -- the same shape as the flap report that never expired."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override(override_id="first"))
    subject.exits_to_place()
    subject.observe_override(override(active=False, override_id="first"))
    subject.exits_to_place()

    subject.observe_override(override(override_id="second"))
    assert len(subject.exits_to_place()) == 1
    assert subject.standing.instructions_acted_on == 2


def test_where_an_exit_is_sent_is_never_guessed():
    """Paper and live are not interchangeable. With no money mode read, this
    places nothing and says why."""
    clock = Clock()
    subject = PositionFlattener(
        repeat_after_seconds=5.0, quantity_increment=0.001, now_ns=clock
    )
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())

    assert subject.exits_to_place() == ()
    assert subject.standing.refused_no_money_mode == 1

    subject.observe_money_mode("live")
    assert subject.exits_to_place()[0].destination == LIVE_VENUE


def test_an_exit_repeated_after_no_wait_at_all_is_refused_at_construction():
    with pytest.raises(ValueError):
        PositionFlattener(repeat_after_seconds=0.0, quantity_increment=0.001)


def test_an_override_with_no_id_of_its_own_is_still_one_instruction():
    """human-override-reader names an unnamed override `override-{sequence}` and
    the sequence increments on every read -- once a second. Keying the flattening
    on that id would read as a new instruction every tick and place a fresh exit
    for every open position every second."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))

    for sequence in range(1, 6):
        subject.observe_override(override(override_id=f"override-{sequence}"))
        placed = subject.exits_to_place()
        clock.advance(1.0)
        if sequence > 1:
            assert placed == (), "one instruction, one exit"

    assert subject.standing.exits_placed == 1
    assert subject.standing.instructions_acted_on == 1


def test_an_exit_the_book_will_not_take_shows_how_long_it_has_been_asked_for():
    """Measured 2026-08-27: STORJUSDT was refused 114 times for having no price,
    because the contract settled on the venue the day before and there was no
    market to close it into. The repeat counter climbing reads like progress; how
    long the oldest exit has been waiting is what says the book will not take it."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("STORJUSDT", 21810.2))
    subject.observe_override(override())
    subject.exits_to_place()

    for _ in range(20):
        clock.advance(6.0)
        subject.exits_to_place()

    assert subject.standing.longest_wait_seconds == pytest.approx(120.0)
    assert subject.standing.waiting_for_a_fill == 1

    subject.observe_position(position("STORJUSDT", 0.0))
    subject.exits_to_place()
    assert subject.standing.longest_wait_seconds == 0.0


def test_an_empty_door_ends_the_flattening():
    """human-override-reader publishes nothing once the file is deleted, so the
    part that holds the instruction has to be told the door is empty. Measured
    2026-08-27: without this the override was removed and the exits kept coming."""
    clock = Clock()
    subject = flattener(clock)
    subject.observe_position(position("BTCUSDT", 0.5))
    subject.observe_override(override())
    subject.exits_to_place()
    assert subject.is_flattening

    subject.observe_override(None)
    clock.advance(60.0)
    assert subject.exits_to_place() == ()
    assert not subject.is_flattening
    assert subject.standing.instructions_not_about_the_book == 0, (
        "an empty door is not an instruction about something else"
    )
    assert subject.standing.reads_with_no_override == 1
    assert subject.standing.overrides_read == 1, (
        "an empty door is not an override read: on a spine with no override "
        "written, overrides_read climbing once a second reports an instruction "
        "nobody gave -- measured 241 of them in the first four minutes"
    )
