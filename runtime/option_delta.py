"""An option's delta estimated the way the broker states it, before the contract is priced.

Needed where a contract must be chosen before it is subscribed -- and so before the broker
has said anything about it. `broker-symbol-universe-bridge` picks the far strikes an
expiry-day zero-to-hero trade lives on this way (2026-09-15).

**Black-Scholes, no rate, calendar time to the session's close.** Of three bases fitted
against the delta Upstox states for today's NIFTY 15 SEP 26 contracts (69 samples,
measurements/2026-09-15-zero-to-hero-strikes/upstox-delta-time-basis.txt), calendar
seconds to the 15:30 close reproduced it to a median 0.004 (p90 0.008); trading seconds to
the close missed by 0.087, and calendar seconds to the master's 23:59:59 expiry stamp by
0.064. The rate is left out because over the hours that matter its effect is below that
error.

The estimate uses one volatility for every strike. A real chain's far strikes carry more
implied volatility than its middle (skew), so a far strike's true delta is somewhat
larger than this says; a caller choosing by it should expect the broker's own figure to
land on the far side of the boundary for the nearest strikes it picks.
"""

from __future__ import annotations

import math

CALL = "CE"
PUT = "PE"


def estimated_delta(
    spot: float, strike: float, option_type: str, implied_volatility: float,
    seconds_to_expiry: float, seconds_per_year: float,
) -> float | None:
    """Delta of a call (0..1) or a put (-1..0); None where it cannot be estimated.

    None for an expired contract, a volatility or price that is not positive, or an
    option type that is neither a call nor a put -- never a guessed zero, which would
    read as the furthest-out-of-the-money contract there is.
    """
    if seconds_to_expiry <= 0 or implied_volatility <= 0 or spot <= 0 or strike <= 0:
        return None
    if option_type not in (CALL, PUT):
        return None
    years = seconds_to_expiry / seconds_per_year
    spread = implied_volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * implied_volatility * implied_volatility * years) / spread
    cumulative = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return cumulative if option_type == CALL else cumulative - 1.0


__all__ = ["CALL", "PUT", "estimated_delta"]
