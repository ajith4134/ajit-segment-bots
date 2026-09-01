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
