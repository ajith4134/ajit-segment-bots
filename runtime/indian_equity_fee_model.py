"""Upstox's real Equity Intraday charge stack -- six components on turnover,
none of them the options stack this project already had.

Source: https://upstox.com/brokerage-charges/, fetched 2026-09-05, reading the
**current** blocks on the page ("From 1st October 2024") and not the superseded
ones beside them. Direct quotes, per component:

  brokerage                    "Rs20 per executed order or 0.1% (whichever is lower)"
  STT (sell side)              "0.025% on the sell side"
  exchange txn charge (NSE)    "0.00297% per trade on buy & sell."
  IPFT charge (NSE)            "Rs0.10 per Lakh of the turnover"
  stamp duty (buy side)        "0.003% or Rs300 / crore on buy side"
  SEBI charges                 "Rs10/crore"
  GST                          "18% (on brokerage + transaction charges + IPFT charges)"

**This exists because charging the options stack to an equity trade is wrong in
both the rate and the base.** `paper-fill-simulator` charged
`upstox_options_order_cost` to every Upstox fill regardless of instrument, which
for a cash-equity intraday trade meant 0.1% sell-side STT where the real rate is
0.025%, and a 0.03503% exchange transaction charge where the real one is
0.00297% -- an order of magnitude on that component. Both are computed on the
premium for an option and on the turnover for a share, so it is not one number
being off: it is a different stack.

Two differences from the options model are the ones to keep in mind, because
they are exactly where a copy of it would have gone wrong:

- **Brokerage is a minimum, not a flat.** Options are "Flat Rs20 per executed
  order"; equity intraday is "Rs20 per executed order or 0.1% (whichever is
  lower)". On a Rs5,000 order that is Rs5, not Rs20 -- a quarter of the whole
  charge stack at that size. Collapsing it to a flat Rs20 would overstate the
  cost of every small trade and quietly make the segment's own
  `minimum_capital_per_trade` look better justified than it is.
- **SEBI charges are included here and are absent from the options model.** The
  page lists "Rs10/crore" against all four products, so the options stack is
  missing a component; that is noted rather than fixed here, because changing
  what an options fill costs is a separate change with its own evidence, and
  folding it into this one would make both unreviewable. At Rs10/crore it is
  0.0001% and does not change any conclusion either way.

RL-061: every rate is a named setting with provenance, and the provenance is
seven separately published lines, not one blended number. None of them is a
literal in decision code -- they arrive from the operator's settings, the same
way the options rates do.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.trading_types import BUY, SELL


@dataclass(frozen=True)
class EquityChargeBreakdown:
    """One executed equity intraday order's real Upstox charge stack, itemised
    so each component's provenance stays traceable rather than folded into one
    number nobody can check against the source page again."""

    brokerage: float
    stt: float
    exchange_transaction_charge: float
    ipft_charge: float
    stamp_duty: float
    sebi_charge: float
    gst: float

    @property
    def total(self) -> float:
        return (
            self.brokerage + self.stt + self.exchange_transaction_charge
            + self.ipft_charge + self.stamp_duty + self.sebi_charge + self.gst
        )


def upstox_equity_intraday_order_cost(
    turnover: float,
    side: str,
    *,
    flat_brokerage: float,
    brokerage_rate: float,
    stt_sell_rate: float,
    exchange_transaction_charge_rate: float,
    ipft_charge_rate: float,
    stamp_duty_buy_rate: float,
    sebi_charge_rate: float,
    gst_rate: float,
) -> EquityChargeBreakdown:
    """What one executed equity intraday order really costs, by component.

    `turnover` is quantity x price -- the traded value of the shares, which for
    equity is the base of every percentage component. (For an option the base is
    the premium, which is why the two models cannot share one function.)

    Side decides two components and only two: STT is charged on the sell leg and
    stamp duty on the buy leg. Everything else is charged on both, so a round
    trip pays each of those twice and each of the other two once.
    """
    if turnover < 0:
        raise ValueError("an order's turnover cannot be negative")
    if side not in (BUY, SELL):
        raise ValueError(f"{side!r} is neither {BUY!r} nor {SELL!r}")

    # "Rs20 per executed order or 0.1% (whichever is lower)" -- a minimum, not a
    # flat. This is the line that makes equity brokerage different from options
    # brokerage, and the one a copy of the options model would have got wrong.
    brokerage = min(flat_brokerage, turnover * brokerage_rate)

    stt = turnover * stt_sell_rate if side == SELL else 0.0
    stamp_duty = turnover * stamp_duty_buy_rate if side == BUY else 0.0
    exchange_transaction_charge = turnover * exchange_transaction_charge_rate
    ipft_charge = turnover * ipft_charge_rate
    sebi_charge = turnover * sebi_charge_rate

    # "18% (on brokerage + transaction charges + IPFT charges)" -- and on those
    # three only. STT, stamp duty and the SEBI charge are outside the GST base,
    # which is what the page says and is not an approximation to tidy up.
    gst = gst_rate * (brokerage + exchange_transaction_charge + ipft_charge)

    return EquityChargeBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_transaction_charge=exchange_transaction_charge,
        ipft_charge=ipft_charge,
        stamp_duty=stamp_duty,
        sebi_charge=sebi_charge,
        gst=gst,
    )
