"""Tests against real Upstox instrument-master values (assets.upstox.com/
market-quote/instruments/exchange/complete.json.gz, fetched 2026-09-01) and
the real InstrumentListing/LtpUpdate shapes already verified this session.
Never an invented shape (RL-063)."""

from runtime.brokers.broker_adapter import InstrumentListing, LtpUpdate
from runtime.tape import NOT_SENT, TradeFidelity
from parts.market_data_feed.broker_market_data_bridge import BrokerMarketDataBridge

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


def _ltp(instrument_key, price, quantity=10.0, ltt_ms=1_740_000_000_000):
    return LtpUpdate(
        instrument_key=instrument_key, last_traded_price=price,
        last_traded_quantity=quantity, last_traded_time_ms=ltt_ms,
        close_price=None, broker_time_ns=ltt_ms * 1_000_000,
    )


def test_trade_for_is_none_before_the_listing_resolves():
    bridge = BrokerMarketDataBridge()
    assert bridge.trade_for(_ltp("NSE_FO|1001", 220.0)) is None


def test_builds_a_real_normalised_trade_readable_by_the_existing_consumers():
    bridge = BrokerMarketDataBridge()
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
    bridge = BrokerMarketDataBridge()
    bridge.observe_listing(NIFTY_UNDERLYING)
    trade = bridge.trade_for(_ltp("NSE_INDEX|Nifty 50", 24500.0))
    assert trade.side is None
    assert trade.signed_quantity is None


def test_sequence_uses_the_project_s_own_not_sent_sentinel():
    """The same convention runtime/venues/binance_usdm.py already uses when
    a venue genuinely sends no sequence number -- not a new one invented here."""
    bridge = BrokerMarketDataBridge()
    bridge.observe_listing(NIFTY_CALL)
    trade = bridge.trade_for(_ltp("NSE_FO|1001", 220.0))
    assert trade.sequence == NOT_SENT


def test_an_update_with_no_quantity_is_not_bridged():
    """Real consumers (usdt_pnl_accountant, peak_excursion_tracker, ...) read
    quantity for real; a fabricated 0.0 would claim zero volume traded,
    which is a fact about the market, not about a missing field."""
    bridge = BrokerMarketDataBridge()
    bridge.observe_listing(NIFTY_CALL)
    update = LtpUpdate(
        instrument_key="NSE_FO|1001", last_traded_price=220.0,
        last_traded_quantity=None, last_traded_time_ms=1_740_000_000_000,
        close_price=None, broker_time_ns=1_740_000_000_000 * 1_000_000,
    )
    assert bridge.trade_for(update) is None


def test_an_unresolved_instrument_is_ignored_not_raised():
    bridge = BrokerMarketDataBridge()
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.trade_for(_ltp("NSE_FO|9999", 100.0)) is None
