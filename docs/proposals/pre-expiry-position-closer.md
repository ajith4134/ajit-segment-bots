# pre-expiry-position-closer

**Proposed 2026-09-05.** Closes at market anything held in a contract that
expires today, a settings-named number of minutes before the session closes,
rather than holding it to settlement.

## The gap

The options spec decided this on 2026-09-01, section 2:

> **Force-close before expiry**, a settings-driven buffer of days rather than
> held to exercise — avoids modelling ITM auto-exercise/assignment in Phase A.
> Exact buffer is a number to justify with real data at implementation time
> (RL-061), not decided here.

Nothing implemented it. Measured 2026-09-05: no part in the tree reasons about
closing a position because its contract is about to stop existing, and
`position-flattener` — the only part that places an exit on its own initiative —
acts solely on a human's `close-positions`. Every other exit is a resting stop
(`stop-order-manager`) or a plan a bot proposed. An option that nobody's stop
caught simply stays open through expiry.

## Why it cannot wait for Phase B

**Indian single-stock options are physically settled.** A bought call held
through expiry does not quietly become cash; it becomes a delivery obligation
for strike × lot size. On a ₹100 premium against a ₹1,400 strike that is more
than an order of magnitude larger than anything the position sizer ever agreed
to. Index options are the milder, cash-settled case of the same rule — a
mis-modelled settlement rather than an obligation — and both are covered here
rather than in two places.

Phase A's second segment bot is stock options (`docs/goal.md` item 3), so this
stops being a modelling nicety the moment bot 2 opens anything.

## Minutes before the close, not a buffer of days

The spec sketched a buffer of whole days. That is the wrong shape for this
project, and the reason is a part that already exists:
`expiry-day-zero-to-hero-detector` is written precisely to trade the expiry-day
move on a far-out-of-the-money contract. A day-scale buffer would forbid the one
detector built for that day from ever holding what it bought.

So the bound is intraday. A position in a contract expiring today is left
completely alone until the session is within
`close_out_minutes_before_the_expiry_session_closes` of its close, and is then
closed at market. The day stays tradeable and nothing is outstanding at
settlement.

**Fifteen minutes**, and its size is set by what must fit inside it rather than
by taste: an exit is repeated no sooner than `order_latency_maximum` (5.0 s) and
only once the previous ask has been answered by the position shrinking, so the
window has to hold several ask-and-answer rounds against a book at its thinnest
of the week. Fifteen minutes is 180 such rounds, and against NSE's 15:30 close it
opens at 15:15 — inside normal market hours, not the 15:40–16:00 post-close
window, which is cash-only and cannot close an option at all. Widen it the first
time an exit is still outstanding at the close; `longest_wait_seconds` and
`positions_at_the_cap` on this part's own standing are what say so.

## What it reuses, and why that is not optional

The exits are placed by `runtime/position_exit_placer.py`, extracted from
`position-flattener` rather than written again. That extraction is the whole
reason this part is safe to add:

    allowance = what is held now - what is already asked and unanswered

That bound is the residue of a live incident on 2026-08-30. Without it the
flattener's repeat fired on a timer alone, every repeat carried a fresh
`client_order_id` so the fill simulator's duplicate guard could never refuse
one, and the asks accumulated rather than replacing each other — 426 exits
placed against 2 open positions, 2,199 in flight, and then filled:

    flatten sell TURBOUSDT  13,020,303  against ~8,500,000 held -> a NEW short
    1,048,525 USDT of flatten notional against a 190,900 USDT book

A close that overshoots does not stop at flat, it reverses. A second part
writing that logic from scratch would have written that bug from scratch. The
17 tests that already protect the flattener were left unchanged through the
extraction, which is what says the extraction was faithful.

## What it cannot judge, it reports

A position's `symbol` is an `instrument_key` from some parts and a
`trading_symbol` from others, so expiries are indexed under both. A position
whose contract resolves to neither is counted in `positions_with_no_listing`,
and one that resolves to something with no expiry — an index, an equity — in
`positions_that_are_not_options`. The two are separate counters on purpose: "this
is not an option" and "I could not find out what this is" are different facts,
and the second one is a position this part cannot protect (Rule 8).

`skipped_tick_effect` is `corrupts`, unlike every other exit-placing part here.
A tick skipped inside the closing window is a position still open when the
contract settles, and there is no later tick that can undo it.

## Verification

    python3 dashboard/check_contracts.py
    python3 dashboard/check_payload_reads.py
    python3 dashboard/check_part_calls.py
    .venv/bin/python -m pytest tests/parts/risk_capital_allocation

and live, on the first expiry day the spine runs through: `positions_expiring_today`
rising during the session, `ticks_before_the_window` climbing until 15:15,
`closed_because_of_expiry` moving after it, and `positions_with_no_listing`
staying at zero — a position it cannot resolve is a position it cannot protect.
