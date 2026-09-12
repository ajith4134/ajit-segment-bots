"""The cash-equity universe is derived by exclusion, from the broker's own master.

The operator's instruction, 2026-09-05: the intraday cash bot trades every NSE
share the derivatives segments do not already cover. Two segments holding one
underlying is exposure nothing bounds -- each stays inside its own risk limits
while the machine as a whole is twice as long as either believes.

Stated as a rule rather than a list. Measured against the whole real NSE master
that day: 2,654 ordinary shares, 210 of them F&O underlyings, 2,444 left. A list
of 2,444 names typed into a settings file is fiction the day NSE adds an F&O
name, and nobody can audit it.

Run against a real captured slice (RL-063), cut from that master and carrying one
of every case the rule must decide: three F&O names with their real option rows,
six ordinary shares nothing is written on, and the four kinds of NSE_EQ row that
are not ordinary shares at all -- a BE-series share, an SME listing, a sovereign
gold bond and a government security.
"""

import pytest

from parts.market_data_feed.broker_symbol_universe_bridge import (
    BrokerSymbolUniverseBridge, EquityWithoutADerivative,
)
from parts.segment_bot.instrument_selector import (
    CHOSEN, SPOT, UNKNOWN_SEGMENT, UPSTOX_VENUE_ID, InstrumentSelector,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.trading_types import OPTION

SECOND_NS = 1_000_000_000


class _AnIntent:
    """The shape `select()` reads: an asset and a direction, nothing more (T-4)."""

    def __init__(self, venue_id, symbol, is_long=True):
        self.venue_id, self.symbol, self.is_long = venue_id, symbol, is_long
        self.notional_quote = 10_000.0
        self.horizon_seconds = 3600.0
        self.needs_convexity = False
        self.fill_within_seconds = None


def _read_captured(name):
    import json
    import pathlib as _pathlib

    return json.loads(
        (
            _pathlib.Path(__file__).resolve().parents[2] / "captured/upstox" / name
        ).read_text()
    )


def _read_slice():
    return _read_captured(SLICE)

SLICE = "2026-09-05-nse-master-cash-equity-slice.json"

# The three F&O names in the slice. Each has real option rows beside it, so the
# exclusion is exercised through the join the master actually provides:
# a derivative's underlying_key is exactly the share's own instrument_key.
COVERED_BY_DERIVATIVES = {"RELIANCE", "TCS", "INFY"}

# The kinds of NSE_EQ row that are not ordinary shares. BYKE is BE-series --
# trade-for-trade, which cannot be traded intraday at all, so an intraday bot
# holding it would place orders the exchange rejects.
NOT_ORDINARY_SHARES = {"BYKE", "KCK", "749RJ35", "GS220830C"}


@pytest.fixture
def listings(read_captured_json):
    return UpstoxAdapter().read_instrument_listings(
        read_captured_json("upstox", SLICE)
    )


def a_bridge():
    return BrokerSymbolUniverseBridge(
        tracked_trading_symbols=("NIFTY",),
        option_contracts_per_underlying={"NIFTY": 50},
        equity_selection=EquityWithoutADerivative(),
    )


class Shortlist:
    """A cash-equity-shortlist, as the bridge reads it -- only `.symbols` matters."""

    def __init__(self, symbols):
        self.symbols = tuple(symbols)


def universe_after_a_full_cycle(bridge, listings, shortlist_symbols=None):
    """Every listing, then the first again -- which is how the bridge learns the
    master has been heard through once. A shortlist naming every trading symbol
    in the slice by default, since exclusion (not the shortlist cut) is what
    most of these tests exercise -- the dedicated shortlist tests pass a
    narrower one explicitly."""
    for listing in listings:
        bridge.observe_listing(listing)
    bridge.observe_listing(listings[0])
    if shortlist_symbols is None:
        shortlist_symbols = {listing.trading_symbol for listing in listings}
    bridge.observe_shortlist(Shortlist(shortlist_symbols))
    return {entry.symbol for entry in bridge.universe()}


def test_a_share_with_no_derivative_is_in_the_universe(listings):
    bridge = a_bridge()

    symbols = universe_after_a_full_cycle(bridge, listings)

    assert bridge.standing.equities_published == 6
    assert bridge.standing.equities_listed == 9
    assert len(symbols & NOT_ORDINARY_SHARES) == 0


def test_a_share_the_derivatives_segments_cover_is_excluded(listings):
    """The whole point: one underlying is never held by two segments at once."""
    bridge = a_bridge()

    symbols = universe_after_a_full_cycle(bridge, listings)

    assert symbols & COVERED_BY_DERIVATIVES == set()
    assert bridge.standing.equities_covered_by_a_derivative == 3


def test_nothing_is_published_until_the_master_has_been_heard_through(listings):
    """A share whose options have not been spoken yet reads as having none.

    The master is restated over a 30-minute cycle, so publishing before the end
    of one would put an F&O name into the cash segment for up to half an hour --
    which is exactly the double exposure this rule exists to prevent.
    """
    bridge = a_bridge()
    for listing in listings:
        bridge.observe_listing(listing)

    symbols = {entry.symbol for entry in bridge.universe()}

    assert bridge.standing.equity_universe_is_waiting_for_a_full_catalogue_cycle
    assert bridge.standing.equities_published == 0
    assert symbols & COVERED_BY_DERIVATIVES == set()
    assert not symbols - {"NIFTY"}


def test_nothing_is_published_until_a_shortlist_has_arrived(listings):
    """A shortlist that has never spoken is not the same as an empty one.

    Publishing every excluded share while waiting would be the unranked,
    uncapped universe the shortlist exists to replace (2026-09-05).
    """
    bridge = a_bridge()
    for listing in listings:
        bridge.observe_listing(listing)
    bridge.observe_listing(listings[0])

    symbols = {entry.symbol for entry in bridge.universe()}

    assert bridge.standing.equity_universe_is_waiting_for_a_shortlist
    assert bridge.standing.equities_published == 0
    assert not symbols - {"NIFTY"}


def test_only_shortlisted_shares_are_published(listings):
    """The shortlist narrows what exclusion already allowed -- it never widens it."""
    bridge = a_bridge()
    for listing in listings:
        bridge.observe_listing(listing)
    bridge.observe_listing(listings[0])
    ordinary_shares = {
        entry.symbol for entry in bridge.universe()
    }  # empty: no shortlist yet
    all_symbols = {listing.trading_symbol for listing in listings}

    # A shortlist of everything: exclusion still applies, nothing more shows up
    # than the by-exclusion test already expects.
    bridge.observe_shortlist(Shortlist(all_symbols))
    published_with_full_shortlist = {entry.symbol for entry in bridge.universe()}
    assert bridge.standing.equities_published == 6

    # A shortlist naming none of the ordinary shares: exclusion still holds,
    # but nothing is published because the shortlist admits nothing.
    bridge.observe_shortlist(Shortlist(COVERED_BY_DERIVATIVES))
    published_with_narrow_shortlist = {entry.symbol for entry in bridge.universe()}
    assert bridge.standing.equities_published == 0
    assert bridge.standing.equities_outside_the_shortlist == 6


def test_the_wait_ends_when_the_first_listing_comes_round_again(listings):
    bridge = a_bridge()

    universe_after_a_full_cycle(bridge, listings)

    assert bridge.standing.catalogue_cycles_heard == 1
    assert not bridge.standing.equity_universe_is_waiting_for_a_full_catalogue_cycle


def test_a_spine_that_asks_for_no_derived_universe_publishes_none(listings):
    """A bridge handed no selection publishes only what it was given, which is
    what a spine trading only derivatives states."""
    bridge = BrokerSymbolUniverseBridge(
        tracked_trading_symbols=("NIFTY",),
        option_contracts_per_underlying={"NIFTY": 50},
    )

    symbols = universe_after_a_full_cycle(bridge, listings)

    assert bridge.standing.equities_published == 0
    assert not symbols - {"NIFTY"}


def test_a_share_is_published_as_spot_and_an_index_is_published_as_no_kind():
    """The join that was never tested, and where the segment's whole session went.

    Both halves of this were already covered: the bridge was tested for *which*
    shares it publishes, and `instrument-selector` was tested for what it does
    with a SPOT listing -- against a `CapturableSymbol` typed into that test.
    Nothing tested that the bridge produces one, and it did not: every
    non-contract went out with `instrument_kind=None`, which the selector reads
    as "an option's underlying" and files in the ATM tracker rather than in the
    book of instruments an intent can be expressed through.

    Measured on the live spine 2026-09-08: the bridge published 47 shares,
    `instrument-selector.chosen_by_kind` held 17,089 `option` and nothing else,
    and `paper-account-cash-equity-intraday` read `fills_applied 0` for the whole
    session while index-options and stock-options both held real positions.

    NIFTY stays kindless on purpose: an index is a number, not something anyone
    can buy, and giving it a tradeable kind would offer the segment an instrument
    the exchange has none of.
    """
    listings = UpstoxAdapter().read_instrument_listings(
        _read_slice()
    )
    bridge = a_bridge()
    universe_after_a_full_cycle(bridge, listings)

    by_symbol = {entry.symbol: entry for entry in bridge.universe()}
    shares = {
        symbol: entry for symbol, entry in by_symbol.items()
        if symbol not in COVERED_BY_DERIVATIVES
        and symbol not in NOT_ORDINARY_SHARES
        and entry.instrument_kind != OPTION
        and symbol != "NIFTY"
    }
    assert shares, "no ordinary share reached the universe, so nothing was checked"
    assert all(entry.instrument_kind == SPOT for entry in shares.values()), {
        symbol: entry.instrument_kind for symbol, entry in shares.items()
    }
    # The index half, from the master slice that carries a real NIFTY index row:
    # kindless, because an index is a number and not something anyone can buy.
    nifty_bridge = a_bridge()
    nifty_listings = UpstoxAdapter().read_instrument_listings(
        _read_captured("2026-09-04-nse-instrument-master-nifty-slice.json")
    )
    for listing in nifty_listings:
        nifty_bridge.observe_listing(listing)
    index_rows = [
        listing for listing in nifty_listings if listing.expiry_ms is None
    ]
    assert index_rows, "the nifty slice carries no index row, so nothing was checked"
    assert nifty_bridge._entry_for_underlying(index_rows[0]).instrument_kind is None


def test_a_published_share_reaches_a_real_instrument_choice():
    """End to end across the seam: the bridge's own entry, into the real selector.

    This is the assertion the two halves could not make separately -- that what
    the bridge publishes is a thing `instrument-selector` can express a
    cash-equity intent through.
    """
    listings = UpstoxAdapter().read_instrument_listings(_read_slice())
    bridge = a_bridge()
    universe_after_a_full_cycle(bridge, listings)

    share = next(
        entry for entry in bridge.universe()
        if entry.instrument_kind == SPOT and entry.symbol not in NOT_ORDINARY_SHARES
    )

    selector = InstrumentSelector(
        built_segments=("cash-equity-intraday",), maximum_cost_fraction=0.05,
        round_trip_cost_fraction=0.001,
        segment_of=lambda instrument: (
            "cash-equity-intraday" if instrument.instrument_kind == SPOT else UNKNOWN_SEGMENT
        ),
    )
    selector.observe_listed_symbol(share)
    selector.observe_price(UPSTOX_VENUE_ID, share.symbol, 100.0, observed_at_ns=1_000 * SECOND_NS)

    choice = selector.select(_AnIntent(UPSTOX_VENUE_ID, share.symbol))

    assert choice.state == CHOSEN, choice.reason
    assert choice.chosen.contract_symbol == share.symbol
    assert choice.chosen.instrument_kind == SPOT
