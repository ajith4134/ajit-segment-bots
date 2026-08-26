"""A level that stopped arriving must not read as a level that is still true.

The failure this file exists to prevent happened on the live run of 2026-08-23:
`position-sizer` priced an ENAUSDT order at 0.17019, which was the real market at
09:29:08 -- fifty-six minutes earlier. Nothing was broken in the arithmetic. The
sizer read its entry price out of a `LatestByKey`, that symbol's messages stopped
arriving under input loss, and the shape went on returning the last one it saw
with no way for any reader to tell how old it was.

`LatestValue` has carried `observed_at_ns` since it was written. The per-key shape
lost exactly the field that makes staleness visible, and 81 parts read through it.
"""

from __future__ import annotations

import pytest

from runtime.bus import Message
from runtime.input_assembly import (
    Batch,
    LatestByKey,
    LatestStatementBySource,
    LatestValue,
)

ONE_SECOND_NS = 1_000_000_000


class Price:
    """The shape of the levels the sizer actually reads, reduced to what is keyed."""

    def __init__(self, symbol: str, price: float) -> None:
        self.symbol = symbol
        self.price = price


def _message(payload: object, at_ns: int, sequence: int = 0) -> Message:
    return Message(
        data_type="consolidated-price",
        producer_part_id="cross-venue-price-consolidator",
        sequence=sequence,
        published_at_ns=at_ns,
        payload=payload,
    )


def _delivering(*batches: tuple[Message, ...]):
    """A reader that hands over one batch per call, then nothing -- input loss."""
    remaining = list(batches)

    def read() -> tuple[Message, ...]:
        return remaining.pop(0) if remaining else ()

    return read


def test_a_key_carries_when_it_was_observed():
    """The fact the sizer needed and could not get."""
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering((_message(Price("ENAUSDT", 0.17019), at),)),
        key_of=lambda price: price.symbol,
    )
    levels.mapping()

    assert levels.observed_at_ns("ENAUSDT") == at


def test_an_unseen_key_has_no_observation_time():
    """Never seen and stale are different facts, and neither is a price."""
    levels = LatestByKey(read=_delivering(()), key_of=lambda price: price.symbol)
    levels.mapping()

    assert levels.observed_at_ns("ENAUSDT") is None
    assert levels.age_seconds("ENAUSDT", now_ns=ONE_SECOND_NS) is None


def test_a_keys_age_grows_while_nothing_arrives_for_it():
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering((_message(Price("ENAUSDT", 0.17019), at),)),
        key_of=lambda price: price.symbol,
    )
    levels.mapping()

    assert levels.age_seconds("ENAUSDT", now_ns=at) == pytest.approx(0.0)
    assert levels.age_seconds("ENAUSDT", now_ns=at + 3_360 * ONE_SECOND_NS) == pytest.approx(3360.0)


def test_a_stale_key_is_absent_rather_than_old():
    """The whole point: a bound turns an old level into no level.

    `position-sizer` already refuses to size when its entry price is None. Making
    a stale key absent is therefore what makes that existing refusal fire, rather
    than a new branch every one of 81 readers would have to grow for itself.
    """
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering((_message(Price("ENAUSDT", 0.17019), at),), ()),
        key_of=lambda price: price.symbol,
        maximum_age_seconds=60.0,
    )
    assert levels.mapping(now_ns=at)["ENAUSDT"].price == pytest.approx(0.17019)

    fresh = levels.mapping(now_ns=at + 30 * ONE_SECOND_NS)
    assert "ENAUSDT" in fresh

    stale = levels.mapping(now_ns=at + 3_360 * ONE_SECOND_NS)
    assert "ENAUSDT" not in stale
    assert levels.values(now_ns=at + 3_360 * ONE_SECOND_NS) == ()


def test_a_stale_key_returns_when_it_arrives_again():
    """Staleness is not eviction. The symbol went quiet; it did not cease to exist."""
    at = 1_000 * ONE_SECOND_NS
    later = at + 3_360 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering(
            (_message(Price("ENAUSDT", 0.17019), at),),
            (),
            (_message(Price("ENAUSDT", 0.2044), later, sequence=1),),
        ),
        key_of=lambda price: price.symbol,
        maximum_age_seconds=60.0,
    )
    levels.mapping(now_ns=at)
    assert "ENAUSDT" not in levels.mapping(now_ns=later)

    back = levels.mapping(now_ns=later)
    assert back["ENAUSDT"].price == pytest.approx(0.2044)


def test_staleness_is_counted_so_it_can_be_seen():
    """A refusal nobody can count is indistinguishable from an input that never came."""
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering(
            (
                _message(Price("ENAUSDT", 0.17019), at),
                _message(Price("BTCUSDT", 64_000.0), at, sequence=1),
            ),
            (),
        ),
        key_of=lambda price: price.symbol,
        maximum_age_seconds=60.0,
    )
    levels.mapping(now_ns=at)
    levels.mapping(now_ns=at + 3_360 * ONE_SECOND_NS)

    assert levels.stale_keys == 2
    assert levels.fresh_keys == 0
    assert levels.keys_seen == 2


def test_without_a_bound_nothing_expires():
    """Levels that are true until they change -- the machine still has its cores.

    The bound is opt-in per assembly because what counts as old belongs to the data
    type, not to the shape: a hardware fact does not go stale in a minute and a
    price does.
    """
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering((_message(Price("ENAUSDT", 0.17019), at),), ()),
        key_of=lambda price: price.symbol,
    )
    levels.mapping(now_ns=at)

    assert "ENAUSDT" in levels.mapping(now_ns=at + 3_360 * ONE_SECOND_NS)
    assert levels.stale_keys == 0


def test_a_bound_that_is_not_a_positive_number_is_refused():
    """RL-061: the bound is a named setting, and a nonsense one is a refusal here
    rather than an expiry that silently never fires."""
    with pytest.raises(ValueError):
        LatestByKey(read=_delivering(()), key_of=lambda p: p.symbol, maximum_age_seconds=0.0)
    with pytest.raises(ValueError):
        LatestByKey(read=_delivering(()), key_of=lambda p: p.symbol, maximum_age_seconds=-1.0)


def test_forget_still_drops_a_key_and_its_observation_time():
    at = 1_000 * ONE_SECOND_NS
    levels = LatestByKey(
        read=_delivering((_message(Price("ENAUSDT", 0.17019), at),)),
        key_of=lambda price: price.symbol,
    )
    levels.mapping()
    levels.forget("ENAUSDT")

    assert levels.keys_seen == 0
    assert levels.observed_at_ns("ENAUSDT") is None


def test_latest_value_still_reports_when_it_was_observed():
    """The sibling shape that already had this, kept honest by the same file."""
    at = 1_000 * ONE_SECOND_NS
    level = LatestValue(read=_delivering((_message(Price("ENAUSDT", 0.17019), at),)))

    assert level.value() is not None
    assert level.observed_at_ns == at
    assert level.has_been_seen


def test_a_batch_is_emptied_by_reading_it():
    """Unchanged by this work, asserted because nothing else asserts it."""
    at = 1_000 * ONE_SECOND_NS
    batch = Batch(read=_delivering((_message(Price("ENAUSDT", 0.17019), at),), ()))

    assert len(batch.payloads()) == 1
    assert batch.payloads() == ()
    assert batch.messages_seen == 1


# ---- LatestStatementBySource -------------------------------------------------
#
# The second half of the same lesson, from 2026-08-26. A source that speaks in
# sets has no key that works: keyed by the source, the last entry of a statement
# erases the rest; keyed by source and entry, an entry the source stops making is
# never replaced by anything and binds forever. Both were live defects in
# `position-sizer`'s hold on risk limits on the same day, and the second one
# refused all 838 intents of a run.


class Limit:
    """A statement's entry, reduced to what identifies it."""

    def __init__(self, source: str, scope: str, fraction: float, decided_at_ns: int) -> None:
        self.source = source
        self.scope = scope
        self.fraction = fraction
        self.decided_at_ns = decided_at_ns


def _statements(read, maximum_age_seconds: float | None = None):
    return LatestStatementBySource(
        read=read,
        source_of=lambda entry: entry.source,
        stamp_of=lambda entry: entry.decided_at_ns,
        entry_of=lambda entry: entry.scope,
        maximum_age_seconds=maximum_age_seconds,
    )


def test_a_later_statement_replaces_the_whole_previous_one():
    at = 1_000 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (
                _message(Limit("halt-enforcer", "BTCUSDT", 0.0, at), at),
                _message(Limit("halt-enforcer", "ETHUSDT", 0.0, at), at, sequence=1),
            ),
            (_message(Limit("halt-enforcer", "", 0.02, at + ONE_SECOND_NS), at + ONE_SECOND_NS),),
        )
    )
    held.mapping()
    every = held.mapping()

    assert [entry.fraction for entry in every.values()] == [0.02]
    assert held.statements_replaced == 1


def test_the_same_statement_arriving_in_pieces_is_held_whole():
    """The bus sends one datagram per item, so a set published at once need not arrive at once."""
    at = 1_000 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (_message(Limit("event-risk-limiter", "", 0.5, at), at),),
            (_message(Limit("event-risk-limiter", "BTCUSDT", 0.1, at), at, sequence=1),),
        )
    )
    held.mapping()
    every = held.mapping()

    assert sorted(entry.fraction for entry in every.values()) == [0.1, 0.5]
    assert held.statements_replaced == 0


def test_an_entry_of_a_superseded_statement_is_dropped():
    at = 1_000 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (_message(Limit("halt-enforcer", "", 0.02, at + ONE_SECOND_NS), at + ONE_SECOND_NS),),
            (_message(Limit("halt-enforcer", "BTCUSDT", 0.0, at), at),),
        )
    )
    held.mapping()
    every = held.mapping()

    assert [entry.fraction for entry in every.values()] == [0.02]
    assert held.arrivals_already_superseded == 1
    assert held.messages_seen == 2


def test_a_source_that_goes_quiet_is_absent_whole():
    """Half a statement is not a statement, so a stale source leaves entirely."""
    at = 1_000 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (
                _message(Limit("halt-enforcer", "BTCUSDT", 0.0, at), at),
                _message(Limit("halt-enforcer", "ETHUSDT", 0.0, at), at, sequence=1),
            ),
        ),
        maximum_age_seconds=30.0,
    )
    assert len(held.mapping(now_ns=at)) == 2

    every = held.mapping(now_ns=at + 31 * ONE_SECOND_NS)
    assert every == {}
    assert held.stale_sources == 1
    assert held.fresh_sources == 0
    assert held.sources_seen == 1, "the source went quiet, it did not cease to exist"


def test_a_quiet_source_comes_back_when_it_speaks_again():
    at = 1_000 * ONE_SECOND_NS
    later = at + 40 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (_message(Limit("halt-enforcer", "BTCUSDT", 0.0, at), at),),
            (_message(Limit("halt-enforcer", "", 0.02, later), later),),
        ),
        maximum_age_seconds=30.0,
    )
    held.mapping(now_ns=at)
    every = held.mapping(now_ns=later)

    assert [entry.fraction for entry in every.values()] == [0.02]


def test_one_sources_statement_does_not_touch_anothers():
    at = 1_000 * ONE_SECOND_NS
    held = _statements(
        _delivering(
            (
                _message(Limit("halt-enforcer", "", 0.05, at), at),
                _message(Limit("exposure-limiter", "", 0.01, at), at, sequence=1),
            ),
            (_message(Limit("halt-enforcer", "", 1.0, at + ONE_SECOND_NS), at + ONE_SECOND_NS),),
        )
    )
    held.mapping()
    every = held.mapping()

    assert sorted(entry.fraction for entry in every.values()) == [0.01, 1.0]
    assert held.statement_of("exposure-limiter")[0].fraction == 0.01


def test_forget_drops_a_source_whole():
    at = 1_000 * ONE_SECOND_NS
    held = _statements(_delivering((_message(Limit("halt-enforcer", "", 0.0, at), at),)))
    held.mapping()

    held.forget("halt-enforcer")
    assert held.mapping() == {}
    assert held.sources_seen == 0
    assert held.observed_at_ns("halt-enforcer") is None


def test_a_statement_bound_that_is_not_a_positive_number_is_refused():
    for refused in (0.0, -1.0):
        with pytest.raises(ValueError):
            _statements(_delivering(), maximum_age_seconds=refused)
