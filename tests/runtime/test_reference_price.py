"""Choosing which price to act on, against real bid/ask and real trades.

The rule this file checks is the answer to a problem that kept recurring here:
every part that judged a price was hardcoded to the last trade, which is the one
source that goes stale exactly when it is needed -- a symbol nobody is trading has
no recent trade and a perfectly good resting market. Measured 2026-08-24,
spread-reversion-detector refused 1,438,376 of 4,199,062 tests on that.

The shape comes from nautilus_trader's `PriceType` (`Bid, Ask, Mid, Last, Mark`),
read from source: name the price source rather than assume it.

The quotes here are real ones off Binance's `!bookTicker`, captured the same day
(RL-063). Their spreads are real too, which matters: the rule turns on how wide a
market is, and a made-up spread would be a test of the number somebody typed.
"""

import pytest

from runtime.reference_price import (
    BOTH_TOO_OLD,
    NEVER_SEEN,
    QUOTE_TOO_WIDE,
    ChosenPrice,
    NoPrice,
    ReferencePriceChooser,
)
from runtime.venues.binance_usdm import build_venue_adapter

SECOND_NS = 1_000_000_000
QUOTE_FIXTURE = "2026-08-24-ws-bookticker-all-symbols.jsonl"


class FixedBound:
    """A staleness bound that does not learn, so these tests measure one thing."""

    def __init__(self, seconds):
        self.value = seconds
        self.reason = "a bound fixed for this test"


class FixedStaleness:
    def __init__(self, seconds=10.0):
        self._bound = FixedBound(seconds)

    def believable_age_seconds(self, venue_id, symbol):
        return self._bound


@pytest.fixture
def real_quotes(read_captured_payloads):
    """Every real quote in the captured all-market frame, narrowest spread first."""
    adapter = build_venue_adapter()
    quotes = [
        change
        for _, payload in read_captured_payloads("binance-usdm", QUOTE_FIXTURE)
        for change in adapter.read_quote_changes(payload)
    ]
    assert quotes
    return sorted(quotes, key=lambda q: (q.ask_price - q.bid_price) / q.ask_price)


@pytest.fixture
def chooser():
    # A round trip at the shipped taker rate: the same yardstick the rest of the
    # system uses for whether a price difference matters.
    return ReferencePriceChooser(FixedStaleness(10.0), materiality_fraction=0.001)


def test_a_symbol_with_neither_a_trade_nor_a_quote_says_so(chooser):
    """Never seen is not a stale price and not a zero."""
    answer = chooser.price_for("binance-usdm", "NOTHINGUSDT", 0)
    assert isinstance(answer, NoPrice)
    assert answer.reason == NEVER_SEEN


def test_a_fresh_trade_beats_a_fresh_quote(chooser, real_quotes):
    """A trade is what somebody paid; a mid is what two people are asking for."""
    quote = real_quotes[0]
    chooser.observe_trade("binance-usdm", quote.symbol, 123.5, 0)
    chooser.observe_quote(
        "binance-usdm", quote.symbol, quote.bid_price, quote.ask_price, 5 * SECOND_NS
    )

    answer = chooser.price_for("binance-usdm", quote.symbol, 5 * SECOND_NS)
    assert isinstance(answer, ChosenPrice)
    assert answer.price == 123.5
    assert not answer.came_from_a_quote
    assert chooser.describe()["priced_from_a_trade"] == 1


def test_a_narrow_quote_stands_in_once_the_trade_is_too_old(chooser, real_quotes):
    """The whole point: the symbol stopped trading, the market did not go away."""
    quote = real_quotes[0]
    width = (quote.ask_price - quote.bid_price) / quote.ask_price
    assert width <= 0.001, "the narrowest real quote should be inside a round trip"

    chooser.observe_trade("binance-usdm", quote.symbol, 123.5, 0)
    chooser.observe_quote(
        "binance-usdm", quote.symbol, quote.bid_price, quote.ask_price, 20 * SECOND_NS
    )

    answer = chooser.price_for("binance-usdm", quote.symbol, 25 * SECOND_NS)
    assert isinstance(answer, ChosenPrice)
    assert answer.came_from_a_quote
    assert answer.price == pytest.approx((quote.bid_price + quote.ask_price) / 2)
    assert answer.observed_at_ns == 20 * SECOND_NS
    assert chooser.describe()["priced_from_a_quote"] == 1


def test_a_quote_wider_than_a_round_trip_is_not_a_stand_in(chooser, real_quotes):
    """Its mid is not within a round trip of anything anybody would trade at."""
    quote = real_quotes[0]
    # A real symbol's real bid, widened to a real market's worst shape.
    chooser.observe_trade("binance-usdm", quote.symbol, quote.bid_price, 0)
    chooser.observe_quote(
        "binance-usdm", quote.symbol,
        quote.bid_price, quote.bid_price * 1.05, 20 * SECOND_NS,
    )

    answer = chooser.price_for("binance-usdm", quote.symbol, 25 * SECOND_NS)
    assert isinstance(answer, NoPrice)
    assert answer.reason == QUOTE_TOO_WIDE
    assert chooser.describe()["refused_quote_too_wide"] == 1


def test_both_too_old_refuses_and_says_both_ages(chooser, real_quotes):
    quote = real_quotes[0]
    chooser.observe_trade("binance-usdm", quote.symbol, 123.5, 0)
    chooser.observe_quote("binance-usdm", quote.symbol, quote.bid_price, quote.ask_price, 0)

    answer = chooser.price_for("binance-usdm", quote.symbol, 60 * SECOND_NS)
    assert isinstance(answer, NoPrice)
    assert answer.reason == BOTH_TOO_OLD
    assert chooser.describe()["refused_both_too_old"] == 1


def test_a_crossed_quote_is_not_a_market_and_is_not_kept(chooser, real_quotes):
    """quote-level-sampler already refuses these; keeping one here would price on it."""
    quote = real_quotes[0]
    chooser.observe_quote(
        "binance-usdm", quote.symbol, quote.ask_price + 1.0, quote.bid_price, 0
    )
    assert chooser.describe()["symbols_with_a_quote"] == 0
    assert isinstance(chooser.price_for("binance-usdm", quote.symbol, 0), NoPrice)


def test_every_real_quote_in_the_capture_prices_or_is_refused_for_width(
    chooser, real_quotes
):
    """No real quote may produce a price outside its own bid and ask."""
    for quote in real_quotes:
        chooser.observe_quote(
            "binance-usdm", quote.symbol, quote.bid_price, quote.ask_price, 0
        )
        answer = chooser.price_for("binance-usdm", quote.symbol, 0)
        if isinstance(answer, ChosenPrice):
            assert quote.bid_price <= answer.price <= quote.ask_price
        else:
            assert answer.reason == QUOTE_TOO_WIDE


def test_a_materiality_that_would_believe_anything_is_refused():
    with pytest.raises(ValueError):
        ReferencePriceChooser(FixedStaleness(), materiality_fraction=0.0)
