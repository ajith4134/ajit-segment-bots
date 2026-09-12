"""Both option universes derive themselves from the broker's own master (RL-063).

The operator, 2026-09-12: *"Lets focus only on option index and option stocks
full universe and retire the intraday cash"*, then *"make sure notin isard coded
and detects auto maticly every tme"*.

So neither segment names its underlyings. `index-options` takes every NSE or BSE
index some option is written on; `stock-options` takes every ordinary NSE share
some option is written on. Both are read off Upstox's real instrument master
every time it is restated, so an exchange listing options on a new index, or NSE
revising the F&O list, is picked up without an edit to any file.

These run against the real master at
`~/.local/share/ajit-segment-bots/instrument-master/complete.json.gz` — 118,388
rows, the file the live feed itself reads. A fixture is exactly what would let a
derived universe quietly resolve to a list somebody typed.

**Full universe of underlyings, not of contracts**, and the distinction is the
whole design. The master carries 27,012 stock option contracts and 9,166 index
ones; the connection accepts 2,000 instrument keys. 220 underlyings at 8
contracts each is 1,980.
"""

from __future__ import annotations

import collections
import statistics

import pytest

from parts.market_data_feed.broker_symbol_universe_bridge import (
    BrokerSymbolUniverseBridge, IndexWithAnOption, StockWithAnOption,
)

# Upstox's free-tier WebSocket limit, from `runtime/brokers/upstox.py`'s own
# declared_limits (`ltpc_combined_limit`, upstox v3 docs fetched 2026-09-01).
CONNECTION_KEY_LIMIT = 2000
CHAIN_WIDTH = 8

# What the real master held on 2026-09-12. Asserted as floors and exact counts
# where the number is a property of the exchange rather than of the file's age.
INDICES_WITH_OPTIONS = 10
STOCKS_WITH_OPTIONS = 210


@pytest.fixture(scope="module")
def real_listings():
    from runtime.brokers.instrument_master import fetch_and_parse_listings
    from runtime.brokers.upstox import UpstoxAdapter

    listings = fetch_and_parse_listings(UpstoxAdapter())
    assert len(listings) > 50_000, (
        "these tests run against the broker's real instrument master; a short one "
        "means the fetch failed rather than that the exchange shrank"
    )
    return listings


def a_bridge(*rules, seed="A-NAME-NO-SEGMENT-TRADES"):
    """A bridge whose seed deliberately names nothing any segment trades.

    Everything it ends up tracking is therefore derived. The constructor refuses
    an empty seed -- an empty universe is the state that let
    `universal-symbol-sweeper` run 3,114 sweeps over nothing on 2026-09-04 --
    so the seed is one name the master does not carry.
    """
    return BrokerSymbolUniverseBridge(
        tracked_trading_symbols=(seed,),
        option_contracts_per_underlying={seed: CHAIN_WIDTH},
        option_underlying_selections=rules,
        derived_chain_width=CHAIN_WIDTH,
    )


def fed(bridge, listings):
    for listing in listings:
        bridge.observe_listing(listing)
    bridge.promote_shares_an_option_is_written_on()
    return bridge


def priced(bridge):
    """Price every tracked underlying at its own chain's median strike.

    The master's own number, used because `contracts_for` ranks by distance from
    the underlying's price and publishes nothing without one. It exercises the
    ranking and the cap; it is not a claim about what anything is worth.
    """
    for key in list(bridge._underlying_by_key):
        contracts = bridge._contracts_by_underlying_key.get(key) or {}
        strikes = [c.strike_price for c in contracts.values() if c.strike_price]
        if strikes:
            bridge._price_by_underlying_key[key] = statistics.median(strikes)
    return bridge


def test_every_index_an_option_is_written_on_is_found_without_being_named(real_listings):
    bridge = fed(a_bridge(IndexWithAnOption()), real_listings)

    assert bridge.standing.derived_option_underlyings == INDICES_WITH_OPTIONS
    symbols = {
        listing.trading_symbol for listing in bridge._underlying_by_key.values()
    }
    assert symbols == {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
        "NIFTYFPI", "FOCIT", "SENSEX", "BANKEX", "SENSEX50",
    }


def test_mcx_excludes_itself_rather_than_being_blacklisted(real_listings):
    """MCXBULLDEX carries options and must not appear.

    Not by name: the rule admits NSE_INDEX and BSE_INDEX, and MCXBULLDEX sits in
    MCX_INDEX, which `docs/goal.md` #6 defers. A blacklist would need editing the
    day MCX lists a second index, which is the thing this whole change removes.
    """
    bridge = fed(a_bridge(IndexWithAnOption()), real_listings)

    symbols = {
        listing.trading_symbol for listing in bridge._underlying_by_key.values()
    }
    assert "MCXBULLDEX" not in symbols
    assert not any(
        getattr(listing, "segment", "").startswith("MCX")
        for listing in bridge._underlying_by_key.values()
    )


def test_every_share_an_option_is_written_on_is_found_without_being_named(real_listings):
    bridge = fed(a_bridge(StockWithAnOption()), real_listings)

    assert bridge.standing.derived_option_underlyings == STOCKS_WITH_OPTIONS
    symbols = {
        listing.trading_symbol for listing in bridge._underlying_by_key.values()
    }
    # The fourteen this segment used to state by hand must all still be there --
    # a derived universe that lost them would be a regression wearing progress.
    assert {
        "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK",
        "ITC", "LT", "BHARTIARTL", "KOTAKBANK", "HINDUNILVR", "MARUTI", "TATASTEEL",
    } <= symbols


def test_a_share_no_option_is_written_on_is_not_this_segments(real_listings):
    """The other half of the rule, and the half that needs the whole master.

    2,655 ordinary shares are listed and 210 carry options. A rule that admitted
    on "ordinary share" alone would put 2,445 names with no chain into an
    options segment's universe.
    """
    bridge = fed(a_bridge(StockWithAnOption()), real_listings)

    assert bridge.standing.shares_held_pending_an_option > 2_000, (
        "the master must carry far more ordinary shares than carry options"
    )
    assert bridge.standing.derived_option_underlyings < 500
    for listing in bridge._underlying_by_key.values():
        assert listing.instrument_key in bridge._derivative_underlying_keys


def test_both_rules_together_fit_the_connection_the_broker_actually_gives(real_listings):
    """The budget this whole shape was chosen against.

    220 underlyings and a chain of 8 on each is 1,980 of 2,000 keys. The full
    universe of *contracts* is 36,178 and was never subscribable; what the
    operator asked for is the full universe of underlyings, which is.
    """
    bridge = priced(fed(
        a_bridge(IndexWithAnOption(), StockWithAnOption()), real_listings
    ))
    universe = bridge.universe()

    underlyings = [e for e in universe if e.instrument_kind != "option"]
    contracts = [e for e in universe if e.instrument_kind == "option"]

    assert len(underlyings) == INDICES_WITH_OPTIONS + STOCKS_WITH_OPTIONS
    assert len(universe) <= CONNECTION_KEY_LIMIT, (
        f"{len(universe)} keys against a connection that accepts "
        f"{CONNECTION_KEY_LIMIT} and evicts nothing"
    )
    assert len(contracts) == len(underlyings) * CHAIN_WIDTH

    per_underlying = collections.Counter()
    for contract in contracts:
        per_underlying[contract.underlying_symbol] += 1
    assert set(per_underlying.values()) == {CHAIN_WIDTH}, (
        "every underlying gets the same chain width; one that got more would be "
        "spending another's slot"
    )
    assert len(per_underlying) == len(underlyings), (
        "an underlying with no chain is a subscription that cannot produce a trade"
    )


def test_a_bridge_given_no_rule_derives_nothing(real_listings):
    """A spine trading only stated universes must be unchanged by any of this."""
    bridge = fed(a_bridge(), real_listings)

    assert bridge.standing.derived_option_underlyings == 0
    assert bridge.standing.shares_held_pending_an_option == 0
    assert bridge._underlying_by_key == {}
