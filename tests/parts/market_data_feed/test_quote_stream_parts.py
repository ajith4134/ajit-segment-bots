"""The two quote parts, on payloads both venues actually sent.

`venue-quote-stream-reader` turns a venue's frames into whole quotes;
`quote-level-sampler` turns a stream of those into one frame per venue per tick.
The chain from captured bytes to a level a decision can be sized against is
exercised end to end here, because every link in it was written at once and a
test of each link alone would not have caught a mismatch between them.

RL-063: the payloads come from `tests/captured/`, never from a fixture typed here.
"""

import importlib

import pytest

from parts.market_data_feed.quote_level_sampler import (
    PART_ID as SAMPLER_PART_ID,
    QuoteLevelSampler,
    describe_quote_sampling,
)
from parts.market_data_feed.venue_quote_stream_reader import (
    PART_ID as READER_PART_ID,
    QuoteReaderStanding,
    describe_quote_reading,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.quote_assembly import QuoteAssembler
from runtime.quote_frames import quote_levels_in
from runtime.venues.adapter_registry import load_venue_adapter

QUOTE_PARTS = {
    "venue-quote-stream-reader": "parts.market_data_feed.venue_quote_stream_reader",
    "quote-level-sampler": "parts.market_data_feed.quote_level_sampler",
}

QUOTE_FIXTURES = {
    "binance-usdm": "2026-08-24-ws-bookticker-all-symbols.jsonl",
    "bybit-linear": "2026-08-24-public-linear-tickers-through-one-sided-delta.jsonl",
}


@pytest.mark.parametrize("part_id", sorted(QUOTE_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    """RL-067: what is built matches the diagram, checked rather than trusted."""
    module = importlib.import_module(QUOTE_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


def quotes_from(venue_id, read_captured_payloads):
    """Every complete quote in a venue's captured stream, through the real chain."""
    adapter = load_venue_adapter(venue_id)
    assembler = QuoteAssembler(venue_id, adapter.quote_stream_amends_rather_than_restates())
    return [
        quote
        for _, payload in read_captured_payloads(venue_id, QUOTE_FIXTURES[venue_id])
        for change in adapter.read_quote_changes(payload)
        if (quote := assembler.apply_change(change)) is not None
    ]


# ---- quote-level-sampler ---------------------------------------------------------


@pytest.mark.parametrize("venue_id", sorted(QUOTE_FIXTURES))
def test_a_captured_stream_becomes_levels_a_decision_can_be_sized_against(
    venue_id, read_captured_payloads
):
    """The whole chain: venue bytes -> changes -> quotes -> frame -> level."""
    sampler = QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=2000)
    for quote in quotes_from(venue_id, read_captured_payloads):
        sampler.observe_quote(quote)

    frames = sampler.frames_due(now_ns=2**62)
    assert frames, "a stream of real quotes published no frame"
    levels = list(quote_levels_in(frames))
    assert levels
    for level in levels:
        assert level.venue_id == venue_id
        assert level.bid_price < level.ask_price
        assert level.bid_price < level.mid_price < level.ask_price
        assert level.spread_fraction >= 0


def test_a_level_keeps_the_venue_s_moment_never_the_frame_s(read_captured_payloads):
    """The property the whole design turns on, checked on real timestamps."""
    quotes = quotes_from("binance-usdm", read_captured_payloads)
    sampler = QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=2000)
    for quote in quotes:
        sampler.observe_quote(quote)

    published_at = 2**62
    frames = sampler.frames_due(now_ns=published_at)
    stamps = {quote.symbol: quote.venue_time_ns for quote in quotes}
    for level in quote_levels_in(frames):
        assert level.observed_at_ns == stamps[level.symbol]
        assert level.observed_at_ns != published_at


def test_a_crossed_quote_is_refused_rather_than_priced(read_captured_payloads):
    """A bid above an ask is a moment caught mid-update, not a market.

    Built by inverting a real captured quote rather than by inventing one: what is
    under test is the sampler's guard, and the venues do not send crossed quotes
    to order on demand.
    """
    from dataclasses import replace

    real = quotes_from("binance-usdm", read_captured_payloads)[0]
    crossed = replace(real, bid_price=real.ask_price + 1.0)

    sampler = QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=2000)
    sampler.observe_quote(crossed)

    assert describe_quote_sampling(sampler)["crossed_quotes_refused"] == 1
    assert describe_quote_sampling(sampler)["quotes_observed"] == 0
    assert sampler.frames_due(now_ns=2**62) == ()


def test_no_frame_is_published_before_the_cadence_has_elapsed(read_captured_payloads):
    sampler = QuoteLevelSampler(cadence_seconds=1.0, maximum_symbols_per_frame=2000)
    for quote in quotes_from("binance-usdm", read_captured_payloads):
        sampler.observe_quote(quote)

    assert sampler.frames_due(now_ns=1_000_000_000)
    assert sampler.frames_due(now_ns=1_500_000_000) == ()
    assert sampler.frames_due(now_ns=2_100_000_000)


def test_a_universe_too_large_for_one_frame_is_split_and_says_so(read_captured_payloads):
    """The bus refuses an oversized datagram, so a split is counted, never silent."""
    quotes = quotes_from("binance-usdm", read_captured_payloads)
    assert len(quotes) > 4, "this fixture is too small to test splitting"

    sampler = QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=2)
    for quote in quotes:
        sampler.observe_quote(quote)

    frames = sampler.frames_due(now_ns=2**62)
    assert len(frames) > 1
    assert all(frame.was_split for frame in frames)
    assert describe_quote_sampling(sampler)["frames_split"] == 1
    assert {level.symbol for level in quote_levels_in(frames)} == {
        quote.symbol for quote in quotes
    }


def test_the_sampler_reports_itself_under_its_own_part_id(read_captured_payloads):
    sampler = QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=2000)
    assert describe_quote_sampling(sampler)["part_id"] == SAMPLER_PART_ID


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_cadence_that_would_never_publish_is_refused(bad):
    with pytest.raises(ValueError):
        QuoteLevelSampler(cadence_seconds=bad, maximum_symbols_per_frame=10)


def test_a_frame_bound_below_one_symbol_is_refused():
    with pytest.raises(ValueError):
        QuoteLevelSampler(cadence_seconds=0.25, maximum_symbols_per_frame=0)


# ---- venue-quote-stream-reader ---------------------------------------------------


def test_the_reader_reports_every_venue_not_only_the_first():
    """A part reports one standing, and this part carries every captured venue.

    Reporting the first venue's counters would make a dead second venue invisible
    -- the defect commit 7a70a55 fixed for fourteen other parts.
    """
    readers = {
        "binance-usdm": _StubReader("binance-usdm", connections=1, drained=10, published=9),
        "bybit-linear": _StubReader("bybit-linear", connections=3, drained=40, published=31),
    }
    standing = describe_quote_reading(readers)

    assert standing["part_id"] == READER_PART_ID
    assert standing["venues"] == 2
    assert standing["connections"] == 4
    assert standing["messages_drained"] == 50
    assert standing["quotes_published"] == 40
    assert standing["symbols_held"] == 12


def test_a_reader_that_has_read_nothing_reports_zeroes_rather_than_nothing():
    """Absence of evidence renders as its own number, never as a missing key."""
    standing = describe_quote_reading({})
    assert standing["venues"] == 0
    for key in ("connections", "messages_drained", "quotes_published", "symbols_held"):
        assert standing[key] == 0


class _StubReader:
    """A reader that has already run, so the merge of standings can be checked alone.

    Not a stub of the venue: the connection, the adapter and the assembler are all
    tested against real payloads elsewhere. What is under test here is arithmetic
    across venues, and driving it with two live sockets would test the network.
    """

    def __init__(self, venue_id, connections, drained, published):
        self.standing = QuoteReaderStanding(
            venue_id=venue_id,
            connections=connections,
            messages_drained=drained,
            quotes_published=published,
            assembler={"symbols_held": 6, "changes_naming_no_side": 1},
        )
