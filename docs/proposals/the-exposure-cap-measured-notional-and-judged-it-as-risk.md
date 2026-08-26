# The exposure cap measured notional and judged it as risk

**Proposed by Claude, 2026-08-26. Decided by the operator the same day: the caps
mean risk.**

## What was measured

On the live spine at 15:16, ten minutes after a restart:

    position-sizer      71,233 actionable intents, sized 0,
                        refused_no_risk_allowed 62,935
    exposure-limiter    open_positions 12, allotment 10000,
                        total_exposure 1.9878
    stop-order-manager  placed 0, stops_resting 0

`exposure-limiter` records each position as `notional / allotment` and judges the
sum against three settings the operator wrote about **risk**:

    risk_maximum_per_position_fraction  0.01  "the most one position may risk"
    risk_maximum_total_fraction         0.05  "the most every open position may risk together"
    risk_maximum_per_cluster_fraction   0.02

The per-position note says it outright: *"at the 10000 USDT paper balance that is
100 USDT at risk per trade, and a stop 0.5% away therefore buys about 20000 USDT
of notional"*. A position sized to risk 1% of the allotment is therefore recorded
by this part as roughly 200% of it, and twelve of them read as 199% against a 5%
cap. The limiter allowed nothing, on any symbol, for as long as any position was
open.

## What it should measure

The risk a position actually carries: how much is lost if it goes to its stop.

    risk of a position = |average_entry_price - stop_price| * |quantity| / allotment

That is the quantity `position-sizer` solves for when it sizes, so the two parts
finally reason about the same number: the sizer says "risk 1% of the allotment on
this trade" and the limiter says "the book is already risking 4.6% of it".

## The position with no stop

`Position` carries no stop, so the stop has to come from the book's own state.
`stop-adjustment` carries `venue_id`, `symbol`, `entry_price`, `previous_stop` and
`new_stop` for an open position, and is produced by `profit-lock` and
`exit-order-chainer` — both in this block. This proposal adds it to
`exposure-limiter`'s consumes.

**A position whose stop nobody can name is counted at its full notional.** Not
excluded, and not assumed small: a position with no stop resting can lose all of
it, so its notional *is* its risk. That is not a defensive default, it is the
arithmetic — and on the day this was written it is also the true state of the
book, because `stop-order-manager` has placed 0 stops and 12 positions are open.
The count is published as `positions_without_a_stop` so the board says which of
the two kinds of exposure is binding rather than leaving them summed together.

## What this does not fix

The cap will keep binding while those twelve positions sit unprotected, and that
is the correct answer to the question the limiter is asked. What is wrong is one
layer up: `profit-lock` publishes an adjustment only when a stop *moves* and
`exit-order-chainer` acts on a *fill*, so a position restored from a checkpoint
has never been offered a stop at all. State that is only ever acted on through
events is invisible to anything that restarts. That is the next piece of work and
it is named here so this proposal is not read as having finished it.
