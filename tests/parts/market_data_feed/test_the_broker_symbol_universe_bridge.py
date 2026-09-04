"""`symbol-universe` needs a producer, and what it says has to be matchable.

Eleven parts consume `symbol-universe`. Since `symbol-catalogue-reader` came off
the live spine in the 2026-09-02 crypto cutover, nothing produces it -- so
`universal-symbol-sweeper` swept an empty list 3,114 times with every skip
counter at 0, and the whole chain below it (entry-candidate, bull-side-candidate,
feature vectors, convictions, intents) sat at zero.

These run against Upstox's own NSE instrument master, captured verbatim
(tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json,
RL-063): the real NIFTY and BANKNIFTY index instruments, the whole of NIFTY's
nearest expiry, and part of the following one.

The two things that would make this bridge useless while looking like it worked:
publishing under a symbol nothing else uses, so the sweeper can never match a
universe entry to a price; and publishing the chain whole, which is the failure
the operator's standing ruling on `captured_symbol_count` (2026-08-25) exists to
prevent -- the pair scanner fails as the square of the universe.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.broker_adapter.broker_price_level_sampler import BrokerPriceFrame, BrokerPriceLevel
from parts.market_data_feed.broker_symbol_universe_bridge import (
    PART_DECLARATION,
    BrokerSymbolUniverseBridge,
)
from runtime.brokers.upstox import UpstoxAdapter

CAPTURED = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
)
TRACKED = ("NIFTY", "BANKNIFTY")
# Roughly where NIFTY traded on the day the master was captured. Used only to
# say which strikes are near the money; the ladder itself is the real one.
A_NIFTY_PRICE = 24_500.0


@pytest.fixture(scope="module")
def real_listings():
    rows = json.loads(CAPTURED.read_text())
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    return tuple(UpstoxAdapter.read_instrument_listings(adapter, rows))


def a_bridge(contracts_per_underlying: int = 8) -> BrokerSymbolUniverseBridge:
    return BrokerSymbolUniverseBridge(
        tracked_trading_symbols=TRACKED,
        option_contracts_per_underlying=contracts_per_underlying,
        now_ms=lambda: 1_788_800_000_000,  # before the captured nearest expiry
    )


def a_price_frame(instrument_key: str, price: float) -> BrokerPriceFrame:
    return BrokerPriceFrame(
        broker_id="upstox",
        levels=(BrokerPriceLevel(instrument_key=instrument_key, price=price,
                                 observed_at_ns=1),),
        published_at_ns=1, part_number=1, of_parts=1,
    )


def nifty_index_key(real_listings) -> str:
    return next(l.instrument_key for l in real_listings if l.trading_symbol == "NIFTY")


def a_fed_bridge(real_listings, contracts_per_underlying: int = 8):
    bridge = a_bridge(contracts_per_underlying)
    for listing in real_listings:
        bridge.observe_listing(listing)
    bridge.observe_price_frame(a_price_frame(nifty_index_key(real_listings), A_NIFTY_PRICE))
    return bridge


def test_the_part_produces_symbol_universe():
    assert "symbol-universe" in PART_DECLARATION.produces
    assert "broker-instrument-listing" in PART_DECLARATION.consumes
    assert "broker-price-frame" in PART_DECLARATION.consumes


def test_the_captured_master_is_the_real_one(real_listings):
    """Guards the fixture itself: these are Upstox's own keys, not invented ones."""
    assert any(l.instrument_key == "NSE_INDEX|Nifty 50" for l in real_listings)
    assert sum(1 for l in real_listings if l.instrument_type in ("CE", "PE")) > 150


def test_the_underlyings_are_published_under_their_trading_symbol(real_listings):
    """The sweeper keys its universe (venue_id, symbol) and matches it against
    price frames, which every other bridge publishes under trading_symbol."""
    universe = a_fed_bridge(real_listings).universe()

    symbols = {entry.symbol for entry in universe}
    assert "NIFTY" in symbols
    assert "BANKNIFTY" in symbols
    # Never the instrument_key -- that format is Upstox's own detail (T-4).
    assert not any("|" in entry.symbol for entry in universe)


def test_it_publishes_the_option_chain_not_only_the_underlyings(real_listings):
    universe = a_fed_bridge(real_listings).universe()
    options = [entry for entry in universe if entry.instrument_kind == "option"]

    assert options, "the chain is what this segment actually buys"


def test_the_chain_is_capped_per_underlying(real_listings):
    """The whole nearest expiry is 174 contracts for NIFTY alone; the pair
    scanner fails as the square of the universe (captured_symbol_count, RL)."""
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=8)

    options = [e for e in bridge.universe() if e.instrument_kind == "option"]

    assert len(options) == 8


def test_the_contracts_kept_are_the_ones_nearest_the_money(real_listings):
    """Ranking is what makes a small cap the right small cap: measured
    2026-09-04, only 8.5% of traded NSE_FO instruments ever filled a detector
    window, and the ones that did not are far from the money."""
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=6)

    strikes = {e.strike_price for e in bridge.universe() if e.instrument_kind == "option"}

    assert strikes, "no option was selected at all"
    assert max(abs(strike - A_NIFTY_PRICE) for strike in strikes) < 500.0


def test_only_the_nearest_expiry_is_published(real_listings):
    """The fixture carries a second expiry precisely so this can fail."""
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=200)

    expiries = {e.expiry_ms for e in bridge.universe() if e.instrument_kind == "option"}

    assert len(expiries) == 1


def test_an_underlying_with_no_price_contributes_no_contracts(real_listings):
    """Ranking by distance from a price nobody has read is ranking by nothing.

    A bridge that fell back to 'the first N strikes' would look identical on the
    board while publishing a different universe (Rule 8).
    """
    bridge = a_bridge()
    for listing in real_listings:
        bridge.observe_listing(listing)
    # No price frame observed at all.

    universe = bridge.universe()

    assert not [e for e in universe if e.instrument_kind == "option"]
    assert bridge.standing.underlyings_without_a_price > 0


def test_an_expired_contract_is_never_published(real_listings):
    """`now` past the captured expiry leaves no nearest expiry to publish."""
    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=TRACKED,
        option_contracts_per_underlying=8,
        now_ms=lambda: 1_999_999_999_000,
    )
    for listing in real_listings:
        bridge.observe_listing(listing)
    bridge.observe_price_frame(a_price_frame(nifty_index_key(real_listings), A_NIFTY_PRICE))

    assert not [e for e in bridge.universe() if e.instrument_kind == "option"]


def test_the_price_increment_is_carried_from_the_master(real_listings):
    """tick-size-resolver reads this off symbol-universe; a None would be a
    guess at the increment an order is rounded to."""
    universe = a_fed_bridge(real_listings).universe()
    options = [e for e in universe if e.instrument_kind == "option"]

    assert all(entry.price_increment is not None for entry in options)


def test_an_empty_tracked_list_is_refused():
    """An empty list publishes nothing while looking like a working bridge."""
    with pytest.raises(ValueError):
        BrokerSymbolUniverseBridge(
            tracked_trading_symbols=(),
            option_contracts_per_underlying=8,
        )
