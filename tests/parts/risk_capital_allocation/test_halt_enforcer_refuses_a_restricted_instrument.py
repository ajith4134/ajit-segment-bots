"""An instrument NSE has restricted must not take a new position.

The restriction shapes are exactly what instrument-restriction-state publishes
from the live NSE responses captured 2026-09-02 (LICHSGFIN and SAIL were the
two names in ban that day).
"""

import datetime

from runtime.market_conditions import InstrumentRestriction, RestrictionKind
from runtime.risk_types import EVERY_SYMBOL
from parts.risk_capital_allocation.halt_enforcer import (
    HUMAN_OVERRIDE,
    INSTRUMENT_RESTRICTION,
    HaltEnforcer,
)

BAN_DAY = datetime.date(2026, 9, 2)
OBSERVED_AT_NS = 1_756_800_000_000_000_000
ALLOWED_WHEN_CLEAR = 0.02


def _banned(symbol="LICHSGFIN", kind=RestrictionKind.FNO_BAN, source="nse-fo-secban"):
    return InstrumentRestriction(
        symbol=symbol, kinds=(kind,), sources=(source,),
        stated_for=BAN_DAY, observed_at_ns=OBSERVED_AT_NS,
    )


def _enforcer():
    return HaltEnforcer(allowed_fraction_when_clear=ALLOWED_WHEN_CLEAR)


def test_a_banned_instrument_zeroes_risk_for_that_symbol_only():
    """A ban on two names is not a reason to stop trading everything else --
    the 2026-08-25 defect this part already carries a docstring about, where
    two anomalous symbols out of a hundred stopped every trade in the system."""
    enforcer = _enforcer()
    enforcer.observe_restrictions((_banned(), _banned("SAIL")))
    limit = enforcer.read_limit()
    assert limit.fraction_of_allotment == 0.0
    assert set(limit.symbols) == {"LICHSGFIN", "SAIL"}
    assert limit.symbols != EVERY_SYMBOL


def test_a_ban_that_stops_being_reported_releases_the_halt():
    """NSE publishes no un-ban: the level simply stops carrying the symbol.
    A halt released only explicitly would never lift."""
    enforcer = _enforcer()
    enforcer.observe_restrictions((_banned(),))
    enforcer.observe_restrictions(())
    assert enforcer.read_limit().fraction_of_allotment == ALLOWED_WHEN_CLEAR


def test_the_restriction_halt_names_the_exchange_that_claimed_it():
    enforcer = _enforcer()
    enforcer.observe_restrictions((_banned(),))
    assert "nse-fo-secban" in enforcer.read_limit().reason


def test_an_asm_restriction_halts_the_same_way_a_ban_does():
    enforcer = _enforcer()
    enforcer.observe_restrictions((
        _banned("A2ZINFRA", RestrictionKind.ASM_LONG_TERM, "nse-asm-longterm"),
    ))
    limit = enforcer.read_limit()
    assert limit.fraction_of_allotment == 0.0
    assert set(limit.symbols) == {"A2ZINFRA"}


def test_a_restriction_never_outranks_a_broader_halt():
    """Precedence order matters: a human override scoped to everything must not
    be narrowed to the banned symbols."""
    enforcer = _enforcer()
    enforcer.observe_restrictions((_banned(),))
    enforcer.raise_halt(HUMAN_OVERRIDE, "everything", "operator said stop")
    assert enforcer.read_limit().symbols == EVERY_SYMBOL


def test_an_unrestricted_book_raises_no_restriction_halt_at_all():
    enforcer = _enforcer()
    enforcer.observe_restrictions(())
    assert INSTRUMENT_RESTRICTION not in enforcer.standing.active


def test_a_restriction_that_still_permits_opening_is_not_halted():
    """InstrumentRestriction with no kinds means nothing is standing on that
    symbol. Halting it would stop trading a name the exchange never stopped."""
    enforcer = _enforcer()
    clear = InstrumentRestriction(
        symbol="RELIANCE", kinds=(), sources=(), stated_for=BAN_DAY,
        observed_at_ns=OBSERVED_AT_NS,
    )
    enforcer.observe_restrictions((clear,))
    assert enforcer.read_limit().fraction_of_allotment == ALLOWED_WHEN_CLEAR


def test_the_halted_symbols_are_stated_in_a_stable_order():
    """The limit's reason and scope ride on every published risk-limit. An
    order that wandered between ticks would read as a changing halt."""
    enforcer = _enforcer()
    enforcer.observe_restrictions((_banned("SAIL"), _banned("LICHSGFIN")))
    assert enforcer.read_limit().symbols == ("LICHSGFIN", "SAIL")
