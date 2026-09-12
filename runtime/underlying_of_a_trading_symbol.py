"""What a trading symbol is a claim on, derived once rather than four times.

Four parts needed this and four wrote their own, each right about a different
market and wrong about the other:

    volatility-gap-detector      symbol.rstrip("USDT")
    cross-segment-signal-bridge  settlement suffix only
    options-flow-reader          settlement suffix only
    cross-segment-exposure-watch first token, then settlement suffix  <- correct

**`rstrip` is not "remove this suffix".** It strips every trailing character that
appears in the set, so `rstrip("USDT")` removes any run of U, S, D and T from the
end of the name. It happened to give `BTC` for `BTCUSDT` and is simply wrong
everywhere else. Measured against the real Upstox master 2026-09-12, it mangles
**39 of the 210 NSE F&O stock underlyings**:

    LT          -> L            ADANIENT   -> ADANIEN
    HAVELLS     -> HAVELL       ASIANPAINT -> ASIANPAIN
    CUMMINSIND  -> CUMMINSIN    EICHERMOT  -> EICHERMO
    NESTLEIND   -> NESTLEIN     DMART      -> DMAR

A mangled key does not raise: it silently fails to match the surface keyed by the
real underlying, so `volatility-gap-detector` recorded an implied volatility
against nothing for those names and its counters read exactly as they do when a
symbol is simply quiet.

The settlement-suffix-only rule is wrong the other way. An NSE option's trading
symbol is `"HINDUNILVR 1980 PE 29 SEP 26"`, which ends in no settlement currency,
so that rule returns the whole contract name and the option never resolves to
the share it is a claim on.

**The rule, in order:**

1. **The first whitespace token**, when there is one. An NSE option, future or
   index contract leads with the share or index it is written on, so this is the
   underlying and it is exact.
2. **The settlement suffix**, when the symbol carries the one this venue states
   and is longer than it. Removed by slicing, never by `rstrip`. Kept because it
   is still right for a perpetual and costs nothing where there is no suffix.
3. **The symbol itself.** A share, an index, or a name whose shape neither rule
   recognises. Returning it unchanged is the honest answer: this function's job
   is to name the underlying, and inventing one it cannot derive would put a
   symbol nothing prices into whatever is keyed by it.

Lives in `runtime/` rather than in a part because it is data-shaped knowledge
that several parts need and none of them owns -- the same argument
`runtime/symbol_round_trip_cost.py` makes for the round trip. A part importing
another part to get it would be wired to that part rather than to the data (T-4).
"""

from __future__ import annotations


def underlying_of_a_trading_symbol(symbol: str, settlement: str = "") -> str:
    """The share, index or asset this trading symbol is a claim on.

    `settlement` is the venue's own settlement currency where it has one, and is
    empty for a venue whose symbols carry no suffix -- which is every Indian one.
    Passing it is what keeps the crypto shape working; leaving it out is correct
    for NSE and BSE.
    """
    if not symbol:
        return symbol
    head = symbol.split(" ", 1)[0]
    if head != symbol:
        return head
    if settlement and symbol.endswith(settlement) and len(symbol) > len(settlement):
        return symbol[: -len(settlement)]
    return symbol


__all__ = ["underlying_of_a_trading_symbol"]
