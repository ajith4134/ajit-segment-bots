"""A level is published as one message per item, not as the list.

Crash on the live spine, 2026-09-02: halt-enforcer read the restriction level
with `LatestValue`, whose `.value()` is the last *payload* -- one
InstrumentRestriction, not the tuple the producer passed to `publish`.
`observe_restrictions` iterated it and raised
`TypeError: 'InstrumentRestriction' object is not iterable`, and the part
crash-looped. Nothing in the unit tests could see it: they call
`observe_restrictions` directly with a tuple, which is exactly what the wire
does not deliver.

The second half matters more than the crash. `runtime/bus.py`'s `publish` is
`for item in items`, so an **empty** level publishes nothing at all -- "no
symbol is banned any more" cannot arrive as an empty list. The only way it
arrives is as a restatement that stops coming, which is why the reader carries
its own age bound.
"""

import datetime

from runtime.bus import Message
from runtime.input_assembly import LatestByKey
from runtime.market_conditions import InstrumentRestriction, RestrictionKind
from parts.risk_capital_allocation.halt_enforcer import HaltEnforcer

BAN_DAY = datetime.date(2026, 9, 2)
SECOND_NS = 1_000_000_000
NOW_NS = 1_756_800_000_000_000_000
ALLOWED_WHEN_CLEAR = 0.02
MAXIMUM_AGE_SECONDS = 180.0


def _message(symbol, published_at_ns):
    """What the bus really hands back. The real `Message` is used rather than a
    stand-in because the whole point of this file is the wire's shape: a fake
    that carried its time under some other name would age nothing and pass.
    """
    return Message(
        data_type="instrument-restriction",
        producer_part_id="instrument-restriction-state",
        sequence=0,
        published_at_ns=published_at_ns,
        payload=_restriction(symbol),
    )


def _restriction(symbol):
    return InstrumentRestriction(
        symbol=symbol, kinds=(RestrictionKind.FNO_BAN,), sources=("nse-fo-secban",),
        stated_for=BAN_DAY, observed_at_ns=NOW_NS,
    )


def _reader(messages):
    delivered = [tuple(messages)]

    def read():
        out, delivered[0] = delivered[0], ()
        return out

    return read


def _assembly(messages):
    return LatestByKey(
        read=_reader(messages),
        key_of=lambda restriction: restriction.symbol,
        maximum_age_seconds=MAXIMUM_AGE_SECONDS,
    )


def test_a_two_symbol_level_arrives_as_two_messages_and_both_halt():
    """The shape that crashed: the producer published a tuple of two, and the
    reader is handed them one at a time."""
    restrictions = _assembly([
        _message("LICHSGFIN", NOW_NS),
        _message("SAIL", NOW_NS),
    ])
    enforcer = HaltEnforcer(allowed_fraction_when_clear=ALLOWED_WHEN_CLEAR)
    enforcer.observe_restrictions(restrictions.values(now_ns=NOW_NS))
    limit = enforcer.read_limit()
    assert set(limit.symbols) == {"LICHSGFIN", "SAIL"}
    assert limit.fraction_of_allotment == 0.0


def test_a_symbol_that_stops_being_restated_expires_and_stops_halting():
    """The only way an un-ban can arrive. An empty level publishes no messages
    at all, so a reader with no age bound would halt that name until the
    process restarted."""
    restrictions = _assembly([_message("LICHSGFIN", NOW_NS)])
    enforcer = HaltEnforcer(allowed_fraction_when_clear=ALLOWED_WHEN_CLEAR)
    enforcer.observe_restrictions(restrictions.values(now_ns=NOW_NS))
    assert enforcer.read_limit().fraction_of_allotment == 0.0

    later_ns = NOW_NS + int((MAXIMUM_AGE_SECONDS + 1) * SECOND_NS)
    enforcer.observe_restrictions(restrictions.values(now_ns=later_ns))
    assert enforcer.read_limit().fraction_of_allotment == ALLOWED_WHEN_CLEAR


def test_a_symbol_still_being_restated_keeps_halting():
    restrictions = _assembly([_message("LICHSGFIN", NOW_NS)])
    enforcer = HaltEnforcer(allowed_fraction_when_clear=ALLOWED_WHEN_CLEAR)
    still_inside_ns = NOW_NS + int((MAXIMUM_AGE_SECONDS - 1) * SECOND_NS)
    enforcer.observe_restrictions(restrictions.values(now_ns=still_inside_ns))
    assert enforcer.read_limit().fraction_of_allotment == 0.0


def test_one_symbol_expiring_leaves_the_other_halted():
    """Per-key ageing, not one clock for the whole level: two names banned on
    different days must not lift together."""
    fresh_ns = NOW_NS + int((MAXIMUM_AGE_SECONDS - 1) * SECOND_NS)
    restrictions = _assembly([
        _message("LICHSGFIN", NOW_NS),
        _message("SAIL", fresh_ns),
    ])
    enforcer = HaltEnforcer(allowed_fraction_when_clear=ALLOWED_WHEN_CLEAR)
    at_ns = NOW_NS + int((MAXIMUM_AGE_SECONDS + 1) * SECOND_NS)
    enforcer.observe_restrictions(restrictions.values(now_ns=at_ns))
    assert enforcer.read_limit().symbols == ("SAIL",)
