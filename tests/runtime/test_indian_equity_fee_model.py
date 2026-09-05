"""Upstox's Equity Intraday charge stack is not the options stack.

The defect this guards: `paper-fill-simulator` charged
`upstox_options_order_cost` to every Upstox fill regardless of instrument, so a
cash-equity intraday trade paid 0.1% STT on premium where it owes 0.025% on
turnover, and a 0.03503% exchange transaction charge where it owes 0.00297%.
Measured on the 2026-09-04 tape: a MARUTI round trip of 3 shares cost 118.79
under the options stack and 60.78 under the real one, twice the true cost.

Rates are the operator's settings, read here the way the live part reads them,
so a test that passes against invented numbers cannot pass against the file.
"""

from __future__ import annotations

import pytest

from runtime.indian_equity_fee_model import upstox_equity_intraday_order_cost
from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.trading_types import BUY, SELL


def rates() -> dict:
    """The equity rates as the operator's own settings state them."""
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    read = lambda name: float(document.read_value(name))  # noqa: E731
    return {
        "flat_brokerage": read("equity_intraday_flat_brokerage"),
        "brokerage_rate": read("equity_intraday_brokerage_rate"),
        "stt_sell_rate": read("equity_intraday_stt_sell_rate"),
        "exchange_transaction_charge_rate": read(
            "equity_intraday_exchange_transaction_charge_rate"
        ),
        "ipft_charge_rate": read("equity_intraday_ipft_charge_rate"),
        "stamp_duty_buy_rate": read("equity_intraday_stamp_duty_buy_rate"),
        "sebi_charge_rate": read("equity_intraday_sebi_charge_rate"),
        "gst_rate": read("equity_intraday_gst_rate"),
    }


def test_brokerage_is_the_lower_of_the_flat_and_the_percentage():
    """'Rs20 per executed order or 0.1% (whichever is lower)' -- a minimum.

    The single most likely way to get this wrong is to copy the options model,
    where brokerage is genuinely flat. On a small order that overstates the
    charge fourfold.
    """
    small = upstox_equity_intraday_order_cost(5_000.0, BUY, **rates())
    large = upstox_equity_intraday_order_cost(50_000.0, BUY, **rates())

    assert small.brokerage == pytest.approx(5.0), "0.1% of 5,000 is the lower arm"
    assert large.brokerage == pytest.approx(20.0), "the flat Rs20 is the lower arm"


def test_stt_is_charged_on_the_sell_leg_only():
    assert upstox_equity_intraday_order_cost(100_000.0, BUY, **rates()).stt == 0.0
    assert upstox_equity_intraday_order_cost(
        100_000.0, SELL, **rates()
    ).stt == pytest.approx(25.0), "0.025% of 100,000"


def test_stamp_duty_is_charged_on_the_buy_leg_only():
    assert upstox_equity_intraday_order_cost(
        100_000.0, BUY, **rates()
    ).stamp_duty == pytest.approx(3.0), "0.003% of 100,000"
    assert upstox_equity_intraday_order_cost(100_000.0, SELL, **rates()).stamp_duty == 0.0


def test_gst_is_charged_on_brokerage_transaction_and_ipft_and_nothing_else():
    """STT, stamp duty and the SEBI charge are outside the GST base."""
    charge = upstox_equity_intraday_order_cost(100_000.0, SELL, **rates())
    taxable = charge.brokerage + charge.exchange_transaction_charge + charge.ipft_charge

    assert charge.gst == pytest.approx(0.18 * taxable)
    assert charge.gst < 0.18 * (taxable + charge.stt), "STT must not be in the base"


def test_the_total_is_every_component_and_nothing_more():
    charge = upstox_equity_intraday_order_cost(250_000.0, SELL, **rates())

    assert charge.total == pytest.approx(
        charge.brokerage + charge.stt + charge.exchange_transaction_charge
        + charge.ipft_charge + charge.stamp_duty + charge.sebi_charge + charge.gst
    )


def test_the_options_stack_costs_an_equity_round_trip_twice_what_it_owes():
    """The measured defect, kept as a test so it cannot come back quietly.

    Real numbers from the 2026-09-04 tape: MARUTI at 12,785.00 in and 12,768.00
    out, 3 shares -- what `cash-equity-intraday` actually traded in the replay.
    """
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    read = lambda name: float(document.read_value(name))  # noqa: E731
    options = {
        "flat_brokerage": read("options_flat_brokerage"),
        "stt_sell_rate": read("options_stt_sell_rate"),
        "exchange_transaction_charge_rate": read(
            "options_exchange_transaction_charge_rate"
        ),
        "ipft_charge_rate": read("options_ipft_charge_rate"),
        "stamp_duty_buy_rate": read("options_stamp_duty_buy_rate"),
        "gst_rate": read("options_gst_rate"),
    }
    bought, sold = 3 * 12_785.0, 3 * 12_768.0

    correct = (
        upstox_equity_intraday_order_cost(bought, BUY, **rates()).total
        + upstox_equity_intraday_order_cost(sold, SELL, **rates()).total
    )
    wrong = (
        upstox_options_order_cost(bought, BUY, **options).total
        + upstox_options_order_cost(sold, SELL, **options).total
    )

    assert correct == pytest.approx(60.78, abs=0.01)
    assert wrong > 1.9 * correct, (
        f"the options stack charges {wrong:,.2f} for an equity round trip that "
        f"really costs {correct:,.2f}; if this ratio has moved, a rate changed"
    )


def test_a_negative_turnover_is_refused_rather_than_priced():
    with pytest.raises(ValueError):
        upstox_equity_intraday_order_cost(-1.0, BUY, **rates())


def test_a_side_that_is_neither_buy_nor_sell_is_refused():
    """Neither leg's charges apply, so pricing it would invent a number."""
    with pytest.raises(ValueError):
        upstox_equity_intraday_order_cost(1_000.0, "sideways", **rates())
