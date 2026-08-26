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
ninety-eight.

**A limiter's tick is one statement, and its next statement replaces it.**
Neither of the two obvious keyings can hold that:

    keyed by the limiter        the last limit of a statement erases the rest, so
                                event-risk-limiter -- one limit for the whole book
                                plus one per symbol with an event -- collapses to
                                whichever arrived last
    keyed by limiter and scope  a scope the limiter stops issuing is never
                                replaced by anything, which is the defect above

So the reader tells one statement from the next by the limiter's own
`decided_at_ns`, which every limit decided in one tick carries identically:
`LatestStatementBySource`. A later stamp replaces the whole set, the same stamp
joins it, an earlier one is dropped. A limit the limiter stops issuing is gone
the moment it issues its next word, rather than waiting to age out.

The age bound stays, for the other silence: a limiter that stops speaking
altogether. It fails in the safe direction -- with no limit applying to a symbol
the sizer refuses to size, so expiry withdraws permission rather than granting it.
"""

from __future__ import annotations

from runtime.input_assembly import LatestStatementBySource
from runtime.bus import Message
from runtime.risk_types import RiskLimit

HALT_ENFORCER = "halt-enforcer"
EXPOSURE_LIMITER = "exposure-limiter"
EVENT_RISK_LIMITER = "event-risk-limiter"

MAXIMUM_AGE_SECONDS = 30.0
ONE_SECOND_NS = 1_000_000_000
# A stated clock, never the real one. The bound is measured against the time the
# bus stamps a message with, so an assembly built at import time and read minutes
# later has expired everything -- which is what a `time.time_ns()` here did: these
# eight tests passed alone and failed inside the full suite, twenty-four minutes
# after the module was imported. Every read below names the moment it reads at.
NOW_NS = 1_000 * ONE_SECOND_NS
READ_AT_NS = NOW_NS + ONE_SECOND_NS


def limit(limiter: str, fraction: float, symbols=(), at_ns: int = 1) -> RiskLimit:
    """One limit of a statement. `at_ns` identifies the statement it belongs to."""
    return RiskLimit(
        limiter=limiter,
        fraction_of_allotment=fraction,
        reason="under test",
        is_binding=fraction <= 0.0,
        decided_at_ns=NOW_NS + at_ns,
        symbols=symbols,
    )


def _delivering(*batches_of_limits):
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
        for batch in batches_of_limits
    ]

    def read():
        return batches.pop(0) if batches else ()

    return read


def _held(read, maximum_age_seconds: float | None = MAXIMUM_AGE_SECONDS):
    """The sizer's own assembly of risk limits."""
    return LatestStatementBySource(
        read=read,
        source_of=lambda held: held.limiter,
        stamp_of=lambda held: held.decided_at_ns,
        entry_of=lambda held: held.symbols,
        maximum_age_seconds=maximum_age_seconds,
    )


def binding_limit_for(every_limit, symbol):
    """The sizer's own rule: the smallest fraction any limit binding this symbol allows."""
    applying = [
        held.fraction_of_allotment
        for held in every_limit.values()
        if held.applies_to(symbol)
    ]
    return min(applying) if applying else None


def test_a_scoped_zero_is_replaced_by_the_all_clear():
    """The whole defect, in two ticks."""
    limits = _held(
        _delivering(
            (limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),),
            (limit(HALT_ENFORCER, 0.02, symbols=(), at_ns=2),),
        )
    )

    # Each mapping() drains one batch, exactly as one tick of the sizer does.
    halted = limits.mapping(now_ns=READ_AT_NS)
    assert binding_limit_for(halted, "BTCUSDT") == 0.0, "the halt did not bind"

    # The halt lifts. Its all-clear carries no scope -- a different symbol tuple,
    # and under the old keying a different key, so the zero survived it.
    every = limits.mapping(now_ns=READ_AT_NS)
    assert binding_limit_for(every, "BTCUSDT") == 0.02, (
        "a released halt still forbids risk on the symbol it was raised over"
    )


def test_a_scoped_halt_still_leaves_other_symbols_alone():
    """The 2026-08-25 fix must survive the 2026-08-26 one."""
    limits = _held(
        _delivering((limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),))
    )
    every = limits.mapping(now_ns=READ_AT_NS)

    assert binding_limit_for(every, "BTCUSDT") == 0.0
    assert binding_limit_for(every, "ETHUSDT") is None, (
        "a halt over one symbol bound another"
    )


def test_two_limiters_still_both_bind():
    """One limiter's statement must not replace another limiter's."""
    limits = _held(
        _delivering(
            (
                limit(HALT_ENFORCER, 0.05, symbols=(), at_ns=1),
                limit(EXPOSURE_LIMITER, 0.01, symbols=(), at_ns=1),
            ),
        )
    )
    every = limits.mapping(now_ns=READ_AT_NS)

    assert len(every) == 2
    assert binding_limit_for(every, "BTCUSDT") == 0.01, (
        "the tighter of two limiters did not bind"
    )


def test_a_limiter_that_says_nothing_cannot_raise_another_limiters_floor():
    """The property the min() is there for."""
    limits = _held(
        _delivering(
            (limit(EXPOSURE_LIMITER, 0.0, symbols=(), at_ns=1),),
            (limit(HALT_ENFORCER, 0.05, symbols=(), at_ns=2),),
        )
    )
    limits.mapping(now_ns=READ_AT_NS)
    every = limits.mapping(now_ns=READ_AT_NS)

    assert binding_limit_for(every, "BTCUSDT") == 0.0, (
        "a second limiter speaking raised a floor the first one had lowered"
    )


def test_a_halt_scoped_to_several_symbols_is_replaced_whole():
    """A scope narrowing must not leave the wider scope's zero behind."""
    limits = _held(
        _delivering(
            (limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT", "ETHUSDT"), at_ns=1),),
            (limit(HALT_ENFORCER, 0.0, symbols=("ETHUSDT",), at_ns=2),),
        )
    )
    limits.mapping(now_ns=READ_AT_NS)
    every = limits.mapping(now_ns=READ_AT_NS)

    assert binding_limit_for(every, "ETHUSDT") == 0.0
    assert binding_limit_for(every, "BTCUSDT") is None, (
        "a symbol dropped out of the halt's scope is still bound by the old scope"
    )


def test_every_limit_of_one_statement_is_held():
    """What keying by the limiter alone would have broken.

    event-risk-limiter states one limit for whatever affects the whole book and
    one per symbol with an event in force. All of them are its current word.
    """
    limits = _held(
        _delivering(
            (
                limit(EVENT_RISK_LIMITER, 0.5, symbols=(), at_ns=1),
                limit(EVENT_RISK_LIMITER, 0.1, symbols=("BTCUSDT",), at_ns=1),
                limit(EVENT_RISK_LIMITER, 0.2, symbols=("ETHUSDT",), at_ns=1),
            ),
        )
    )
    every = limits.mapping(now_ns=READ_AT_NS)

    assert len(every) == 3, "a statement's own limits erased each other"
    assert binding_limit_for(every, "BTCUSDT") == 0.1
    assert binding_limit_for(every, "ETHUSDT") == 0.2
    assert binding_limit_for(every, "SOLUSDT") == 0.5, (
        "a symbol with no event of its own is bound by what affects every symbol"
    )


def test_a_statement_split_across_two_reads_is_held_whole():
    """The bus sends one datagram per limit, so a statement need not arrive at once."""
    limits = _held(
        _delivering(
            (limit(EVENT_RISK_LIMITER, 0.5, symbols=(), at_ns=1),),
            (limit(EVENT_RISK_LIMITER, 0.1, symbols=("BTCUSDT",), at_ns=1),),
        )
    )
    limits.mapping(now_ns=READ_AT_NS)
    every = limits.mapping(now_ns=READ_AT_NS)

    assert len(every) == 2, "the second half of a statement replaced the first"
    assert binding_limit_for(every, "BTCUSDT") == 0.1
    assert binding_limit_for(every, "SOLUSDT") == 0.5


def test_a_limit_from_a_superseded_statement_is_dropped():
    """A datagram that arrives late must not resurrect a withdrawn limit."""
    limits = _held(
        _delivering(
            (limit(HALT_ENFORCER, 0.02, symbols=(), at_ns=2),),
            (limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),),
        )
    )
    limits.mapping(now_ns=READ_AT_NS)
    every = limits.mapping(now_ns=READ_AT_NS)

    assert binding_limit_for(every, "BTCUSDT") == 0.02, (
        "a limit from a statement the limiter has already replaced bound a symbol"
    )
    assert limits.arrivals_already_superseded == 1


def test_a_limiter_that_stops_speaking_stops_binding():
    """The other silence: not a scope withdrawn, a limiter gone.

    The sizer refuses to size a symbol no limit applies to, so this expiry
    withdraws permission rather than granting it -- the refusal is the safe end.
    """
    limits = _held(
        _delivering((limit(HALT_ENFORCER, 0.0, symbols=("BTCUSDT",), at_ns=1),))
    )
    limits.mapping(now_ns=READ_AT_NS)

    long_after_ns = NOW_NS + int((MAXIMUM_AGE_SECONDS + 1) * ONE_SECOND_NS)
    every = limits.mapping(now_ns=long_after_ns)

    assert every == {}, "a limiter that has gone silent still bound a symbol"
    assert limits.stale_sources == 1
    assert binding_limit_for(every, "BTCUSDT") is None
