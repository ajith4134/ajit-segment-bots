# trade-capital-bounds-gate consumes position

Proposed by Claude, 2026-09-07, after finding positions eight times the ceiling.

## The defect

`maximum_capital_per_trade` is 200,000 rupees. Measured on the live spine that
day, **66 open positions were above it**, the largest at more than six times:

| capital | contract | quantity | entry |
|---|---|---|---|
| Rs 1,277,667 | BHARTIARTL 1860 PE 29 SEP 26 | 40,386.99 | 31.64 |
| Rs 715,292 | BHARTIARTL 1860 PE 29 SEP 26 | 21,873.22 | 32.70 |
| Rs 675,600 | KOTAKBANK 425 PE 29 SEP 26 | 105,875.01 | 6.38 |

Nothing was broken in the gate. It does exactly what it was written to do:
`bound(sized_order, bounds)` caps **one order** at the maximum, and it capped
2,327 of them. But a position is the sum of many orders, and the bots re-decide
the same contract every few seconds. One contract's own history, from the
position journal:

    opened      4,149 units
    changed     4,978 -> 5,548 -> 8,139 -> 9,174 -> 10,492
    closed
    opened      2,816 units
    changed     5,376 -> 12,423 -> 20,590 -> 24,983 -> 32,531

Every single add passed the gate at or under 200,000. Thirty-two thousand units
at 24.40 is **Rs 794,000** in a segment allowed 200,000 a trade.

The operator's own words for this setting are "the most one trade may commit",
and its note says the gate "refuses above it rather than trimming silently". A
trade is a position, not a slice of one. The bound as implemented is on the
slice, so the thing the operator bounded is not the thing that was bounded.

## What changes

`trade-capital-bounds-gate` consumes `position`.

It then bounds **what this order would make the position**, not what this order
is on its own: an order is capped to the room left under the ceiling, and refused
outright when there is no room. A symbol nothing is held in behaves exactly as
before, so this is not a new rule for the first order of a trade — it is the
same rule, applied to the quantity the operator was actually talking about.

## Why here and not elsewhere

**Not in `position-sizer`.** Sizing answers "how much risk does this intent
deserve" from the stop distance and the risk limit. The capital ceiling is a
different question with a different owner -- `capital-allotment-reader` reads it
from the operator's own file -- and the gate exists precisely so that answer is
applied in one place after sizing.

**Not in `exposure-limiter`.** That bounds exposure across symbols and segments,
which is the portfolio question. This is the per-trade one, and collapsing them
would make one number answer two questions.

**Not by refusing to re-enter a symbol already held.** Adding to a winner is a
strategy this project has not ruled out, and a bound is not a ban. The room left
under the ceiling is the honest expression: add while there is room, stop when
there is not.

## Contract

`position` is produced by `position-close-detector` in `portfolio-state` and is
already consumed across `risk-capital-allocation` (`exposure-limiter`,
`position-flattener`, `profit-lock` and others), so this adds no new block edge
and no peer-block crossing. `check_contracts.py` must pass unchanged.
