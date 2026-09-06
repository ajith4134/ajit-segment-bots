"""Tests against real Upstox instrument-master values (assets.upstox.com/
market-quote/instruments/exchange/complete.json.gz, fetched 2026-09-01) and
the real InstrumentListing/LtpUpdate shapes already verified this session.
Never an invented shape (RL-063)."""

from runtime.brokers.broker_adapter import InstrumentListing, LtpUpdate
from runtime.tape import NOT_SENT, TradeFidelity
from parts.market_data_feed.broker_market_data_bridge import BrokerMarketDataBridge

# Upstox's own documented ltpc_combined_limit, which is what the
# unresolved_broker_update_hold_limit setting carries in the running spine.
HELD_INSTRUMENT_LIMIT = 2000

NIFTY_UNDERLYING = InstrumentListing(
    instrument_key="NSE_INDEX|Nifty 50", exchange="NSE", segment="NSE_INDEX",
    instrument_type="INDEX", trading_symbol="NIFTY", lot_size=None, tick_size=None,
    freeze_quantity=None, expiry_ms=None, strike_price=None, underlying_key=None,
    intraday_margin_percent=None, intraday_leverage=None,
)
NIFTY_CALL = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)


def _bridge(held_instrument_limit=HELD_INSTRUMENT_LIMIT):
    return BrokerMarketDataBridge(held_instrument_limit=held_instrument_limit)


def _ltp(instrument_key, price, quantity=10.0, ltt_ms=1_740_000_000_000):
    return LtpUpdate(
        instrument_key=instrument_key, last_traded_price=price,
        last_traded_quantity=quantity, last_traded_time_ms=ltt_ms,
        close_price=None, broker_time_ns=ltt_ms * 1_000_000,
    )


def test_trade_for_is_none_before_the_listing_resolves():
    bridge = _bridge()
    assert bridge.trade_for(_ltp("NSE_FO|1001", 220.0)) is None


def test_builds_a_real_normalised_trade_readable_by_the_existing_consumers():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    trade = bridge.trade_for(_ltp("NSE_FO|1001", 220.0, quantity=75.0, ltt_ms=1_740_000_000_000))
    assert trade is not None
    assert trade.venue_id == "upstox"
    assert trade.symbol == "NIFTY 24500 CE"
    assert trade.price == 220.0
    assert trade.quantity == 75.0
    assert trade.venue_time_ns == 1_740_000_000_000 * 1_000_000
    assert trade.fidelity == TradeFidelity.LAST_TRADED_PRICE_ONLY


def test_side_is_none_not_fabricated():
    """Upstox's LTP ticker has no aggressor to report -- never guessed."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_UNDERLYING)
    trade = bridge.trade_for(_ltp("NSE_INDEX|Nifty 50", 24500.0))
    assert trade.side is None
    assert trade.signed_quantity is None


def test_sequence_uses_the_project_s_own_not_sent_sentinel():
    """The same convention runtime/venues/binance_usdm.py already uses when
    a venue genuinely sends no sequence number -- not a new one invented here."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    trade = bridge.trade_for(_ltp("NSE_FO|1001", 220.0))
    assert trade.sequence == NOT_SENT


def test_an_update_with_no_quantity_is_bridged_with_quantity_none():
    """Measured on the live tape of Friday 2026-09-04: 75.1% of Upstox LTP
    updates carry no last_traded_quantity, and 100% of NSE_INDEX ones do not.
    Dropping them, which is what this bridge used to do, meant no index price
    could reach `market-data` at all. The size is None -- not 0.0, which would
    claim a print in which nothing changed hands -- and the price still
    travels, which is what 26 consumers of this type actually read."""
    bridge = _bridge()
    bridge.observe_listing(NIFTY_UNDERLYING)
    update = LtpUpdate(
        instrument_key="NSE_INDEX|Nifty 50", last_traded_price=24500.0,
        last_traded_quantity=None, last_traded_time_ms=1_740_000_000_000,
        close_price=None, broker_time_ns=1_740_000_000_000 * 1_000_000,
    )
    trade = bridge.trade_for(update)
    assert trade is not None
    assert trade.price == 24500.0
    assert trade.quantity is None
    assert trade.quote_volume is None
    assert trade.signed_quantity is None


def test_an_unresolved_update_is_held_and_released_when_its_listing_arrives():
    """The defect this closes, measured on the live spine 2026-09-06: the feed
    delivers its whole snapshot on connect while the listings arrive on a 300 s
    restatement conveyor, so 2,000 updates met an empty map and every one was
    dropped -- 0 market-data published against instruments_resolved 2,000."""
    bridge = _bridge()
    assert bridge.trade_for(_ltp("NSE_FO|1001", 220.0, quantity=75.0)) is None
    assert bridge.trades_now_resolvable() == ()

    bridge.observe_listing(NIFTY_CALL)
    released = bridge.trades_now_resolvable()
    assert len(released) == 1
    assert released[0].symbol == "NIFTY 24500 CE"
    assert released[0].price == 220.0
    # Released once, not on every later tick.
    assert bridge.trades_now_resolvable() == ()


def test_only_the_newest_held_update_per_instrument_is_released():
    """An LTP is a level: the older price it replaced was never a print anyone
    missed, and releasing both would put a stale price on the wire as if new."""
    bridge = _bridge()
    bridge.trade_for(_ltp("NSE_FO|1001", 220.0))
    bridge.trade_for(_ltp("NSE_FO|1001", 231.5))
    bridge.observe_listing(NIFTY_CALL)
    released = bridge.trades_now_resolvable()
    assert [trade.price for trade in released] == [231.5]


def test_holding_is_bounded_so_an_unlistable_instrument_cannot_leak():
    bridge = _bridge(held_instrument_limit=2)
    for token in range(5):
        bridge.trade_for(_ltp(f"NSE_FO|{token}", 100.0 + token))
    standing = bridge._awaiting_listing.describe()
    assert standing["updates_awaiting_listing"] == 2.0
    assert standing["instruments_dropped_at_hold_limit"] == 3.0


def test_an_unresolved_instrument_is_ignored_not_raised():
    bridge = _bridge()
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.trade_for(_ltp("NSE_FO|9999", 100.0)) is None


def test_a_released_update_never_lands_behind_this_tick_s_fresher_price():
    """The order the tick publishes in is a correctness property, not a
    detail: a held update is older than one that arrived this tick, and
    everything that keeps a last price would end up keeping the stale one."""
    bridge = _bridge()
    bridge.trade_for(_ltp("NSE_FO|1001", 220.0))       # held, no listing yet
    bridge.observe_listing(NIFTY_CALL)
    published = bridge.trades_from([_ltp("NSE_FO|1001", 244.0)])
    assert [trade.price for trade in published] == [220.0, 244.0]
