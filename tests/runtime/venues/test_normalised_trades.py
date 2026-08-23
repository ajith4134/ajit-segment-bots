"""Normalisation on read: what the venues actually sent, in this project's terms.

`market-data` has 65 consumers in the blueprint. If each of them parsed a venue's
JSON, 65 parts would know what a venue looks like, and the two venues do not agree
on the one field that matters most for reading pressure: Binance says whether the
buyer was the maker, Bybit says which way the taker went. These tests hold the line
that exactly one place knows that.

Every payload here is a real frame captured from the live socket (RL-063).
"""

from __future__ import annotations

import json

import pytest

from runtime.tape import TradeFidelity
from runtime.trading_types import BUY, SELL
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import NormalisedTrade

BINANCE = "binance-usdm"
BYBIT = "bybit-linear"
BINANCE_TRADE_RUN = "2026-08-22-btcusdt-aggtrade-run.jsonl"
BINANCE_MIXED_RUN = "2026-08-22-market-ws-aggtrade-kline.jsonl"
BYBIT_TRADE_RUN = "2026-08-22-public-linear-trade.jsonl"
BYBIT_BOOK_RUN = "2026-08-22-public-linear-orderbook.jsonl"


def trades_from(adapter, records) -> list[NormalisedTrade]:
    return [trade for _received_at_ns, payload in records for trade in adapter.read_trades(payload)]


@pytest.mark.parametrize(
    "venue_id, capture, expected_fidelity",
    [
        (BINANCE, BINANCE_TRADE_RUN, TradeFidelity.VENUE_AGGREGATED),
        (BYBIT, BYBIT_TRADE_RUN, TradeFidelity.EVERY_PRINT),
    ],
)
def test_every_captured_trade_run_yields_trades(venue_id, capture, expected_fidelity, read_captured_payloads):
    adapter = load_venue_adapter(venue_id)
    trades = trades_from(adapter, read_captured_payloads(venue_id, capture))

    assert trades, f"{venue_id} sent trades and none were read"
    for trade in trades:
        assert trade.venue_id == venue_id
        assert trade.price > 0
        assert trade.quantity > 0
        assert trade.side in {BUY, SELL}
        assert trade.venue_time_ns > 0
        assert trade.fidelity is expected_fidelity


def test_fidelity_travels_with_the_trade_because_the_venues_mean_different_things(read_captured_payloads):
    """Binance has no raw trade stream at all; Bybit sends every print."""
    binance = trades_from(load_venue_adapter(BINANCE), read_captured_payloads(BINANCE, BINANCE_TRADE_RUN))
    bybit = trades_from(load_venue_adapter(BYBIT), read_captured_payloads(BYBIT, BYBIT_TRADE_RUN))

    assert {trade.fidelity for trade in binance} == {TradeFidelity.VENUE_AGGREGATED}
    assert {trade.fidelity for trade in bybit} == {TradeFidelity.EVERY_PRINT}


def test_binance_reports_the_aggressor_not_the_maker(read_captured_payloads):
    """`m` is whether the buyer was the maker, so m true means the seller aggressed."""
    adapter = load_venue_adapter(BINANCE)
    records = read_captured_payloads(BINANCE, BINANCE_TRADE_RUN)
    checked_both_sides = set()
    for _received_at_ns, payload in records:
        message = json.loads(payload)
        if message.get("e") != "aggTrade":
            continue
        (trade,) = adapter.read_trades(payload)
        expected = SELL if message["m"] else BUY
        assert trade.side == expected
        assert trade.price == float(message["p"])
        assert trade.quantity == float(message["q"])
        assert trade.sequence == int(message["a"])
        checked_both_sides.add(expected)
    assert checked_both_sides == {BUY, SELL}, (
        f"the capture only exercised {checked_both_sides}; the inversion is only proven by both"
    )


def test_bybit_reports_the_taker_side_it_already_sends(read_captured_payloads):
    adapter = load_venue_adapter(BYBIT)
    for _received_at_ns, payload in read_captured_payloads(BYBIT, BYBIT_TRADE_RUN):
        message = json.loads(payload)
        if not str(message.get("topic", "")).startswith("publicTrade."):
            continue
        read = adapter.read_trades(payload)
        assert len(read) == len(message["data"])
        for trade, raw in zip(read, message["data"], strict=True):
            assert trade.side == (BUY if raw["S"].lower() == "buy" else SELL)
            assert trade.price == float(raw["p"])
            assert trade.quantity == float(raw["v"])
            assert trade.sequence == int(raw["seq"])


def test_a_bybit_message_carrying_many_trades_yields_all_of_them(read_captured_payloads):
    """A signature returning one trade would have silently dropped the rest."""
    adapter = load_venue_adapter(BYBIT)
    biggest = 0
    for _received_at_ns, payload in read_captured_payloads(BYBIT, BYBIT_TRADE_RUN):
        biggest = max(biggest, len(adapter.read_trades(payload)))
    assert biggest > 1, "the capture holds no batched message, so batching is untested"


def test_each_bybit_trade_carries_its_own_time_not_the_messages(read_captured_payloads):
    """The tape indexes on the message's time; a consumer wants the trade's."""
    adapter = load_venue_adapter(BYBIT)
    differing = 0
    for _received_at_ns, payload in read_captured_payloads(BYBIT, BYBIT_TRADE_RUN):
        message = json.loads(payload)
        if not str(message.get("topic", "")).startswith("publicTrade."):
            continue
        message_time_ns = int(message["ts"]) * 1_000_000
        for trade in adapter.read_trades(payload):
            if trade.venue_time_ns != message_time_ns:
                differing += 1
    assert differing > 0, "every trade matched its message's timestamp, so nothing proves the difference"


@pytest.mark.parametrize(
    "venue_id, capture",
    [(BINANCE, BINANCE_MIXED_RUN), (BYBIT, BYBIT_BOOK_RUN)],
)
def test_a_message_that_is_not_a_trade_yields_no_trades(venue_id, capture, read_captured_payloads):
    """Control frames, candles and book updates all read as no trades, not as an error."""
    adapter = load_venue_adapter(venue_id)
    records = read_captured_payloads(venue_id, capture)
    non_trade = [
        payload
        for _received_at_ns, payload in records
        if not adapter.read_trades(payload)
    ]
    assert non_trade, f"{capture} was expected to hold messages that are not trades"


def test_signed_quantity_says_which_way_the_aggressor_went(read_captured_payloads):
    adapter = load_venue_adapter(BYBIT)
    trades = trades_from(adapter, read_captured_payloads(BYBIT, BYBIT_TRADE_RUN))
    buys = [trade for trade in trades if trade.side == BUY]
    sells = [trade for trade in trades if trade.side == SELL]

    assert buys and sells, "the capture must hold both directions for this to prove anything"
    assert all(trade.signed_quantity > 0 for trade in buys)
    assert all(trade.signed_quantity < 0 for trade in sells)
    assert all(
        trade.quote_volume == pytest.approx(trade.price * trade.quantity) for trade in trades
    )


# ---- candles, normalised on read the same way ---------------------------------

BINANCE_KLINE_RUN = "2026-08-22-market-ws-kline-through-close.jsonl"
BYBIT_KLINE_RUN = "2026-08-22-public-linear-kline-through-close.jsonl"


def candles_from(adapter, records):
    return [candle for _received_at_ns, payload in records for candle in adapter.read_candles(payload)]


@pytest.mark.parametrize(
    "venue_id, capture",
    [(BINANCE, BINANCE_KLINE_RUN), (BYBIT, BYBIT_KLINE_RUN)],
)
def test_a_captured_kline_run_yields_candles_and_exactly_the_closes_the_venue_flagged(
    venue_id, capture, read_captured_payloads
):
    """Both runs were captured through a minute boundary, so each carries one
    close. The closed flag is the one field the candle reader exists to keep."""
    adapter = load_venue_adapter(venue_id)
    candles = candles_from(adapter, read_captured_payloads(venue_id, capture))
    assert candles
    assert all(candle.venue_id == venue_id and candle.symbol == "BTCUSDT" for candle in candles)
    assert all(candle.high >= max(candle.open, candle.close) >= min(candle.open, candle.close) >= candle.low > 0 for candle in candles)
    assert all(candle.open_time_ns < candle.close_time_ns for candle in candles)
    assert sum(candle.is_closed for candle in candles) == 1


def test_bybit_packs_the_close_and_the_next_open_into_one_message(read_captured_payloads):
    adapter = load_venue_adapter(BYBIT)
    per_message = [
        adapter.read_candles(payload) for _t, payload in read_captured_payloads(BYBIT, BYBIT_KLINE_RUN)
    ]
    two = next(batch for batch in per_message if len(batch) == 2)
    assert two[0].is_closed and not two[1].is_closed
    assert two[1].open_time_ns == two[0].close_time_ns + 1_000_000, "the next minute starts where this one ends"


def test_bybit_sends_no_trade_count_and_that_is_none_not_zero(read_captured_payloads):
    adapter = load_venue_adapter(BYBIT)
    assert all(c.trades is None for c in candles_from(adapter, read_captured_payloads(BYBIT, BYBIT_KLINE_RUN)))
    binance = load_venue_adapter(BINANCE)
    assert all(c.trades is not None for c in candles_from(binance, read_captured_payloads(BINANCE, BINANCE_KLINE_RUN)))


@pytest.mark.parametrize(
    "venue_id, capture",
    [(BINANCE, BINANCE_TRADE_RUN), (BYBIT, BYBIT_TRADE_RUN)],
)
def test_a_message_that_is_not_a_candle_yields_no_candles(venue_id, capture, read_captured_payloads):
    adapter = load_venue_adapter(venue_id)
    assert candles_from(adapter, read_captured_payloads(venue_id, capture)) == []
