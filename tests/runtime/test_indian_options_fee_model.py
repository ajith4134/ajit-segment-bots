"""Tests against Upstox's real published Equity Options charge stack
(https://upstox.com/brokerage-charges/, scraped 2026-09-01 -- see
runtime/indian_options_fee_model.py's own docstring for the source quote
per line). Real rates, not invented fixtures (RL-063)."""

import pytest

from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.trading_types import BUY, SELL

FLAT_BROKERAGE = 20.0
STT_SELL_RATE = 0.001
EXCHANGE_TRANSACTION_CHARGE_RATE = 0.0003503
IPFT_CHARGE_RATE = 0.000005
STAMP_DUTY_BUY_RATE = 0.00003
GST_RATE = 0.18


def a_cost(premium_value, side):
    return upstox_options_order_cost(
        premium_value, side,
        flat_brokerage=FLAT_BROKERAGE, stt_sell_rate=STT_SELL_RATE,
        exchange_transaction_charge_rate=EXCHANGE_TRANSACTION_CHARGE_RATE,
        ipft_charge_rate=IPFT_CHARGE_RATE, stamp_duty_buy_rate=STAMP_DUTY_BUY_RATE,
        gst_rate=GST_RATE,
    )


def test_brokerage_is_flat_regardless_of_premium_value():
    small = a_cost(1_000.0, BUY)
    large = a_cost(1_000_000.0, BUY)
    assert small.brokerage == FLAT_BROKERAGE
    assert large.brokerage == FLAT_BROKERAGE


def test_stt_applies_only_on_the_sell_side():
    buy = a_cost(100_000.0, BUY)
    sell = a_cost(100_000.0, SELL)
    assert buy.stt == 0.0
    assert sell.stt == pytest.approx(100_000.0 * STT_SELL_RATE)


def test_stamp_duty_applies_only_on_the_buy_side():
    buy = a_cost(100_000.0, BUY)
    sell = a_cost(100_000.0, SELL)
    assert buy.stamp_duty == pytest.approx(100_000.0 * STAMP_DUTY_BUY_RATE)
    assert sell.stamp_duty == 0.0


def test_exchange_and_ipft_charges_apply_on_both_sides():
    buy = a_cost(100_000.0, BUY)
    sell = a_cost(100_000.0, SELL)
    expected_exchange = 100_000.0 * EXCHANGE_TRANSACTION_CHARGE_RATE
    expected_ipft = 100_000.0 * IPFT_CHARGE_RATE
    assert buy.exchange_transaction_charge == pytest.approx(expected_exchange)
    assert sell.exchange_transaction_charge == pytest.approx(expected_exchange)
    assert buy.ipft_charge == pytest.approx(expected_ipft)
    assert sell.ipft_charge == pytest.approx(expected_ipft)


def test_gst_is_charged_only_on_brokerage_exchange_and_ipft_never_on_stt_or_stamp_duty():
    sell = a_cost(100_000.0, SELL)
    expected_gst = GST_RATE * (
        FLAT_BROKERAGE
        + 100_000.0 * EXCHANGE_TRANSACTION_CHARGE_RATE
        + 100_000.0 * IPFT_CHARGE_RATE
    )
    assert sell.gst == pytest.approx(expected_gst)


def test_total_sums_every_component():
    cost = a_cost(100_000.0, SELL)
    assert cost.total == pytest.approx(
        cost.brokerage + cost.stt + cost.exchange_transaction_charge
        + cost.ipft_charge + cost.stamp_duty + cost.gst
    )


def test_zero_premium_value_charges_only_the_flat_brokerage_and_its_gst():
    cost = a_cost(0.0, BUY)
    assert cost.stt == 0.0
    assert cost.stamp_duty == 0.0
    assert cost.exchange_transaction_charge == 0.0
    assert cost.ipft_charge == 0.0
    assert cost.gst == pytest.approx(GST_RATE * FLAT_BROKERAGE)
    assert cost.total == pytest.approx(FLAT_BROKERAGE * (1 + GST_RATE))


def test_negative_premium_value_is_refused():
    with pytest.raises(ValueError):
        a_cost(-1.0, BUY)
