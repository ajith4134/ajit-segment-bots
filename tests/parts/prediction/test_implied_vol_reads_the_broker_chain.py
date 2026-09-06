"""implied-vol-reader against the real Upstox chain shapes.

The part had `reads 0 / quotes_seen 0 / surfaces_published 0` for its whole life
(measured on the live spine 2026-09-06) because `start_part` was a stub citing
RL-050, the crypto build order under which options were unbuilt. These are the
pieces the rewiring added, tested against the real `InstrumentListing`,
`BrokerOptionGreeks` and `NormalisedQuote` shapes -- never an invented one
(RL-063).
"""

from parts.prediction.implied_vol_reader import (
    CALL, PUT, MILLISECONDS_TO_NANOSECONDS,
    ImpliedVolReader, OptionChainListings, option_quote_from,
)
from runtime.brokers.broker_adapter import BrokerOptionGreeks, InstrumentListing
from runtime.venues.venue_adapter import NormalisedQuote

NIFTY_INDEX = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None, underlying_symbol=None,
)
NIFTY_CALL = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None, underlying_symbol="NIFTY",
)
NIFTY_PUT = InstrumentListing(
    instrument_key="NSE_FO|1002", exchange="NSE", segment="NSE_FO",
    instrument_type="PE", trading_symbol="NIFTY 24500 PE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None, underlying_symbol="NIFTY",
)
RELIANCE = InstrumentListing(
    instrument_key="NSE_EQ|INE002A01018", exchange="NSE", segment="NSE_EQ",
    instrument_type="EQ", trading_symbol="RELIANCE", lot_size=1, tick_size=0.05,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None, underlying_symbol=None,
)

NOW_NS = 1_740_000_000_000 * 1_000_000


def _greeks(instrument_key, implied_volatility=0.145, broker_time_ns=NOW_NS):
    return BrokerOptionGreeks(
        instrument_key=instrument_key, delta=0.52, theta=-8.1, gamma=0.0004,
        vega=12.3, rho=1.1, implied_volatility=implied_volatility,
        broker_time_ns=broker_time_ns,
    )


def _quote(symbol, bid=220.0, ask=220.6):
    return NormalisedQuote(
        venue_id="upstox", symbol=symbol, bid_price=bid, bid_quantity=75.0,
        ask_price=ask, ask_quantity=150.0, venue_time_ns=NOW_NS,
    )


# ---- OptionChainListings -----------------------------------------------------

def test_a_listing_is_looked_up_by_key_and_by_its_own_symbol():
    held = OptionChainListings()
    held.observe_listing(NIFTY_CALL)
    assert held.listing_of("NSE_FO|1001") is NIFTY_CALL
    assert held.symbol_of("NSE_FO|1001") == "NIFTY 24500 CE"
    assert held.instruments_known == 1


def test_the_underlying_is_named_without_the_underlyings_own_listing():
    """The measurement that decided this: only **26 of 1,707** subscribed
    options had their underlying subscribed as well, so resolving
    `underlying_key` against the underlying's own listing would have waited for
    ever on 98% of the chain. Every one of the 94,352 options in Upstox's
    master states `underlying_symbol` outright, and 17,800 carry no
    `underlying_key` at all."""
    held = OptionChainListings()
    held.observe_listing(NIFTY_CALL)
    # The underlying's listing is deliberately never observed here.
    assert held.symbol_of("NSE_INDEX|Nifty 50") is None
    option = option_quote_from(
        NIFTY_CALL, "NIFTY 24500 CE",
        _quote("NIFTY 24500 CE"), _greeks("NSE_FO|1001"), NOW_NS,
    )
    assert option is not None
    assert option.underlying == "NIFTY"


def test_a_contract_that_names_no_underlying_at_all_is_refused():
    """Not guessed from the trading symbol: a surface filed under a parsed name
    is a surface no consumer of the real name would ever find."""
    nameless = InstrumentListing(
        instrument_key="NSE_FO|1003", exchange="NSE", segment="NSE_FO",
        instrument_type="CE", trading_symbol="MYSTERY 100 CE", lot_size=1,
        tick_size=0.05, freeze_quantity=None, expiry_ms=1_740_100_000_000,
        strike_price=100.0, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None, underlying_symbol=None,
    )
    assert option_quote_from(
        nameless, "MYSTERY 100 CE",
        _quote("MYSTERY 100 CE"), _greeks("NSE_FO|1003"), NOW_NS,
    ) is None


def test_a_listing_with_no_instrument_key_is_skipped_not_raised_on():
    held = OptionChainListings()
    held.observe_listing(object())
    assert held.instruments_known == 0


# ---- option_quote_from -------------------------------------------------------

def test_a_contract_becomes_a_quote_carrying_the_brokers_own_implied_volatility():
    """Read, never modelled: the number served is the one the market made, not
    one solved locally from a price."""
    option = option_quote_from(
        NIFTY_CALL, "NIFTY 24500 CE",
        _quote("NIFTY 24500 CE"), _greeks("NSE_FO|1001", implied_volatility=0.145), NOW_NS,
    )
    assert option is not None
    assert option.kind == CALL
    assert option.strike == 24500.0
    assert option.underlying == "NIFTY"
    assert option.bid == 220.0 and option.ask == 220.6
    assert option.implied_volatility == 0.145
    assert option.is_two_sided


def test_a_put_reads_as_a_put():
    option = option_quote_from(
        NIFTY_PUT, "NIFTY 24500 PE",
        _quote("NIFTY 24500 PE"), _greeks("NSE_FO|1002"), NOW_NS,
    )
    assert option.kind == PUT


def test_an_instrument_that_is_not_an_option_contributes_no_point():
    """A subscription carries equities, futures and an index too."""
    assert option_quote_from(
        RELIANCE, "RELIANCE",
        _quote("RELIANCE"), _greeks("NSE_EQ|INE002A01018"), NOW_NS,
    ) is None


def test_seconds_to_expiry_is_measured_from_now_and_goes_negative_when_past():
    """An expired contract must not read as the nearest expiry."""
    before = option_quote_from(
        NIFTY_CALL, "NIFTY 24500 CE",
        _quote("NIFTY 24500 CE"), _greeks("NSE_FO|1001"), NOW_NS,
    )
    expected = (NIFTY_CALL.expiry_ms * MILLISECONDS_TO_NANOSECONDS - NOW_NS) / 1e9
    assert before.seconds_to_expiry == expected
    assert before.seconds_to_expiry > 0

    after_expiry_ns = (NIFTY_CALL.expiry_ms + 60_000) * MILLISECONDS_TO_NANOSECONDS
    after = option_quote_from(
        NIFTY_CALL, "NIFTY 24500 CE",
        _quote("NIFTY 24500 CE"), _greeks("NSE_FO|1001"), after_expiry_ns,
    )
    assert after.seconds_to_expiry < 0


def test_the_quote_is_stamped_with_the_brokers_own_time_not_arrival_time():
    """The staleness bound is about the market's age, not our latency."""
    stamped = 1_739_999_000_000 * 1_000_000
    option = option_quote_from(
        NIFTY_CALL, "NIFTY 24500 CE", _quote("NIFTY 24500 CE"),
        _greeks("NSE_FO|1001", broker_time_ns=stamped), NOW_NS,
    )
    assert option.quoted_at_ns == stamped


# ---- the holding fix ---------------------------------------------------------

def test_a_contracts_newer_quote_replaces_its_older_one():
    """Was an append-only list, harmless only while the part received nothing.
    On a live chain at hundreds of quotes a second it grew without bound, and
    the staleness filter does not help -- it runs at read time and leaves what
    it dropped in the list."""
    reader = ImpliedVolReader(
        maximum_quote_age_seconds=60.0, minimum_strikes_per_expiry=3,
        now_ns=lambda: NOW_NS,
    )
    for implied in (0.10, 0.12, 0.15):
        reader.observe_quote(
            "upstox",
            option_quote_from(
                NIFTY_CALL, "NIFTY 24500 CE", _quote("NIFTY 24500 CE"),
                _greeks("NSE_FO|1001", implied_volatility=implied), NOW_NS,
            ),
        )
    assert reader.standing.quotes_seen == 3
    held = reader._quotes["upstox:NIFTY"]
    assert len(held) == 1
    assert held["NIFTY 24500 CE"].implied_volatility == 0.15


# ---- the feed-connected state stays reachable --------------------------------

def test_a_read_before_any_greek_arrives_still_says_no_feed():
    """Never a flat surface. A flat surface is an invented forward view and it
    would reach every sizing decision through the vol features."""
    reader = ImpliedVolReader(
        maximum_quote_age_seconds=60.0, minimum_strikes_per_expiry=3,
        now_ns=lambda: NOW_NS,
    )
    surface = reader.read("upstox", "NIFTY", spot=24500.0)
    assert surface.state == "no-options-feed-is-connected"
    assert not surface.is_usable
    assert surface.by_expiry == {}
