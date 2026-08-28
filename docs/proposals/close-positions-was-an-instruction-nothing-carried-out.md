# close-positions was an instruction nothing carried out

**Written 2026-08-27, on the operator's instruction to exit every open trade.**

`human-override-reader` has understood five instructions since the door was
built, and `close-positions` is one of them. Tracing what happens to it:

    human-override-reader  ->  human-override
      trading-halt-decider ->  trading-halt carrying may_close_positions
      halt-enforcer        ->  risk-limit of zero

Both consumers stop the bot *opening* something new. **Neither closes anything.**
`halt-enforcer` produces `risk-limit` and nothing else; no part in the blueprint
places an exit because a human asked for one. An operator who wrote the override
and walked away would come back to the same book, held by a system that had
recorded the instruction and obeyed half of it.

Exits happen one way today: a resting stop fires, placed by `stop-order-manager`
from a `stop-adjustment`. On 2026-08-27 the open book was 17 positions and
**10 of them had no stop resting**, so for those there was no path to flat at all.

## What this adds

One part, `position-flattener`, in the block that already enforces a human's stop:

    consumes  human-override, position, money-mode
    produces  order-request, part-health

On an active `close-positions` it places one market exit per open position, side
opposite the position, quantity the whole of it. The order goes out as
`order-request` — the same wire `stop-order-manager` puts its exits on, so the
exit is latency-gated by `order-latency-simulator` and filled by
`paper-fill-simulator` exactly as any other order is.

### Four decisions worth not undoing

**A market order, not a tightened stop.** Moving every stop to the touch would
flatten the book with machinery that already exists and would be a lie in the
journal: the position would read as stopped out at its risk limit, when what
happened is that a human asked for it to be closed. What the record says about
why a trade ended is the raw material every learner here trains on, and
`loss-cause-classifier`, `exit-quality-scorer` and `exit-timing-learner` would
all be reading a stop-out that never happened.

**Only `close-positions`.** `stop-everything` and `stop-trading` say the machine
must go quiet; only this one says the book must go. Reading them as one would
liquidate a book because a human wanted the bot to stop thinking.

**Repeat only after `order_latency_maximum`.** Derived, not chosen (RL-061):
that setting is the longest the paper book will hold an order before answering,
so an exit unfilled past it has genuinely not been taken — while an exit repeated
sooner is two closes racing for one position.

**Forgetting is scoped to the `override_id`.** What has been sent is remembered
against the instruction that caused it, so a second `close-positions` next week
flattens again. A part that remembered across instructions would refuse to close
a book it had closed once before, which is the same shape as the flap report that
never expired (`a-sixty-second-hold-that-lasted-six-hours.md`).

Where an exit is sent is never defaulted: with no `money-mode` read it places
nothing and says so. Paper and live are not interchangeable, and guessing is how
a paper instruction reaches a venue.

## Priority

Ranked 30 in `part-priority.toml`, and `never_switched_off_priority_ceiling`
moves from 29 to 30 to cover it. The reasoning is the one that already put the
governor's own instruments there: **a part that exists to carry out a human's
instruction must not be switched off by the machine the human is instructing.**
It takes no resource floor — that stops at `reservation_priority_ceiling` (10) —
it is simply never shed.

## What this does not do

It does not decide *when* to close. Nothing in the system can raise a
`close-positions`; it comes from a file only a human writes
(`human_override_path`), and this part reads the reader's publication of it. The
bot cannot flatten itself.
