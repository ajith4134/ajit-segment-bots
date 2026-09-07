# stop-target-placer prices the chosen instrument, not the underlying it reasoned about

Proposed by Claude, 2026-09-07.

## The defect

`stop-target-placer` recovers an entry price from the bot's own exit plan
(`bull-exit-plan`/`bear-exit-plan`), which reasons in the *underlying's* price
(symbol-price-frame -- NIFTY spot, a stock's own price). It placed the stop
against that price and published it as `stop-target-plan.entry_price` /
`.stop_price`.

`position-sizer` prefers a refined `stop-target-plan`'s `entry_price` over the
chosen instrument's own `reference_price` (deliberately -- the plan is meant to
be the more refined number). For index-options and stock-options, the chosen
instrument is an option *contract*, priced in premium (~220), not the
underlying's spot (~24,500). Sizing an order for the contract against a stop
computed on the underlying's scale put the stop on the wrong side of entry (or
priced the trade using a number that was never the tradeable instrument's own
price) on effectively every option open.

Separately, and only exposed once the first defect is fixed: `stop-target-
placer` derived the order's BUY/SELL side from the intent's own long/short.
Index-options and stock-options are buy-only -- a bearish view is expressed by
*buying a put*, never selling to open -- so a SHORT intent read as SELL,
placing the stop above an entry the BUY order never traded above. Measured on
the live spine 2026-09-07: `stop_from_refined_plan` and `refused_stop_invalid`
matched exactly, 2,040 of 2,040.

## The fix

`stop-target-placer` now consumes `instrument-choice` (already published by
`instrument-selector`, already consumed by `position-sizer`) and:

- prices the stop against the chosen instrument's own `reference_price` when
  one has been chosen, falling back to the underlying's recovered price only
  when nothing has been selected yet;
- translates the exit plan's target into a fraction of the underlying entry
  and reapplies that fraction to the priced entry, so reward-to-risk survives
  the scale change unchanged;
- derives the order's actual side from `instrument-choice.order_side` for an
  open (the one place this translation is defined, per T-4), keeping the
  intent's own translated side for `reduce`/`close`, which act on the contract
  already held rather than a fresh selection.

One new edge, no new part: `instrument-choice` -> `stop-target-plan`.
`position-sizer`'s existing preference for a refined plan's `entry_price` over
the instrument's own is now correct rather than coincidentally correct, because
the refined plan's `entry_price` *is* the instrument's own price once one has
been chosen.
