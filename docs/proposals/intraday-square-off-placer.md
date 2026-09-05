# intraday-square-off-placer

**Proposed 2026-09-05.** Closes everything an intraday segment holds, a
settings-named number of minutes before the session closes, so the broker never
squares the position off instead.

## The gap

Phase A's third bot — the temporary goal of 2026-09-05 — trades cash equity
intraday on the broker's leverage. An intraday (MIS) position is not something
this system may leave open: the broker squares it off itself, from around 15:15
IST, at whatever the book offers, or converts it to delivery with a margin call
behind it.

Either way the exit is one this system did not choose, did not price, and cannot
learn from. It would reach `closed-trade` as a fill nobody here decided, and
every learner downstream — `exit-quality-scorer`, `exit-timing-learner`,
`lesson-extractor` — would train on it as though the bot had made it.

Nothing in the tree does this. `position-flattener` acts only on a human's
`close-positions`, and `pre-expiry-position-closer` (built the same day) closes
only a contract that is about to stop existing.

## Why it is not the expiry closer with a different trigger

The two answer different questions:

| | |
|---|---|
| `pre-expiry-position-closer` | *is this instrument expiring today?* — closes that contract |
| `intraday-square-off-placer` | *may this segment hold anything past today at all?* — closes everything |

They must also stay different in the journal. What a record says about why a
trade ended is the raw material every learner here trains on, so "the contract
expired", "a human said close" and "this segment cannot hold overnight" are
three lessons, not one — and each part carries its own reason string.

## Whether a segment is intraday is the segment's own statement

Read from `positions_are_squared_off_daily` in
`settings/segments/<segment_id>.toml`. True for `cash-equity-intraday`, absent
for both options segments.

**Silence means may-hold**, and that direction matters: both options segments
say nothing and hold a bought contract to its own expiry, so a part that
defaulted the other way would close every option position every afternoon. A
part that inferred "intraday" from the segment's *name* would be guessing, and a
segment added later would inherit the wrong answer in silence.

## The window is set against the broker's deadline, not the exchange's

**Twenty-five minutes**, opening at 15:05 against a 15:30 close. Upstox squares
off open MIS equity positions from roughly 15:15 — fifteen minutes before the
close — so a square-off that merely beat the *close* would be racing an exit the
broker had already begun. Twenty-five leaves ten clear minutes ahead of that,
which at `order_latency_maximum` (5.0 s) is 120 ask-and-answer rounds.

Deliberately wider than `close_out_minutes_before_the_expiry_session_closes`
(15.0), which only has to beat the exchange's own close.

**The 15:15 figure is broker policy and is not verified here.** It is not in
Upstox's instrument master and this project has never placed a live MIS order.
The *mechanism* — finish before the broker starts — is what fixes the shape of
this number; only the deadline itself is assumed, and the setting's own note
says so. Confirm it against Upstox's published square-off timing before this
segment runs in live mode.

## What it reuses

The exits are placed by `runtime/position_exit_placer.py`, the same bound
`position-flattener` learned on 2026-08-30:

    allowance = what is held now - what is already asked and unanswered

Without it that part placed 426 exits against 2 open positions and filled them —
1,048,525 USDT of notional against a 190,900 USDT book. This is the third part
to place exits and the second to reuse that bound rather than write it again.

## The leverage this segment is named for is *not* built here

`leverage_ceiling` is 5.0 in the segment file because that is what the operator
asked for. It is a ceiling this project imposes, not a limit it has verified,
and the real per-stock intraday limit is the broker's — smaller, name by name,
changing daily with SEBI VAR+ELM margins.

**It cannot be read from the instrument master.** Measured 2026-09-05:
`intraday_margin` appears in **zero of the 102,789 rows** of Upstox's real
file, so `InstrumentListing.intraday_margin_percent` is always `None` — a field
the adapter parses that the source never carries.

The real source is Upstox's per-order margin endpoint, which the adapter already
has (`margin_endpoint_url`, `build_margin_quote_request_payload`,
`read_margin_quotes`) and which prices a specific candidate order. Until a part
asks it, `position-sizer` defaults to 1.0x when no `leverage-choice` arrives, so
this segment trades unlevered — safe, and not what it was asked for. That part
is the next piece of bot 3, and it is named here so the gap is visible rather
than assumed closed.

`leverage-selector` cannot be un-parked as it stands: it consumes
`funding-forecast`, a crypto perpetual concept whose only producer is
`funding-rate-forecaster`. The Indian analogue is the broker's own intraday
interest on the borrowed portion, which is a different input and a real piece of
work.

## Verification

    python3 dashboard/check_contracts.py
    .venv/bin/python -m pytest tests/parts/risk_capital_allocation

and live, on the first day an intraday segment runs: `is_intraday` 1,
`ticks_before_the_window` climbing through the session, `squared_off` moving
after 15:05, and `open_positions` at zero before 15:15.
