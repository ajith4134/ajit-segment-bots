from parts.broker_adapter.broker_market_tape_writer import stream_kind_for
from runtime.brokers.broker_adapter import (
    BrokerCandle, BrokerOpenInterest, BrokerOptionGreeks,
    BrokerOrderBookUpdate, LtpUpdate,
)
from runtime.tape import StreamKind


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
