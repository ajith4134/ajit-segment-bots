from parts.broker_adapter.broker_market_tape_writer import (
    PART_DECLARATION,
    observe_subscribed_listing,
    stream_kind_for,
    symbol_for,
)
from runtime.brokers.broker_adapter import (
    BrokerCandle, BrokerOpenInterest, BrokerOptionGreeks,
    BrokerOrderBookUpdate, LtpUpdate,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.tape import StreamKind


def a_listing(instrument_key: str, trading_symbol: str):
    """A real-shaped InstrumentListing, built the way every other test in this
    project builds one -- through the adapter's own row reader -- rather than
    by hand-filling all thirteen fields, most of which this test does not care
    about."""
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    rows = [{
        "segment": "NSE_FO", "name": "TEST", "exchange": "NSE",
        "instrument_type": "CE", "instrument_key": instrument_key,
        "lot_size": 50, "tick_size": 0.05, "trading_symbol": trading_symbol,
        "expiry": 1788892199000, "strike_price": 100.0,
        "underlying_key": "NSE_INDEX|Nifty 50",
    }]
    return UpstoxAdapter.read_instrument_listings(adapter, rows)[0]


def test_stream_kind_for_each_broker_record_type():
    assert stream_kind_for(LtpUpdate) == StreamKind.TRADE
    assert stream_kind_for(BrokerCandle) == StreamKind.CANDLE
    assert stream_kind_for(BrokerOrderBookUpdate) == StreamKind.BOOK
    assert stream_kind_for(BrokerOpenInterest) == StreamKind.OPEN_INTEREST
    assert stream_kind_for(BrokerOptionGreeks) == StreamKind.OPTION_GREEKS


def test_payload_for_json_encodes_the_record():
    import json
    from parts.broker_adapter.broker_market_tape_writer import _payload_for

    update = LtpUpdate(
        instrument_key="NSE_EQ|INE002A01018", last_traded_price=1234.5,
        last_traded_quantity=10.0, last_traded_time_ms=1740729552723,
        close_price=1230.0, broker_time_ns=1740729566039_000_000,
    )
    decoded = json.loads(_payload_for(update))
    assert decoded["instrument_key"] == "NSE_EQ|INE002A01018"
    assert decoded["last_traded_price"] == 1234.5


def test_venue_time_ns_of_reads_broker_time_ns_directly_for_non_candle_kinds():
    from parts.broker_adapter.broker_market_tape_writer import venue_time_ns_of

    update = LtpUpdate(
        instrument_key="NSE_EQ|INE002A01018", last_traded_price=1234.5,
        last_traded_quantity=10.0, last_traded_time_ms=1740729552723,
        close_price=1230.0, broker_time_ns=1740729566039_000_000,
    )
    assert venue_time_ns_of(update) == 1740729566039_000_000


def test_symbol_for_falls_back_to_the_instrument_key_when_unresolved():
    """A tick that arrives before its listing is known must still be written,
    just unlabelled -- never dropped."""
    assert symbol_for({}, "NSE_FO|56316") == "NSE_FO|56316"


def test_observe_subscribed_listing_then_symbol_for_resolves_the_real_name():
    """Real incident, 2026-09-08: broker-market-tape-writer wrote every Upstox
    tick under `instrument_key` (e.g. NSE_FO|56316), but every reader of the
    tape -- dashboard/build_trade_board.py's read_last_price, called with a
    Position's own `symbol` -- looks the file up by trading_symbol. Ten open
    stock-options positions ticked every second and showed NOT MEASURED on
    the board for it. This is the resolution that closes that gap.
    """
    symbol_by_key: dict[str, str] = {}
    listing = a_listing("NSE_FO|56316", "AXISBANK 1260 CE 29 SEP 26")

    observe_subscribed_listing(symbol_by_key, listing)

    assert symbol_for(symbol_by_key, "NSE_FO|56316") == "AXISBANK 1260 CE 29 SEP 26"


def test_the_part_now_consumes_broker_subscribed_instrument_listing():
    assert "broker-subscribed-instrument-listing" in PART_DECLARATION.consumes


def test_venue_time_ns_of_converts_broker_candle_bar_time_ms_to_ns():
    """Real bug, 2026-09-02: write_one() read `record.broker_time_ns`
    unconditionally, but BrokerCandle is the one record kind that carries no
    such field -- only `bar_time_ms`, milliseconds, off Upstox's own OHLC
    `ts`. AttributeError the first time a real candle ever reached the tape
    writer, once the subscribe-mode fix let real feed data arrive at all."""
    from parts.broker_adapter.broker_market_tape_writer import venue_time_ns_of

    candle = BrokerCandle(
        instrument_key="NSE_EQ|INE002A01018", interval="1minute",
        open=100.0, high=101.0, low=99.0, close=100.5, volume=500.0,
        bar_time_ms=1740729552723, is_closed=None,
    )
    assert venue_time_ns_of(candle) == 1740729552723_000_000
