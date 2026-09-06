"""Tests against the real InstrumentListing / BrokerOrderBookUpdate shapes the
Upstox adapter already produces, and real Upstox instrument-master values
(assets.upstox.com/market-quote/instruments/exchange/complete.json.gz, fetched
2026-09-01). Never an invented shape (RL-063)."""

from parts.market_data_feed.broker_quote_bridge import BrokerQuoteBridge
from runtime.brokers.broker_adapter import (
    BrokerOrderBookLevel, BrokerOrderBookUpdate, InstrumentListing,
)
from runtime.part_declaration import load_declaration_from_blueprint

# Upstox's own documented ltpc_combined_limit, which is what the
# unresolved_broker_update_hold_limit setting carries in the running spine.
HELD_INSTRUMENT_LIMIT = 2000

NIFTY_CALL = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)


def _bridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT):
    return BrokerQuoteBridge(held_instrument_limit=held_instrument_limit)


def _book(instrument_key, levels, broker_time_ns=1_740_000_000_000_000_000):
    return BrokerOrderBookUpdate(
        instrument_key=instrument_key,
        levels=tuple(BrokerOrderBookLevel(*level) for level in levels),
        broker_time_ns=broker_time_ns,
    )


def test_the_built_declaration_equals_the_blueprint():
    import parts.market_data_feed.broker_quote_bridge as module

    assert module.PART_DECLARATION == load_declaration_from_blueprint("broker-quote-bridge")


def test_quote_for_is_none_before_the_listing_resolves():
    bridge = _bridge()
    assert bridge.quote_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)])) is None


def test_builds_a_real_normalised_quote_from_the_top_of_book():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    quote = bridge.quote_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)]))
    assert quote is not None
    assert quote.venue_id == "upstox"
    assert quote.symbol == "NIFTY 24500 CE"
    assert quote.bid_price == 225.4
    assert quote.bid_quantity == 75.0
    assert quote.ask_price == 225.7
    assert quote.ask_quantity == 150.0
    assert quote.venue_time_ns == 1_740_000_000_000_000_000
    assert quote.mid_price == (225.4 + 225.7) / 2.0


def test_the_best_level_is_chosen_by_price_not_by_input_order():
    """Upstox's own depth arrives best-first, but a bridge that trusted input
    order and got it wrong would state a mid nobody was quoting."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    quote = bridge.quote_for(
        _book("NSE_FO|1001", [
            (225.35, 30.0, 225.75, 45.0),
            (225.40, 75.0, 225.70, 150.0),
        ])
    )
    assert (quote.bid_price, quote.bid_quantity) == (225.40, 75.0)
    assert (quote.ask_price, quote.ask_quantity) == (225.70, 150.0)


def test_a_padded_zero_priced_level_never_becomes_the_best_ask():
    """Upstox pairs bid and ask at the same depth index and pads the thinner
    side with price=0, quantity=0. A minimum taken across that pad reports a
    best ask of 0 -- the 2026-09-02 bug in broker-order-book-bridge, which here
    would put a mid halfway to zero on the wire as the price to size against."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    quote = bridge.quote_for(
        _book("NSE_FO|1001", [
            (225.40, 75.0, 225.70, 150.0),
            (0.0, 0.0, 0.0, 0.0),
        ])
    )
    assert quote.ask_price == 225.70
    assert quote.bid_price == 225.40


def test_a_book_quoted_on_one_side_only_produces_no_quote():
    """mid_price is (bid + ask) / 2 with no notion of a one-sided market, so a
    quote built without an ask would state a mid at half the bid. A one-sided
    book is a real state of a thin option chain; the honest answer is no quote."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.quote_for(_book("NSE_FO|1001", [(225.40, 75.0, 0.0, 0.0)])) is None
    assert bridge.quote_for(_book("NSE_FO|1001", [(0.0, 0.0, 225.70, 150.0)])) is None
    assert bridge.quotes_refused_for_a_one_sided_book == 2


def test_an_update_held_before_its_listing_is_released_when_it_arrives():
    """The defect measured across all three sibling bridges on 2026-09-06: the
    feed delivers its snapshot on connect while listings arrive on a 300 s
    conveyor, so the burst meets an empty map and used to be dropped."""
    bridge = _bridge()
    assert bridge.quote_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)])) is None
    assert bridge.quotes_now_resolvable() == ()

    bridge.observe_listing(NIFTY_CALL)
    released = bridge.quotes_now_resolvable()
    assert len(released) == 1
    assert released[0].symbol == "NIFTY 24500 CE"
    assert bridge.quotes_now_resolvable() == ()


def test_only_the_newest_held_update_per_instrument_is_released():
    bridge = _bridge()
    bridge.quote_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)]))
    bridge.quote_for(_book("NSE_FO|1001", [(226.1, 60.0, 226.4, 90.0)]))
    bridge.observe_listing(NIFTY_CALL)
    released = bridge.quotes_now_resolvable()
    assert [quote.bid_price for quote in released] == [226.1]


def test_a_released_quote_never_lands_behind_this_tick_s_fresher_one():
    """A quote is a level and its age is the whole reason anyone reads it."""
    bridge = _bridge()
    bridge.quote_for(_book("NSE_FO|1001", [(225.4, 75.0, 225.7, 150.0)]))
    bridge.observe_listing(NIFTY_CALL)
    published = bridge.quotes_from([_book("NSE_FO|1001", [(228.0, 40.0, 228.3, 55.0)])])
    assert [quote.bid_price for quote in published] == [225.4, 228.0]


def test_holding_is_bounded_so_an_unlistable_instrument_cannot_leak():
    bridge = _bridge(held_instrument_limit=2)
    for token in range(5):
        bridge.quote_for(_book(f"NSE_FO|{token}", [(100.0 + token, 5.0, 101.0 + token, 5.0)]))
    standing = bridge._awaiting_listing.describe()
    assert standing["updates_awaiting_listing"] == 2.0
    assert standing["instruments_dropped_at_hold_limit"] == 3.0


def test_standing_reports_what_it_resolved_and_what_it_refused():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    bridge.quote_for(_book("NSE_FO|1001", [(225.40, 75.0, 0.0, 0.0)]))
    standing = __import__(
        "parts.market_data_feed.broker_quote_bridge", fromlist=["describe_bridge"]
    ).describe_bridge(bridge)
    assert standing["part_id"] == "broker-quote-bridge"
    assert standing["instruments_resolved"] == 1
    assert standing["quotes_refused_for_a_one_sided_book"] == 1.0
