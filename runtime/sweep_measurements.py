"""The measurements a watch condition may name, and how each is computed.

Shared vocabulary between three parts that may not import one another (T-4):

- `watch-condition-compiler` refuses a condition naming a measurement nobody
  computes.
- `universal-symbol-sweeper` computes them per symbol from what crosses the bus,
  and evaluates conditions against them.
- `signal-outcome-labeller` stamps them onto every training label, so what the
  miner searches over is the same vocabulary the scanner can watch.

**That third one is why this file exists in its present form.** Until 2026-08-26
the labeller stamped each detector's own evidence keys as the label's features --
`spread_z`, `hedge_ratio`, `reversion_strength` for the pair detector -- and the
miner therefore mined formulas over names that were meaningful only inside the
detector that produced them. A pair's spread z-score is not a property of a
symbol, so no such formula could ever be evaluated on an arbitrary symbol, and
`instruction-writer` refused every one of them as `NO_MEASUREMENT`. A vocabulary
that discovery and scanning do not share is a pipeline that cannot connect, and
the refusal at the end of it was correct rather than conservative.

**Three kinds of measurement, and the third is the one that was missing.**

- *Level* -- what this symbol is doing now: price, the consolidated price.
- *Time-series* -- what this symbol is doing relative to its own recent history:
  its return, that return in units of its own volatility, where it sits in its
  own range. These are the shape the existing detectors already use.
- *Cross-sectional* -- what this symbol is doing relative to every other symbol
  right now. Only a part holding the whole universe at once can compute these,
  which is precisely what the sweeper and the labeller both do and no individual
  detector can.

The cross-sectional half carries a specific correction with it. Crypto
perpetuals are close to a single-factor market, so a raw cross-sectional rank of
returns mostly ranks symbols by their beta to that factor -- it rediscovers which
alt is highest-beta today rather than finding a dislocation. So the universe
median return is subtracted first and the *excess* is what gets ranked.
`return_rank` is kept alongside `market_excess_rank` deliberately: they are
different claims, and a condition that only works on the un-neutralised one is
telling us something about beta that is worth being able to see.

**A measurement that cannot be supported is absent, never zero.** Every statistic
here comes from `RollingWindow`, which returns None rather than a number computed
from too few observations. An absent measurement makes the sweeper skip that
symbol and count it as unmeasurable; a zero would make it evaluate a condition
against a fact nobody established.
"""

from __future__ import annotations

PRICE = "price"
RETURN_OVER_WINDOW = "return_over_window"
CONSOLIDATED_PRICE = "consolidated_price"
VENUE_DISAGREEMENT = "venue_disagreement"
RETURN_Z = "return_z"
REALISED_VOLATILITY = "realised_volatility"
VOLATILITY_RATIO = "volatility_ratio"
POSITION_IN_RANGE = "position_in_range"
DRAWDOWN_FROM_HIGH = "drawdown_from_high"
RUNUP_FROM_LOW = "runup_from_low"

# Computable only by a part that holds the whole universe at once.
RETURN_RANK = "return_rank"
MARKET_EXCESS_RETURN = "market_excess_return"
MARKET_EXCESS_RANK = "market_excess_rank"
VOLATILITY_RANK = "volatility_rank"
RETURN_Z_RANK = "return_z_rank"

PER_SYMBOL_MEASUREMENTS = (
    PRICE,
    RETURN_OVER_WINDOW,
    CONSOLIDATED_PRICE,
    VENUE_DISAGREEMENT,
    RETURN_Z,
    REALISED_VOLATILITY,
    VOLATILITY_RATIO,
    POSITION_IN_RANGE,
    DRAWDOWN_FROM_HIGH,
    RUNUP_FROM_LOW,
)

CROSS_SECTIONAL_MEASUREMENTS = (
    RETURN_RANK,
    MARKET_EXCESS_RETURN,
    MARKET_EXCESS_RANK,
    VOLATILITY_RANK,
    RETURN_Z_RANK,
)

KNOWN_MEASUREMENTS = PER_SYMBOL_MEASUREMENTS + CROSS_SECTIONAL_MEASUREMENTS

# What each cross-sectional measurement ranks. Stated as data rather than as a
# branch per name, so adding one is adding a row.
_RANKED_FROM = {
    RETURN_RANK: RETURN_OVER_WINDOW,
    MARKET_EXCESS_RANK: MARKET_EXCESS_RETURN,
    VOLATILITY_RANK: REALISED_VOLATILITY,
    RETURN_Z_RANK: RETURN_Z,
}


def measure(latest_price=None, window_open_price=None, consolidated=None) -> dict:
    """Every measurement three prices can state; absent ones are left out.

    The narrow form, kept because a caller holding three numbers and no window
    can still say what it knows. `measure_symbol` is what the sweeper and the
    labeller use, and it states strictly more.
    """
    found: dict[str, float] = {}
    if latest_price is not None:
        found[PRICE] = latest_price
        if window_open_price:
            found[RETURN_OVER_WINDOW] = latest_price / window_open_price - 1.0
        if consolidated:
            found[CONSOLIDATED_PRICE] = consolidated
            found[VENUE_DISAGREEMENT] = latest_price / consolidated - 1.0
    return found


def measure_symbol(
    window,
    consolidated=None,
    minimum_observations: int = 2,
    short_window_fraction: float = 0.25,
) -> dict:
    """Everything one symbol's own price window supports, and nothing it does not.

    `window` is a `runtime.rolling_statistics.RollingWindow` over that symbol's
    prices. Statistics it declines to compute are simply absent from the result,
    which is what makes the sweeper's `measurement-missing` count honest.

    `short_window_fraction` decides what "recent" means for the volatility ratio:
    the standard deviation of the most recent slice of returns against the whole
    window's. A ratio above one is a symbol whose volatility is rising, which is
    a different statement from a symbol that has moved.
    """
    prices = list(window.values)
    latest = window.latest
    found = measure(latest, prices[0] if prices else None, consolidated)
    if latest is None or len(prices) < minimum_observations:
        return found

    changes = window.returns()
    if len(changes) >= minimum_observations:
        volatility = _standard_deviation(changes)
        if volatility is not None:
            found[REALISED_VOLATILITY] = volatility
            if volatility > 0.0 and RETURN_OVER_WINDOW in found:
                # The window's return expressed in units of this symbol's own
                # per-observation volatility, so a 2% move means something
                # different in a stablecoin pair and in a thin alt.
                found[RETURN_Z] = found[RETURN_OVER_WINDOW] / volatility
            recent_length = max(minimum_observations, int(len(changes) * short_window_fraction))
            if recent_length < len(changes):
                recent = _standard_deviation(changes[-recent_length:])
                if recent is not None and volatility > 0.0:
                    found[VOLATILITY_RATIO] = recent / volatility

    highest = max(prices)
    lowest = min(prices)
    if highest > lowest:
        found[POSITION_IN_RANGE] = (latest - lowest) / (highest - lowest)
    if highest > 0.0:
        found[DRAWDOWN_FROM_HIGH] = latest / highest - 1.0
    if lowest > 0.0:
        found[RUNUP_FROM_LOW] = latest / lowest - 1.0
    return found


def add_cross_sectional(measured_by_symbol: dict, minimum_symbols: int = 2) -> dict:
    """Add every universe-relative measurement, in place, and return the same mapping.

    `measured_by_symbol` maps whatever key the caller uses for a symbol to that
    symbol's `measure_symbol` result.

    **The excess is computed before anything is ranked.** In a market where most
    symbols move together, ranking raw returns ranks each symbol's sensitivity to
    the move everyone shared. Subtracting the universe median first leaves what
    this symbol did that the market did not, which is the part a cross-sectional
    claim is actually about.

    The median rather than the mean: one symbol printing a bad tick would drag a
    mean far enough to shift every other symbol's excess, and a scanner whose
    whole universe moves because of one bad print is worse than one that does not
    rank at all.

    **Below `minimum_symbols` nothing is added.** A percentile over one
    observation is 0 or 1 by construction and says nothing about the universe;
    absent is the truthful answer.
    """
    if len(measured_by_symbol) < minimum_symbols:
        return measured_by_symbol

    returns = [
        found[RETURN_OVER_WINDOW]
        for found in measured_by_symbol.values()
        if RETURN_OVER_WINDOW in found
    ]
    if len(returns) >= minimum_symbols:
        market = _median(returns)
        for found in measured_by_symbol.values():
            if RETURN_OVER_WINDOW in found:
                found[MARKET_EXCESS_RETURN] = found[RETURN_OVER_WINDOW] - market

    for rank_name, source_name in _RANKED_FROM.items():
        _rank_into(measured_by_symbol, source_name, rank_name, minimum_symbols)
    return measured_by_symbol


def _rank_into(measured_by_symbol: dict, source_name: str, rank_name: str, minimum_symbols: int) -> None:
    """Write each symbol's percentile of one measurement, over the symbols that have it.

    Ties share the average of the positions they span, so two symbols with the
    same return cannot be ordered by the accident of which was observed first.
    """
    having = [
        (found[source_name], found)
        for found in measured_by_symbol.values()
        if source_name in found
    ]
    if len(having) < minimum_symbols:
        return
    having.sort(key=lambda pair: pair[0])
    last = len(having) - 1
    index = 0
    while index <= last:
        stop = index
        while stop < last and having[stop + 1][0] == having[index][0]:
            stop += 1
        percentile = ((index + stop) / 2.0) / last
        for _, found in having[index:stop + 1]:
            found[rank_name] = percentile
        index = stop + 1


def _standard_deviation(values) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance ** 0.5


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


__all__ = [
    "KNOWN_MEASUREMENTS",
    "PER_SYMBOL_MEASUREMENTS",
    "CROSS_SECTIONAL_MEASUREMENTS",
    "PRICE",
    "RETURN_OVER_WINDOW",
    "CONSOLIDATED_PRICE",
    "VENUE_DISAGREEMENT",
    "RETURN_Z",
    "REALISED_VOLATILITY",
    "VOLATILITY_RATIO",
    "POSITION_IN_RANGE",
    "DRAWDOWN_FROM_HIGH",
    "RUNUP_FROM_LOW",
    "RETURN_RANK",
    "MARKET_EXCESS_RETURN",
    "MARKET_EXCESS_RANK",
    "VOLATILITY_RANK",
    "RETURN_Z_RANK",
    "measure",
    "measure_symbol",
    "add_cross_sectional",
]
