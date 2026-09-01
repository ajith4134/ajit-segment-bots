"""Upstox's real Equity Options charge stack -- flat brokerage plus five
percentage-of-premium statutory/exchange charges, not one blended rate.

Source: https://upstox.com/brokerage-charges/, scraped 2026-09-01 (post the
October 2024 STT/CTT and exchange-transaction-charge revision, the current
one on the page). Direct quotes, per component:

  brokerage                    "Flat Rs20 per executed order."
  STT (sell side, on premium)  "0.1% on sell side (on premium)"
  exchange txn charge (NSE)    "0.03503% (on premium)"
  IPFT charge (NSE)            "Rs0.50 per lakh of the turnover (Premium value)"
  stamp duty (buy side)        "0.003% or Rs300 / crore on buy side"
  GST                          "18% (on brokerage + transaction charges + IPFT charges)"

Crypto's taker_fee_rate/maker_fee_rate is one blended percentage because
Binance/Bybit each publish one; Upstox does not -- collapsing these six
lines into a single number would be a fabricated blend, not a sourced one
(RL-061: every number is a setting with provenance, and the provenance here
is six separate published rates, not one).
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.trading_types import BUY, SELL


@dataclass(frozen=True)
class OptionsChargeBreakdown:
    """One executed options order's real Upstox charge stack, itemised so
    each component's provenance stays traceable rather than folded into
    one number nobody can check against the source page again."""

    brokerage: float
    stt: float
    exchange_transaction_charge: float
    ipft_charge: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return (
            self.brokerage + self.stt + self.exchange_transaction_charge
            + self.ipft_charge + self.stamp_duty + self.gst
        )


def upstox_options_order_cost(
    premium_value: float, side: str,
    flat_brokerage: float, stt_sell_rate: float,
    exchange_transaction_charge_rate: float, ipft_charge_rate: float,
    stamp_duty_buy_rate: float, gst_rate: float,
) -> OptionsChargeBreakdown:
    """One executed order's real Upstox options charge stack.

    premium_value is price * quantity for this fill -- every percentage
    charge below is on premium, never on the underlying's notional (the
    source page's options section states its own rates distinct from the
    futures section's). STT applies sell-side only, stamp duty buy-side
    only (both stated as such on the source page) -- one order incurs
    exactly one of the two, never both, matching how a real broker's
    contract note reads.
    """
    if premium_value < 0:
        raise ValueError(f"premium_value must be >= 0, got {premium_value!r}")
    if side not in (BUY, SELL):
        raise ValueError(f"side must be {BUY!r} or {SELL!r}, got {side!r}")

    stt = premium_value * stt_sell_rate if side == SELL else 0.0
    stamp_duty = premium_value * stamp_duty_buy_rate if side == BUY else 0.0
    exchange_transaction_charge = premium_value * exchange_transaction_charge_rate
    ipft_charge = premium_value * ipft_charge_rate
    gst = gst_rate * (flat_brokerage + exchange_transaction_charge + ipft_charge)

    return OptionsChargeBreakdown(
        brokerage=flat_brokerage, stt=stt,
        exchange_transaction_charge=exchange_transaction_charge,
        ipft_charge=ipft_charge, stamp_duty=stamp_duty, gst=gst,
    )


__all__ = ["OptionsChargeBreakdown", "upstox_options_order_cost"]
