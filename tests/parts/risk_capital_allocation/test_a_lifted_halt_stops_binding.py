"""A limiter's next word must replace its last one, whatever scope it carries.

The defect this file exists to prevent, measured on the live spine at 11:47 on
2026-08-26:

    trading-halt-decider   443 halts raised, 443 released, is_halted 0
    halt-enforcer          114,840 limits issued, 18,743 of them zero,
                           18,741 of those scoped to symbols
    position-sizer         838 intents, sized 0, refused_no_risk_allowed 14,
                           intents_that_stood_aside 824
    paper-fill-simulator   14 order-requests received, orders_seen 0, filled 0
    stop-order-manager     0 of 20 open positions had a stop resting

Every halt had been released and nothing was halting, yet no order could be
sized. `halt-enforcer` zeroes risk scoped to the symbols an anomaly was seen on
and publishes its all-clear with no scope at all. The sizer keyed its held limits
on `(limiter, symbols)`, so the all-clear landed on a *different key* and the
scoped zero was never replaced -- and `LatestByKey` holds a key's last value
forever unless bounded, so it never expired either.

With 2,899 anomalies of the kind "one venue moved and the others did not" across
100 venue-symbols, essentially every symbol had been scoped-to-zero once and was
permanently untradeable. Nothing reported a fault: each of those zeros was a
limit halt-enforcer was entitled to issue. Only its immortality was wrong.

The scope itself is not the bug and has not been removed -- `applies_to` still
decides per symbol, which is what stops a two-symbol halt zeroing the other
ninety-eight. What changed is that one limiter now has one current word.
"""

from __future__ import annotations

from runtime.input_assembly import LatestByKey
from runtime.bus import Message
from runtime.risk_types import RiskLimit

HALT_ENFORCER = "halt-enforcer"
EXPOSURE_LIMITER = "exposure-limiter"


def limit(limiter: str, fraction: float, symbols=(), at_ns: int = 1) -> RiskLimit:
    return RiskLimit(
        limiter=limiter,
        fraction_of_allotment=fraction,
        reason="under test",
        is_binding=fraction <= 0.0,
        decided_at_ns=at_ns,
        symbols=symbols,
    )


def _delivering(*limits):
    """A reader handing over one batch per call, the way the bus does."""
    batches = [
        tuple(
            Message(
                data_type="risk-limit",
                producer_part_id=held.limiter,
                sequence=index,
                published_at_ns=held.decided_at_ns,
                payload=held,
            )
            for index, held in enumerate(batch)
        )
        for batch in limits
    ]

    def read():
        return batches.pop(0) if batches else ()

    return read


def binding_limit_for(every_limit, symbol):
    """The sizer's own rule: the smallest fraction any limit binding this symbol allows."""
    applying = [
        held.fraction_of_allotment
        for held in every_limit.values()
        if held.applies_to(symbol)
    ]
    return min(applying) if applying else None


def test_a_scoped_zero_is_replaced_by_the_all_clear():
    """The whole defect, in three ticks."""
    limits = LatestByKey(
        read=_delivering(
            (limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),),
            (limit(HALT_ENFORCER, 0.02, symbols=(), at_ns=2),),
        ),
        key_of=lambda held: held.limiter,
    )

    # Each mapping() drains one batch, exactly as one tick of the sizer does.
    halted = limits.mapping()
    assert binding_limit_for(halted, "BTCUSDT") == 0.0, "the halt did not bind"

    # The halt lifts. Its all-clear carries no scope -- a different symbol tuple.
    every = limits.mapping()
    assert binding_limit_for(every, "BTCUSDT") == 0.02, (
        "a released halt still forbids risk on the symbol it was raised over; "
        "keying on (limiter, symbols) is what made the zero immortal"
    )


def test_a_scoped_halt_still_leaves_other_symbols_alone():
    """The 2026-08-25 fix must survive the 2026-08-26 one."""
    limits = LatestByKey(
        read=_delivering((limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),)),
        key_of=lambda held: held.limiter,
    )
    every = limits.mapping()

    assert binding_limit_for(every, "BTCUSDT") == 0.0
    assert binding_limit_for(every, "ETHUSDT") is None, (
        "a halt over one symbol bound another"
    )


def test_two_limiters_still_both_bind():
    """Keying by limiter must not let one limiter overwrite another."""
    limits = LatestByKey(
        read=_delivering(
            (
                limit(HALT_ENFORCER, 0.05, symbols=(), at_ns=1),
                limit(EXPOSURE_LIMITER, 0.01, symbols=(), at_ns=1),
            ),
        ),
        key_of=lambda held: held.limiter,
    )
    every = limits.mapping()

    assert len(every) == 2
    assert binding_limit_for(every, "BTCUSDT") == 0.01, (
        "the tighter of two limiters did not bind"
    )


def test_a_limiter_that_says_nothing_cannot_raise_another_limiters_floor():
    """The property the min() is there for, unchanged by the rekey."""
    limits = LatestByKey(
        read=_delivering(
            (limit(EXPOSURE_LIMITER, 0.0, symbols=(), at_ns=1),),
            (limit(HALT_ENFORCER, 0.05, symbols=(), at_ns=2),),
        ),
        key_of=lambda held: held.limiter,
    )
    limits.mapping()
    every = limits.mapping()

    assert binding_limit_for(every, "BTCUSDT") == 0.0, (
        "a second limiter speaking raised a floor the first one had lowered"
    )


def test_a_halt_scoped_to_several_symbols_is_replaced_whole():
    """A scope narrowing must not leave the wider scope's zero behind."""
    limits = LatestByKey(
        read=_delivering(
            (limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT", "ETHUSDT"), at_ns=1),),
            (limit(HALT_ENFORCER, 0.0, symbols=("ETHUSDT",), at_ns=2),),
        ),
        key_of=lambda held: held.limiter,
    )
    limits.mapping()
    every = limits.mapping()

    assert binding_limit_for(every, "ETHUSDT") == 0.0
    assert binding_limit_for(every, "BTCUSDT") is None, (
        "a symbol dropped out of the halt's scope is still bound by the old scope"
    )
