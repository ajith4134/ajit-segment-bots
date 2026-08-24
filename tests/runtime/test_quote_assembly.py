"""Assembling whole quotes out of a venue that amends, on a real amending stream.

Every change fed to the assembler here came off Bybit's own `tickers` stream,
captured 2026-08-24 (RL-063). The properties under test are the two that decide
whether a merged quote can be trusted:

* a side the venue did not mention is carried forward, never invented;
* the merged quote is as old as its **stalest** side, so an active bid cannot
  make a forgotten ask look fresh.

The second is the one that matters. Taking the newer of the two stamps would make
every merged quote look as current as its busiest half -- the same shape as the
failure of 2026-08-23, where a re-delivered price was treated as a new one.
"""

import pytest

from runtime.quote_assembly import QuoteAssembler
from runtime.venues.bybit_linear import build_venue_adapter as build_bybit_adapter
from runtime.venues.venue_adapter import QuoteChange

QUOTE_FIXTURE = "2026-08-24-public-linear-tickers-through-one-sided-delta.jsonl"


@pytest.fixture
def captured_changes(read_captured_payloads):
    """Every quote change Bybit sent in the captured window, in the order it sent them."""
    adapter = build_bybit_adapter()
    return [
        change
        for _, payload in read_captured_payloads("bybit-linear", QUOTE_FIXTURE)
        for change in adapter.read_quote_changes(payload)
    ]


@pytest.fixture
def assembler():
    return QuoteAssembler("bybit-linear", amends_rather_than_restates=True)


def test_the_opening_snapshot_completes_a_quote_on_its_own(assembler, captured_changes):
    quote = assembler.apply_change(captured_changes[0])
    assert quote is not None
    assert quote.bid_price < quote.ask_price


def test_a_one_sided_delta_keeps_the_side_the_venue_did_not_mention(
    assembler, captured_changes
):
    """The carried side is the last one the venue actually said, not a guess."""
    snapshot = captured_changes[0]
    assembler.apply_change(snapshot)

    one_sided = next(
        change
        for change in captured_changes[1:]
        if change.bid_price is not None and change.ask_price is None
    )
    quote = assembler.apply_change(one_sided)

    assert quote is not None
    assert quote.bid_price == one_sided.bid_price
    # Untouched by this message, so it is still exactly what the last message
    # naming an ask said -- here the opening snapshot.
    assert quote.ask_price == snapshot.ask_price
    assert quote.ask_quantity == snapshot.ask_quantity


def test_a_merged_quote_is_as_old_as_its_stalest_side(assembler, captured_changes):
    """The whole point. An active bid must not refresh a forgotten ask."""
    snapshot = captured_changes[0]
    assembler.apply_change(snapshot)

    one_sided = next(
        change
        for change in captured_changes[1:]
        if change.bid_price is not None
        and change.ask_price is None
        and change.venue_time_ns > snapshot.venue_time_ns
    )
    quote = assembler.apply_change(one_sided)

    assert quote.venue_time_ns == snapshot.venue_time_ns
    assert quote.venue_time_ns < one_sided.venue_time_ns


def test_a_change_naming_neither_side_completes_nothing_and_is_counted(
    assembler, captured_changes
):
    """Bybit amends funding, open interest and last price on the same stream."""
    assembler.apply_change(captured_changes[0])
    neither = next(
        (
            change
            for change in captured_changes[1:]
            if change.bid_price is None and change.ask_price is None
        ),
        None,
    )
    if neither is None:
        pytest.skip("this capture window held no quote-silent tickers message")
    assert assembler.apply_change(neither) is None
    assert assembler.describe()["changes_naming_no_side"] >= 1


def test_one_side_before_the_other_has_ever_been_seen_completes_nothing(assembler):
    """A symbol whose first message names a bid only cannot be quoted yet.

    Built from a real captured change with its ask stripped, rather than from a
    payload nobody sent: what is under test is the assembler's state machine, and
    the case -- a subscription whose first frame is a delta -- is one the venue
    only produces on a reconnect this capture did not contain.
    """
    bid_only = QuoteChange(
        venue_id="bybit-linear",
        symbol="NEVERSEENUSDT",
        bid_price=79114.5,
        bid_quantity=1.496,
        ask_price=None,
        ask_quantity=None,
        venue_time_ns=1787590405983_000_000,
        is_snapshot=False,
    )
    assert assembler.apply_change(bid_only) is None
    assert assembler.describe()["changes_still_incomplete"] == 1
    assert assembler.symbols_held() == 0


def test_every_captured_change_leaves_the_assembler_self_consistent(
    assembler, captured_changes
):
    """Replaying the whole captured stream: counts add up and no quote is invented."""
    completed = 0
    for change in captured_changes:
        quote = assembler.apply_change(change)
        if quote is None:
            continue
        completed += 1
        assert quote.venue_id == "bybit-linear"
        assert quote.bid_price > 0 and quote.ask_price > 0
        assert quote.spread >= 0

    standing = assembler.describe()
    assert standing["quotes_completed"] == completed
    assert standing["restated_incompletely"] == 0
    assert (
        completed
        + standing["changes_naming_no_side"]
        + standing["changes_still_incomplete"]
        == len(captured_changes)
    )
