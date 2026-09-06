"""The holder that stopped three bridges throwing away the feed's connect burst.

Measured cause, live spine 2026-09-06: `broker-market-data-bridge`,
`broker-candle-bridge` and `broker-order-book-bridge` had received 2,000 / 1,878
/ 1,999 updates and published none, with `instruments_resolved` at 2,000 in all
three -- every key was resolvable by the time anyone looked, and every update
that needed it had already been dropped.
"""

import pytest

from runtime.pending_instrument_updates import UpdatesAwaitingInstrumentListing


def _holder(held_instrument_limit=2000):
    return UpdatesAwaitingInstrumentListing(held_instrument_limit=held_instrument_limit)


def test_nothing_is_released_while_no_instrument_resolves():
    holder = _holder()
    holder.hold("NSE_FO|1001", "first")
    assert tuple(holder.release_resolvable(lambda key: False)) == ()
    assert holder.instruments_awaiting_listing == 1


def test_an_update_is_released_once_its_instrument_resolves():
    holder = _holder()
    holder.hold("NSE_FO|1001", "first")
    listed = {"NSE_FO|1001"}
    assert tuple(holder.release_resolvable(listed.__contains__)) == ("first",)
    # Released, so it leaves the holder rather than being republished forever.
    assert tuple(holder.release_resolvable(listed.__contains__)) == ()
    assert holder.instruments_awaiting_listing == 0


def test_only_the_newest_update_per_instrument_is_kept():
    """These are levels -- an LTP restates the last print, a depth update
    carries full depth, Upstox restates the forming bar. Releasing the ones a
    newer update replaced would put stale prices on the wire as if new."""
    holder = _holder()
    holder.hold("NSE_FO|1001", "stale")
    holder.hold("NSE_FO|1001", "current")
    assert tuple(holder.release_resolvable(lambda key: True)) == ("current",)
    assert holder.updates_replaced_while_held == 1


def test_one_instrument_that_never_resolves_does_not_block_the_ones_that_do():
    holder = _holder()
    holder.hold("NSE_FO|1001", "listed one")
    holder.hold("NSE_FO|9999", "never listed")
    released = tuple(holder.release_resolvable(lambda key: key == "NSE_FO|1001"))
    assert released == ("listed one",)
    assert holder.instruments_awaiting_listing == 1


def test_the_oldest_instrument_is_dropped_at_the_limit_and_counted():
    """A feed can name an instrument the master never listed. Without a bound
    those keys accumulate for the life of the process; with one, the loss is a
    number on the part's own standing rather than a silent leak."""
    holder = _holder(held_instrument_limit=2)
    holder.hold("a", 1)
    holder.hold("b", 2)
    holder.hold("c", 3)
    assert holder.instruments_awaiting_listing == 2
    assert holder.instruments_dropped_at_limit == 1
    assert tuple(holder.release_resolvable(lambda key: True)) == (2, 3)


def test_holding_an_instrument_again_makes_it_the_newest_against_the_limit():
    """A restated instrument is live, not stale -- evicting it before one that
    has not been heard from since would drop exactly the wrong key."""
    holder = _holder(held_instrument_limit=2)
    holder.hold("a", 1)
    holder.hold("b", 2)
    holder.hold("a", 3)
    holder.hold("c", 4)
    assert tuple(holder.release_resolvable(lambda key: True)) == (3, 4)


def test_standing_reports_what_it_is_holding_and_what_it_lost():
    holder = _holder(held_instrument_limit=1)
    holder.hold("a", 1)
    holder.hold("a", 2)
    holder.hold("b", 3)
    tuple(holder.release_resolvable(lambda key: key == "b"))
    assert holder.describe() == {
        "updates_awaiting_listing": 0.0,
        "updates_released_after_listing": 1.0,
        "updates_replaced_while_awaiting": 1.0,
        "instruments_dropped_at_hold_limit": 1.0,
    }


def test_a_holder_that_can_hold_nothing_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        UpdatesAwaitingInstrumentListing(held_instrument_limit=0)
