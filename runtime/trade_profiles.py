"""What a symbol's movement looks like around a decision, and how long it takes.

Substrate, not a part. Five parts consume `excursion-profile` and four consume
`horizon-profile`, and none may import another (T-4) -- so these live here rather
than in whichever part happened to define one first.

They were defined in four places before 2026-08-23: `ExcursionProfile` in the bull
proposer and again in the bear proposer, `HorizonProfile` in both of those and a
third, different thing under the same name in `runtime.trade_decoding_types`. The
bus pickles, so a profile produced by one part arrived at another as a class it did
not recognise. That is the same defect that killed three parts the day the closing
chain was first forked, and it is why one definition is the only safe number.

## Two producers each, on purpose

`excursion-profile` is produced by `excursion-profiler`, from real closed trades,
and by `signal-excursion-profiler`, from detector claims the market settled. The
second exists because the first cannot run until a trade has closed, and a trade
cannot be opened without a stop (docs/proposals/live-excursion-and-horizon-profiling.md).

They are deliberately the same type rather than two. A claim is not a trade -- it
has no slippage, no fees and no size -- but what both measure is the shape of the
symbol's movement inside a stated horizon, which is what a stop distance is about.
Keeping one type is what lets the two be compared once real trades exist; inventing
a second would guarantee they never were.

`trades_observed` is named for what it counts in the older producer. In the claim
producer it counts settled claims, and the profile's `source` says which -- a
number whose meaning changes silently with its producer is worse than two numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Where a profile's observations came from. Read by anything comparing the two, and
# by the board, which must never present a claim-derived profile as trade evidence.
FROM_CLOSED_TRADES = "closed-trades"
FROM_SETTLED_CLAIMS = "settled-claims"


@dataclass(frozen=True)
class ExcursionProfile:
    """How far this symbol travels against and in favour of a correct call.

    Fractions of the price the call was made at, signed towards the call:
    `adverse_excursion` is positive and describes movement *against* it.

    `adverse_excursion` is the excursion a call that came right survived, which is
    the only honest basis for a stop: a stop inside it converts winners into
    losers, and that failure is invisible in the equity curve because it looks
    like a bad strategy rather than a bad stop.

    `favourable_quantiles` maps a quantile to the move reached, which is where
    scaling out belongs -- a target beyond what the symbol reaches is a target that
    never fills.
    """

    venue_id: str
    symbol: str
    side: str
    adverse_excursion: float
    favourable_quantiles: dict
    trades_observed: int
    is_fitted: bool
    source: str = FROM_CLOSED_TRADES
    reason: str = ""
    measured_at_ns: int = 0


@dataclass(frozen=True)
class HorizonProfile:
    """How long this kind of call has taken the market to settle, in seconds.

    Per detector, because how long a move takes is a property of what was noticed
    rather than of the symbol: a liquidation cascade and a cointegration spread
    resolve on different clocks in the same book.

    Not to be confused with `runtime.trade_decoding_types.HorizonProfile`, which
    measures where holding *longer* stops paying. The two wear one name and one
    data type in the blueprint today, and the split is named as a defect in
    docs/proposals/live-excursion-and-horizon-profiling.md.
    """

    detector: str
    median_seconds: float
    trades_observed: int
    is_fitted: bool
    source: str = FROM_CLOSED_TRADES
    reason: str = ""
    measured_at_ns: int = 0


def quantile_of(values, quantile: float) -> float | None:
    """The `quantile` of `values`, or None when there is nothing to take it of.

    Linear interpolation between the two order statistics that bracket the
    position, which is the definition every other estimator in this project uses.
    None rather than a default: a quantile of an empty sample is not zero, and a
    caller handed zero would place a stop at the entry price.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0.0 < quantile < 1.0:
        raise ValueError(
            f"a quantile of {quantile} is not inside the distribution; 0 and 1 are the "
            f"minimum and maximum, which are the two order statistics a sample says least about"
        )
    position = quantile * (len(ordered) - 1)
    below = int(position)
    above = min(below + 1, len(ordered) - 1)
    weight = position - below
    return ordered[below] * (1.0 - weight) + ordered[above] * weight
