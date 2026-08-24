"""Both venues' quote streams, against what each venue actually sent.

Captured 2026-08-24 into `tests/captured/`, provenance in `capture-manifest.json`.
The claims worth capturing for are the two that differ between the venues and
that no amount of reading settles:

* Binance restates a whole quote every frame, one symbol per frame, on the bare
  `/ws` route -- the opposite route from its ticker streams, and a wrong route
  there is an open connection that delivers nothing.
* Bybit amends. Its first message per subscription restates everything and the
  rest carry only what moved, so a delta can name a bid and no ask at all. That
  frame is in the fixture on purpose: the capture ran until one arrived.

RL-063: nothing here is a payload anybody typed.
"""

import pytest

from runtime.quote_assembly import QuoteAssembler
from runtime.tape import StreamKind
from runtime.venues.binance_usdm import (
    EVERY_SYMBOL_QUOTE_STREAM,
    QUOTE_ROUTE,
    build_venue_adapter as build_binance_adapter,
)
from runtime.venues.bybit_linear import build_venue_adapter as build_bybit_adapter
from runtime.venues.venue_adapter import SequenceContinuity, StreamRequest

BINANCE_QUOTE_FIXTURE = "2026-08-24-ws-bookticker-all-symbols.jsonl"
BYBIT_QUOTE_FIXTURE = "2026-08-24-public-linear-tickers-through-one-sided-delta.jsonl"


@pytest.fixture
def binance():
    return build_binance_adapter()


@pytest.fixture
def bybit():
    return build_bybit_adapter()


def quote_changes(adapter, records):
    """Every quote change in a captured stream, control frames dropped by the adapter."""
    return [change for _, payload in records for change in adapter.read_quote_changes(payload)]


# ---- Binance: one route, one topic, whole quotes ---------------------------------


def test_binance_quotes_answer_on_the_bare_route_not_the_market_one(binance):
    """The route is the fact. Measured 2026-08-24: !bookTicker is silent on /market/ws.

    Asserted against the trade route rather than against a literal, so this fails
    if the two are ever made the same by accident -- which is the mistake that
    produces a healthy connection and an empty tape.
    """
    assert binance.stream_endpoint_url(StreamKind.QUOTE) == QUOTE_ROUTE
    assert binance.stream_endpoint_url(StreamKind.QUOTE) != binance.stream_endpoint_url(
        StreamKind.TRADE
    )
    assert binance.stream_endpoint_url(StreamKind.QUOTE) != binance.stream_endpoint_url(
        StreamKind.BOOK
    )


def test_binance_covers_every_symbol_with_one_topic(binance):
    assert binance.every_symbol_quote_topic() == EVERY_SYMBOL_QUOTE_STREAM
    assert binance.subscription_topic(StreamRequest(StreamKind.QUOTE, "BTCUSDT")) == (
        "btcusdt@bookTicker"
    )


def test_binance_restates_rather_than_amends(binance):
    assert binance.quote_stream_amends_rather_than_restates() is False


def test_binance_quote_frames_each_carry_one_symbol_and_both_sides(
    binance, read_captured_payloads
):
    """40 captured frames held 39 distinct symbols: this is the whole market, one at a time."""
    records = read_captured_payloads("binance-usdm", BINANCE_QUOTE_FIXTURE)
    changes = quote_changes(binance, records)
    assert len(changes) > 1
    assert len({change.symbol for change in changes}) > 1
    for change in changes:
        assert change.is_complete(), f"{change.symbol} arrived without both sides"
        assert change.is_snapshot
        assert change.bid_price < change.ask_price
        assert change.bid_quantity > 0 and change.ask_quantity > 0


def test_binance_quote_facts_index_on_the_market_moment(binance, read_captured_payloads):
    """`T`, the engine's stamp, not `E`, the push. A quote's whole value is its age."""
    records = read_captured_payloads("binance-usdm", BINANCE_QUOTE_FIXTURE)
    for _, payload in records:
        facts = binance.read_message_facts(payload)
        if facts is None:  # the subscribe acknowledgement
            continue
        assert facts.stream_kind is StreamKind.QUOTE
        import json

        message = json.loads(payload)
        assert facts.venue_time_ns == int(message["T"]) * 1_000_000
        assert facts.sequence == int(message["u"])


def test_binance_quote_sequence_is_only_promised_non_decreasing(binance):
    """`u` counts book events, not quote events, so consecutive quotes jump."""
    assert binance.sequence_continuity(StreamKind.QUOTE) is SequenceContinuity.NON_DECREASING


# ---- Bybit: named symbols, amended quotes ----------------------------------------


def test_bybit_has_no_wildcard_so_every_symbol_is_named(bybit):
    assert bybit.every_symbol_quote_topic() is None
    assert bybit.subscription_topic(StreamRequest(StreamKind.QUOTE, "btcusdt")) == "tickers.BTCUSDT"


def test_bybit_amends_rather_than_restates(bybit):
    assert bybit.quote_stream_amends_rather_than_restates() is True


def test_bybit_fixture_holds_the_one_sided_delta_the_merge_exists_for(
    bybit, read_captured_payloads
):
    """Without this frame the fixture would prove nothing about the amending case."""
    records = read_captured_payloads("bybit-linear", BYBIT_QUOTE_FIXTURE)
    changes = quote_changes(bybit, records)
    named_one_side_only = [
        change
        for change in changes
        if not change.is_complete()
        and (change.bid_price is not None or change.ask_price is not None)
    ]
    assert named_one_side_only, (
        "the captured tickers stream contains no one-sided delta, so this fixture cannot "
        "check the merge. Recapture it -- the capture runs until one arrives."
    )


def test_bybit_reports_an_absent_side_as_absent_never_as_zero(bybit, read_captured_payloads):
    """Zero is a market nobody quoted. None is the venue not having said."""
    records = read_captured_payloads("bybit-linear", BYBIT_QUOTE_FIXTURE)
    for change in quote_changes(bybit, records):
        for price, quantity in (
            (change.bid_price, change.bid_quantity),
            (change.ask_price, change.ask_quantity),
        ):
            assert price is None or price > 0
            assert quantity is None or quantity >= 0


def test_bybit_marks_only_its_first_message_a_snapshot(bybit, read_captured_payloads):
    records = read_captured_payloads("bybit-linear", BYBIT_QUOTE_FIXTURE)
    changes = quote_changes(bybit, records)
    assert changes[0].is_snapshot
    assert not any(change.is_snapshot for change in changes[1:])


def test_bybit_quotes_assemble_into_whole_quotes_across_the_captured_stream(
    bybit, read_captured_payloads
):
    """The end-to-end claim: a real amending stream becomes real complete quotes."""
    records = read_captured_payloads("bybit-linear", BYBIT_QUOTE_FIXTURE)
    assembler = QuoteAssembler(
        "bybit-linear", bybit.quote_stream_amends_rather_than_restates()
    )
    quotes = [
        quote
        for change in quote_changes(bybit, records)
        if (quote := assembler.apply_change(change)) is not None
    ]
    assert quotes
    for quote in quotes:
        assert quote.bid_price < quote.ask_price
        assert quote.mid_price == pytest.approx((quote.bid_price + quote.ask_price) / 2)
    assert assembler.describe()["restated_incompletely"] == 0
