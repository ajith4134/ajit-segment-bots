# position-sizer closes what is held

**Proposed 2026-09-08.** `position-sizer` sizes a CLOSE/REDUCE intent against
the position it names, instead of through the entry/stop risk-budget path built
for OPEN.

## The gap

The user asked to investigate why no new trades had opened all session. Traced
via a temporary diagnostic on `position-sizer` and the live journal:
`opinion-arbiter` formed real, actionable trade-intents all day (2,355+ in one
sample window) -- but **every single one carried `action: "close"`**. A wider
80MB journal sample found 4,483 actionable intents and zero of them `open`.

`position-sizer.entry_price_for`/`stop_price_for` compute an entry and a stop
for the intent's *own* exit plan, which is how an OPEN is sized. A CLOSE has
neither by construction: `bull-opinion-composer.compose()` refuses to form an
opinion without a complete `exit_plan` (`NO_EXIT_PLAN`), and there is nowhere
new to enter when the position is already open, so no bot attaches one to a
close. Confirmed live: real intents (`ICICIBANK 1440 PE 29 SEP 26`, close)
correctly priced through `entry_price_for`'s fallback (`reference_price=22.525`,
non-None) but then fell through `stop_price_for` to `intent.stop_price`, which
was `None` -- refused as `missing_stop_price`, 100% of the time, for every
close intent this part ever saw.

Separately, `opening_order_target()` only checks `instrument` for `action ==
OPEN`; for CLOSE/REDUCE it returns `(intent.symbol, order_side_for(intent.side))`
unconditionally, which is why `opens_without_an_instrument_choice` stayed 0
throughout -- the check for "is there an instrument to trade" was never reached
for the actions that were actually flowing.

Downstream, `trade-capital-bounds-gate` would have made the same mistake even
once a close was sized: it checks a sized order's notional against the
position's *own already-committed capital*, refusing when `already > 0 and room
<= 0` -- exactly true of a position sized at the ceiling, which is precisely
the position a close order is trying to reduce.

## The fix

- `position-sizer` now consumes `position` (age-bounded on
  `capital_bounds_maximum_age_seconds`, the same setting
  `trade-capital-bounds-gate` already reads `position` under) and sizes
  CLOSE/REDUCE through a new `close_order()` method: quantity is the position's
  own held size, side is the *opposite* of what is held (not the intent's own
  directional side, which is the bot's view, not the closing side), and there
  is no stop or risk budget to size against.
- `SizedOrder`/`BoundedOrder` both carry a new `action` field (default `OPEN`,
  every existing call site unchanged) so a close is identifiable downstream
  without re-deriving it from quantity/side.
- `trade-capital-bounds-gate` skips its capital-ceiling economics entirely for
  `action in (CLOSE, REDUCE)` -- passed straight through at the size the sizer
  already computed from the position.

REDUCE is not distinguished from a full close: `TradeIntent` carries no reduce
fraction, and closing the whole position is the safe direction until a
partial-reduce size is a decision this project has actually observed a bot ask
for.

## What this does not fix

`ADD_TO` (adding to an existing position) still goes through the OPEN-shaped
path unchanged -- no evidence today of it firing, and touching it without that
evidence would be a change made on a guess rather than a measurement.
