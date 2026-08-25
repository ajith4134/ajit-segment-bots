"""A part that wants trades gets trades, whatever else is on the wire.

`market-data` carries trades from `venue-trade-stream-reader` and candles from
`ccxt-venue-reader`. Every reader written before the candle reader ran assumed the
stream held only trades, and on 2026-08-25 `peak-excursion-tracker` crash-looped
857 times on `AttributeError: 'NormalisedCandle' object has no attribute 'price'`
-- the part whose whole output is the extreme a position reached, which is what
stop placement is learned from.

The rule was already written in `runtime/price_frames.levels_in` and not followed
here: a part that dies on an unexpected shape is a part the wiring can kill.
"""

from __future__ import annotations

from runtime.market_data_stream import candles_in, trades_in
from runtime.venues.venue_adapter import NormalisedCandle, NormalisedTrade

MINUTE_NS = 60 * 1_000_000_000


def a_trade(symbol="BTCUSDT", price=80_000.0) -> NormalisedTrade:
    from runtime.venues.venue_adapter import TradeFidelity

    return NormalisedTrade(
        venue_id="binance-usdm", symbol=symbol, price=price, quantity=1.0,
        side="buy", venue_time_ns=MINUTE_NS, sequence=1,
        fidelity=TradeFidelity.EVERY_PRINT,
    )


def a_candle(symbol="BTCUSDT") -> NormalisedCandle:
    return NormalisedCandle(
        venue_id="binance-usdm", symbol=symbol, interval="1m",
        open_time_ns=MINUTE_NS, close_time_ns=MINUTE_NS * 2,
        open=1.0, high=2.0, low=0.5, close=1.5,
        volume=10.0, quote_volume=15.0, trades=3, is_closed=True,
        venue_time_ns=MINUTE_NS,
    )


def test_trades_in_keeps_only_trades():
    mixed = [a_trade(), a_candle(), a_trade(price=81_000.0), a_candle()]
    assert [t.price for t in trades_in(mixed)] == [80_000.0, 81_000.0]


def test_candles_in_keeps_only_candles():
    mixed = [a_trade(), a_candle(), a_trade(), a_candle()]
    assert len(list(candles_in(mixed))) == 2


def test_the_two_halves_do_not_overlap():
    """Nothing is both, so no part can double-count the stream."""
    mixed = [a_trade(), a_candle()]
    trades = set(id(item) for item in trades_in(mixed))
    candles = set(id(item) for item in candles_in(mixed))
    assert not (trades & candles)
    assert len(trades) + len(candles) == len(mixed)


def test_a_candle_alone_yields_no_trades_rather_than_raising():
    """The exact shape that crash-looped the excursion tracker 857 times."""
    assert list(trades_in([a_candle()])) == []


def test_an_unrecognised_shape_is_skipped_not_raised_on():
    """An inbox carries what the wiring delivers, not what a reader hoped for."""
    class Something:
        pass

    assert list(trades_in([Something(), a_trade(), None])) != []
    assert len(list(trades_in([Something(), a_trade(), None]))) == 1


def test_an_empty_batch_is_empty_rather_than_an_error():
    assert list(trades_in([])) == []
    assert list(candles_in([])) == []
