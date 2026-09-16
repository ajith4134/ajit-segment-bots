"""An option's implied volatility, inverted from its premium.

Upstox states implied volatility only for what the live feed subscribed to, and only
live. A past session has premiums and no stated volatility, so volatility-gap-detector
and expiry-day-zero-to-hero-detector cannot be measured on history without this
(docs/superpowers/plans/2026-09-16-detector-edge-across-sessions.md, Task 2).

**Calendar time to the expiry session's close, as `runtime/option_delta.py` uses** --
the basis that reproduced Upstox's own delta to a median 0.004. **Unlike that module,
this one carries a rate.** With no rate, calls inverted 0.0097 above Upstox's figure and
puts 0.0094 below it on 2026-09-15, the same split on 2026-09-08: the mark of a rate
the broker includes. The best-fitting rate differed between those two days (0.04 and
0.07-0.08), so it is absorbing more than a rate; `implied_volatility_annual_carry_rate`
is the value with the least error over both, and its note carries the sweep
(measurements/2026-09-16-implied-volatility-from-premium/).

**A premium no volatility can produce is refused, never clamped.** At or below the
discounted intrinsic value, above what the ceiling volatility prices, or past expiry, the answer is None.
A clamped zero would read as the calmest contract on the board, and a clamped ceiling
as the wildest; both are a number the market never stated.
"""

from __future__ import annotations

import math

from runtime.option_delta import CALL, PUT


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def option_price(
    spot: float, strike: float, option_type: str, volatility: float,
    seconds_to_expiry: float, seconds_per_year: float, annual_rate: float,
) -> float | None:
    """The Black-Scholes premium; None where it is not defined."""
    if seconds_to_expiry <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return None
    if option_type not in (CALL, PUT):
        return None
    years = seconds_to_expiry / seconds_per_year
    spread = volatility * math.sqrt(years)
    discounted_strike = strike * math.exp(-annual_rate * years)
    d1 = (math.log(spot / strike) + (annual_rate + 0.5 * volatility * volatility) * years) / spread
    d2 = d1 - spread
    call = spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
    # Put-call parity: a put is the call less the forward's present value.
    return call if option_type == CALL else call - spot + discounted_strike


def implied_volatility(
    premium: float, spot: float, strike: float, option_type: str,
    seconds_to_expiry: float, seconds_per_year: float, annual_rate: float,
    tolerance: float, maximum_volatility: float,
) -> float | None:
    """The volatility, to within `tolerance`, whose price is `premium`.

    Bisection rather than Newton's method: the price is monotonic in volatility, so
    bisection cannot diverge, and far from the money -- where vega is near zero and a
    Newton step flies off -- is exactly where the zero-to-hero contracts live.
    """
    if premium is None or premium <= 0:
        return None
    ceiling_price = option_price(
        spot, strike, option_type, maximum_volatility, seconds_to_expiry, seconds_per_year,
        annual_rate,
    )
    if ceiling_price is None:
        return None
    # The floor is what a volatility of nothing prices: the discounted intrinsic value.
    forward_gap = spot - strike * math.exp(-annual_rate * seconds_to_expiry / seconds_per_year)
    floor_price = max(0.0, forward_gap) if option_type == CALL else max(0.0, -forward_gap)
    if premium <= floor_price or premium > ceiling_price:
        return None
    low, high = 0.0, maximum_volatility
    while high - low > tolerance:
        middle = 0.5 * (low + high)
        price = option_price(
            spot, strike, option_type, middle, seconds_to_expiry, seconds_per_year, annual_rate,
        )
        if price is None or price < premium:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


__all__ = ["implied_volatility", "option_price"]
