"""Tests against real Upstox values: the interval codes ("I1" = 1minute,
"1d" = daily) and the OHLC field shapes come from upstox.com/developer/
api-documentation/v3/get-market-data-feed (fetched 2026-09-02) and the
already-verified InstrumentListing/BrokerCandle shapes. Never an invented
shape (RL-063)."""

from runtime.brokers.broker_adapter import BrokerCandle, InstrumentListing
from parts.market_data_feed.broker_candle_bridge import BrokerCandleBridge

NIFTY_CALL = InstrumentListing(
    instrument_key="NSE_FO|1001", exchange="NSE", segment="NSE_FO",
    instrument_type="CE", trading_symbol="NIFTY 24500 CE", lot_size=75,
    tick_size=0.05, freeze_quantity=1800.0, expiry_ms=1_740_100_000_000,
    strike_price=24500.0, underlying_key="NSE_INDEX|Nifty 50",
    intraday_margin_percent=None, intraday_leverage=None,
)

ONE_MINUTE_NS = 60_000_000_000
ONE_DAY_NS = 86_400_000_000_000


def _bar(instrument_key="NSE_FO|1001", interval="I1", bar_time_ms=1_740_000_000_000, is_closed=None):
    return BrokerCandle(
        instrument_key=instrument_key, interval=interval,
        open=220.0, high=225.0, low=218.0, close=222.0, volume=1500.0,
        bar_time_ms=bar_time_ms, is_closed=is_closed,
    )


def test_candle_for_is_none_before_the_listing_resolves():
    bridge = BrokerCandleBridge(wanted_interval="I1")
    assert bridge.candle_for(_bar(), now_ns=1_740_000_000_000 * 1_000_000) is None


def test_an_unresolved_instrument_is_ignored_not_raised():
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.candle_for(_bar(instrument_key="NSE_FO|9999"), now_ns=0) is None


def test_a_bar_at_a_different_interval_than_configured_is_ignored_not_mixed_in():
    """Real bug, 2026-09-02: market.marketOHLC.ohlc is a repeated field -- one
    real feed message bundles a 1-minute bar and a daily bar together, and
    this bridge used to republish both onto the shared `candle` wire.
    kline-window-builder keys its per-symbol candle list by (venue_id,
    symbol) alone with no interval check, so the daily bar corrupted a
    window built from 1-minute bars. Configuring the bridge for one interval
    and filtering the rest is what stops that."""
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.candle_for(_bar(interval="1d"), now_ns=0) is None


def test_builds_a_real_normalised_candle_readable_by_kline_window_builder():
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    now_ns = 1_740_000_000_000 * 1_000_000 + ONE_MINUTE_NS
    candle = bridge.candle_for(_bar(), now_ns=now_ns)
    assert candle is not None
    assert candle.venue_id == "upstox"
    assert candle.symbol == "NIFTY 24500 CE"
    assert candle.interval == "I1"
    assert candle.open_time_ns == 1_740_000_000_000 * 1_000_000
    assert candle.open == 220.0
    assert candle.high == 225.0
    assert candle.low == 218.0
    assert candle.close == 222.0
    assert candle.volume == 1500.0


def test_i1_parses_as_one_minute_for_closure_timing():
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    open_ns = 1_740_000_000_000 * 1_000_000
    assert bridge.candle_for(_bar(bar_time_ms=1_740_000_000_000), now_ns=open_ns).close_time_ns == open_ns + ONE_MINUTE_NS


def test_1d_parses_as_one_day_for_closure_timing():
    bridge = BrokerCandleBridge(wanted_interval="1d")
    bridge.observe_listing(NIFTY_CALL)
    open_ns = 1_740_000_000_000 * 1_000_000
    candle = bridge.candle_for(_bar(interval="1d", bar_time_ms=1_740_000_000_000), now_ns=open_ns)
    assert candle.close_time_ns == open_ns + ONE_DAY_NS


def test_an_unrecognised_interval_is_refused_not_guessed():
    bridge = BrokerCandleBridge(wanted_interval="1w")
    bridge.observe_listing(NIFTY_CALL)
    assert bridge.candle_for(_bar(interval="1w"), now_ns=0) is None


def test_is_closed_is_false_while_wall_clock_has_not_reached_the_bar_s_close():
    """Upstox restates the forming bar on every tick and carries no closed
    flag (unlike Binance's k.x / Bybit's confirm) -- inferred from elapsed
    wall time instead, the only fact this venue actually gives."""
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    open_ns = 1_740_000_000_000 * 1_000_000
    still_forming = bridge.candle_for(_bar(bar_time_ms=1_740_000_000_000), now_ns=open_ns + 30_000_000_000)
    assert still_forming.is_closed is False


def test_is_closed_is_true_once_wall_clock_passes_the_bar_s_close():
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    open_ns = 1_740_000_000_000 * 1_000_000
    finished = bridge.candle_for(_bar(bar_time_ms=1_740_000_000_000), now_ns=open_ns + ONE_MINUTE_NS + 1)
    assert finished.is_closed is True


def test_quote_volume_is_the_documented_close_times_volume_approximation():
    """Upstox's OHLC entry carries no per-bar turnover figure at all -- close
    * volume is a stated approximation (RL-061), never treated as the real
    sum of price*quantity a venue that does report turnover would give."""
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    candle = bridge.candle_for(_bar(), now_ns=0)
    assert candle.quote_volume == 222.0 * 1500.0


def test_trades_is_none_not_fabricated_zero():
    """Same shape as Bybit's own kline stream (runtime/venues/venue_adapter.py's
    NormalisedCandle.trades docstring): a venue that sends no count gets None,
    never a 0 that would read as 'nothing traded'."""
    bridge = BrokerCandleBridge(wanted_interval="I1")
    bridge.observe_listing(NIFTY_CALL)
    candle = bridge.candle_for(_bar(), now_ns=0)
    assert candle.trades is None
