# exposure-limiter reads the balance its caps are fractions of

**Proposed by Claude, 2026-08-25, during the payload-shape sweep that followed
`position-sizer` crashing on a field its producer has never carried.**

## What was found

`exposure-limiter` caps risk three ways -- per position, total gross, and per
correlation cluster -- and every cap is *a fraction of the segment's allotment*.
Its own `observe_position` takes that fraction as an argument:

    def observe_position(self, venue_id: str, symbol: str, exposure_fraction: float)

and the part reads it like this:

    limiter.observe_position(
        position.venue_id, position.symbol, getattr(position, "exposure_fraction", 0.0)
    )

`Position` -- `runtime/trading_types.py`, the type `fill-reconciler` publishes on
`position` -- has never carried `exposure_fraction`. It carries `quantity`,
`average_entry_price`, `realised_pnl`, `fees_paid` and two timestamps.

**So every position has been observed at an exposure of zero.** `total_used` is a
sum of zeros, every cluster's share is zero, and the limit this part publishes has
been the full per-position cap on every tick since it first ran. A limiter that
cannot lower a limit is not a limiter; it is a part that makes the board green.

The `getattr` default is what made it invisible. A crash announces itself -- this
returned a plausible number and kept running, which is the failure mode
`docs/rulings` RL-072 and Rule 8 are both about.

## Why this needs a blueprint edit rather than a code fix

The fraction cannot be computed from `position` alone. Notional is
`abs(quantity) * average_entry_price`; a *fraction* needs the allotment it is a
fraction of, and that is `account-balance.equity` -- which this part does not
consume, so under R-01 it has no wire to read it on and no code may invent one.

`account-balance` is produced by `paper-account-keeper` and already consumed by
`position-sizer`, `capital-utilisation-meter` and `fund-lock-ledger` for exactly
this purpose: it is the number the desk sizes against.

## The change

One part gains one input:

    exposure-limiter  consumes += account-balance

No new part, no new data type, no change to what it produces. `T-4` holds: the
part names data, not the part that produces it.

## What it does not fix

Exposure is measured at the cost basis, because that is what `Position` carries.
A position 20% under water is still counted at what it cost. That is the
conservative direction for a long book and the wrong direction for a short one,
and correcting it needs a mark price this part does not consume -- which is a
separate edit, with its own proposal, if measurement shows it matters.
