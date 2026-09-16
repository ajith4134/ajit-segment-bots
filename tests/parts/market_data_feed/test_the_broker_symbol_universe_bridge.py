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
import time

import pytest

from parts.broker_adapter.broker_price_level_sampler import BrokerPriceFrame, BrokerPriceLevel
from parts.market_data_feed.broker_symbol_universe_bridge import (
    PART_DECLARATION,
    BrokerSymbolUniverseBridge,
    warm_start_from_the_masters_own_file,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.trading_types import Position

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


def test_a_held_position_on_the_far_expiry_still_gets_priced(real_listings):
    """Real incident, 2026-09-08: 10 of 16 open stock-options positions had no
    live price at all, because their contract was not the CURRENT nearest
    expiry -- `test_only_the_nearest_expiry_is_published` above proves that
    chain is dropped by design. A position does not stop existing when the
    market rolls past its expiry window; it needs its own price to compute
    unrealised P&L and to let its stop trigger, same as any other holding.
    """
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=8)
    # NIFTY 24200 CE 15 SEP 26 -- the fixture's second, farther expiry, already
    # proven excluded from the ranked chain by test_only_the_nearest_expiry_is_published.
    far_expiry_symbol = "NIFTY 24200 CE 15 SEP 26"
    bridge.observe_position(Position(
        venue_id="upstox", symbol=far_expiry_symbol, quantity=50.0,
        average_entry_price=120.0, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=1, updated_at_ns=1,
    ))

    universe = bridge.universe()

    symbols = {e.symbol for e in universe}
    assert far_expiry_symbol in symbols
    assert bridge.standing.held_positions_forced_in == 1
    assert bridge.standing.held_positions_without_a_listing == 0


def test_a_flat_position_is_not_forced_in(real_listings):
    """A closed position has no capital in it -- forcing its listing back into
    the universe would mean a round trip never lets go of connection budget."""
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=8)
    far_expiry_symbol = "NIFTY 24200 CE 15 SEP 26"
    bridge.observe_position(Position(
        venue_id="upstox", symbol=far_expiry_symbol, quantity=0.0,
        average_entry_price=120.0, realised_pnl=45.0, fees_paid=1.0,
        opened_at_ns=1, updated_at_ns=1,
    ))

    universe = bridge.universe()

    assert far_expiry_symbol not in {e.symbol for e in universe}
    assert bridge.standing.held_positions_forced_in == 0


def test_a_held_symbol_with_no_listing_yet_is_counted_not_silently_dropped(real_listings):
    """The listing may not have come round the catalogue conveyor yet -- absence
    is its own state (Rule 8), not zero indistinguishable from 'nothing held'."""
    bridge = a_fed_bridge(real_listings, contracts_per_underlying=8)
    bridge.observe_position(Position(
        venue_id="upstox", symbol="NIFTY 99999 CE 15 SEP 26", quantity=50.0,
        average_entry_price=1.0, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=1, updated_at_ns=1,
    ))

    bridge.universe()

    assert bridge.standing.held_positions_forced_in == 0
    assert bridge.standing.held_positions_without_a_listing == 1


def test_the_part_now_consumes_position():
    assert "position" in PART_DECLARATION.consumes


def test_warm_start_forces_a_held_positions_listing_in_without_waiting_for_the_bus(
    monkeypatch, real_listings,
):
    """The whole point of the 2026-09-08 fix, measured live: 8 minutes after a
    restart, only 2 of 10 held positions had found their listing through the
    paced bus alone. This proves warm start does not need to wait at all --
    one synchronous read of the same file, at start, before the first tick.
    """
    monkeypatch.setattr(
        "runtime.brokers.instrument_master.fetch_and_parse_listings",
        lambda adapter: real_listings,
    )
    bridge = a_bridge()
    far_expiry_symbol = "NIFTY 24200 CE 15 SEP 26"
    bridge.observe_position(Position(
        venue_id="upstox", symbol=far_expiry_symbol, quantity=50.0,
        average_entry_price=120.0, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=1, updated_at_ns=1,
    ))

    warm_start_from_the_masters_own_file(bridge)

    assert bridge.standing.warm_start_listings_loaded == len(real_listings)
    assert bridge.standing.warm_start_failure is None
    assert far_expiry_symbol in {e.symbol for e in bridge.universe()}


def test_warm_start_records_its_own_failure_rather_than_crashing(monkeypatch):
    """Best-effort: a network hiccup here must leave the bridge exactly as it
    would have been without this function, not take the part down."""
    def always_fails(adapter):
        raise OSError("no route to host")

    monkeypatch.setattr(
        "runtime.brokers.instrument_master.fetch_and_parse_listings", always_fails,
    )
    bridge = a_bridge()

    warm_start_from_the_masters_own_file(bridge)  # must not raise

    assert bridge.standing.warm_start_listings_loaded == 0
    assert "OSError" in bridge.standing.warm_start_failure


def test_an_empty_tracked_list_is_refused():
    """An empty list publishes nothing while looking like a working bridge."""
    with pytest.raises(ValueError):
        BrokerSymbolUniverseBridge(
            tracked_trading_symbols=(),
            option_contracts_per_underlying=8,
        )


def test_a_held_share_resolves_to_the_exchange_its_options_settle_against():
    """One share, two exchanges, and file order deciding which one a position means.

    A held position carries a trading symbol, not an instrument key, so this
    bridge looks the listing up by name. Taking the last row to arrive resolved
    10 of the 14 dual-listed shares on the real 2026-09-16 master -- TCS, INFY,
    MARUTI, SBIN, LT, ITC, KOTAKBANK, TATASTEEL, HINDUNILVR, BHARTIARTL -- to
    their BSE line, which no NSE_FO contract settles against (measured: 27,012
    of 27,012 name the NSE line). Forced into the universe it spent a key on a
    connection that evicts nothing, and since every bridge names an instrument
    by its trading symbol its prints published under the same name as the NSE
    line's -- two prices for one symbol, 0.02%-0.09% apart at the median.

    Both orders are asserted, because the defect was order-dependent and a test
    that fed them one way round would have passed before the fix.
    """
    from runtime.brokers.broker_adapter import InstrumentListing

    def share(segment: str, instrument_type: str) -> InstrumentListing:
        return InstrumentListing(
            instrument_key=f"{segment}|INE467B01029", exchange=segment.split("_")[0],
            segment=segment, instrument_type=instrument_type, trading_symbol="TCS",
            lot_size=1, tick_size=0.05, freeze_quantity=None, expiry_ms=None,
            strike_price=None, underlying_key=None,
            intraday_margin_percent=None, intraday_leverage=None,
        )

    nse, bse = share("NSE_EQ", "EQ"), share("BSE_EQ", "A")
    for order in ((nse, bse), (bse, nse)):
        bridge = a_bridge()
        for listing in order:
            bridge.observe_listing(listing)
        resolved = bridge._listing_by_trading_symbol["TCS"]
        assert resolved.instrument_key == "NSE_EQ|INE467B01029", (
            f"fed {[l.segment for l in order]}, resolved to {resolved.segment}"
        )

    # And the refusal is counted rather than silent.
    bridge = a_bridge()
    bridge.observe_listing(nse)
    bridge.observe_listing(bse)
    assert bridge.standing.listings_not_displacing_a_tradeable_namesake == 1


def test_a_name_claimed_only_by_an_untradeable_listing_still_resolves():
    """The rule is a preference, not a filter.

    A trading symbol nothing tradeable claims -- a currency derivative, a
    sovereign gold bond -- still has to resolve to something, or a held position
    on it reads as a position with no listing at all.
    """
    from runtime.brokers.broker_adapter import InstrumentListing

    bond = InstrumentListing(
        instrument_key="NSE_EQ_BOND|SGBX", exchange="NSE", segment="NSE_EQ_BOND",
        instrument_type="SG", trading_symbol="SGBJUN31", lot_size=1, tick_size=0.01,
        freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )
    bridge = a_bridge()
    bridge.observe_listing(bond)
    assert bridge._listing_by_trading_symbol["SGBJUN31"].instrument_key == "NSE_EQ_BOND|SGBX"


@pytest.fixture(scope="module")
def the_whole_instrument_master():
    """Upstox's own master as the live part reads it, or a skip."""
    from runtime.brokers.instrument_master import fetch_and_parse_listings

    try:
        listings = fetch_and_parse_listings(UpstoxAdapter())
    except Exception as failure:  # no cached master on this machine, no network
        pytest.skip(f"no instrument master to read: {type(failure).__name__}: {failure}")
    if not listings:
        pytest.skip("the instrument master parsed to nothing")
    return listings


def test_each_tracked_underlying_enters_the_universe_once_whatever_else_shares_its_name(
    the_whole_instrument_master,
):
    """The universe is one entry per underlying, not one per listing of its name.

    Measured on the real master 2026-09-16, before this rule: the operator's
    220-underlying universe published **430** entries. 208 were the BSE line of
    a share whose options are all NSE -- every one of the 27,012 NSE_FO stock
    contracts settles against the NSE line -- and 2 were debentures listed on
    NSE_EQ under their issuer's own trading symbol (CHOLAFIN, MOTHERSON).

    Both cost the same two ways. A key on a connection that caps at 2,000 and
    evicts nothing, taken from the option contracts that had to fit beside 220
    underlyings at 8 contracts each; and a second price stream published under a
    symbol that already had one, because every bridge downstream names an
    instrument by its trading_symbol.
    """
    from runtime.segment_settings import OptionUnderlyingsByRule

    rule = OptionUnderlyingsByRule()
    for listing in the_whole_instrument_master:
        rule.observe_listing(listing)
    tracked = tuple(sorted(rule.indices | rule.stocks))
    assert tracked, "no underlying carries an option on this master"

    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=tracked, option_contracts_per_underlying=8,
        now_ms=lambda: time.time_ns() // 1_000_000,
    )
    for listing in the_whole_instrument_master:
        bridge.observe_listing(listing)

    underlyings = [
        entry for entry in bridge.universe()
        if getattr(entry, "underlying_venue_instrument_id", None) is None
    ]
    names = [entry.symbol for entry in underlyings]
    assert sorted(names) == sorted(set(names)), (
        "a name reached the universe twice: "
        + ", ".join(sorted({name for name in names if names.count(name) > 1}))
    )
    assert len(underlyings) == len(tracked)


def test_a_debenture_listed_under_its_issuers_symbol_is_not_that_issuers_underlying():
    """NSE_EQ carries far more than shares.

    Real rows, 2026-09-16: `NSE_EQ|INE121A01024` is the CHOLAFIN share and
    `NSE_EQ|INE121A08PJ0` is a CHOLAFIN debenture (`instrument_type` D1). Both
    are on a segment options settle against and both carry the tracked name, so
    the exchange rule alone does not separate them.
    """
    from runtime.brokers.broker_adapter import InstrumentListing

    def row(key: str, instrument_type: str) -> InstrumentListing:
        return InstrumentListing(
            instrument_key=key, exchange="NSE", segment="NSE_EQ",
            instrument_type=instrument_type, trading_symbol="CHOLAFIN", lot_size=1,
            tick_size=0.05, freeze_quantity=None, expiry_ms=None, strike_price=None,
            underlying_key=None, intraday_margin_percent=None, intraday_leverage=None,
        )

    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=("CHOLAFIN",), option_contracts_per_underlying=8,
        now_ms=lambda: 1_788_800_000_000,
    )
    bridge.observe_listing(row("NSE_EQ|INE121A08PJ0", "D1"))
    bridge.observe_listing(row("NSE_EQ|INE121A01024", "EQ"))

    assert list(bridge._underlying_by_key) == ["NSE_EQ|INE121A01024"]
    assert bridge.standing.underlyings_refused_for_their_exchange == 1
