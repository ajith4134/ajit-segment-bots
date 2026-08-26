# The feed drops a symbol the bot is holding

**Proposed 2026-08-26, after a position went a day without a price.**

## What happened

`symbol-catalogue-reader` selects the `captured_symbol_count` (50) highest-volume
symbols per venue, and re-selects on every catalogue refresh —
`symbol_catalogue_refresh_interval`, 900 seconds. A symbol whose 24-hour volume
slips below rank 50 is dropped from `symbol-universe`, `stream-budget-planner`
stops planning a stream for it, and the readers stop capturing it.

Nothing in that path knows whether the bot is holding the symbol.

Measured 2026-08-26. The bot holds **STORJUSDT on both venues**. Its tape stops at
`2026-08-25 18:40` on binance-usdm and `18:43` on bybit-linear; there is no
`2026-08-26` file for it at all, while `BTCUSDT` was current to the minute. On the
trade board both STORJUSDT rows read:

    price now      NOT MEASURED: the tape has no record for this symbol today
    unrealised     —
    peak / worst   —

## Why this is not a display problem

A position that cannot be priced cannot be managed, and every consequence is
silent:

- **It cannot be stopped out.** `paper-fill-simulator` triggers a resting stop
  when a live price crosses it. No prices arrive, so the stop never triggers, and
  a stop that cannot trigger is not protection — it is the appearance of it.
- **Its excursion stops accruing.** `peak-excursion-tracker` measures against
  arriving trades, so the position's peak and worst freeze at the last price seen.
  When it eventually closes, the record of how it travelled is wrong, and
  `signal-excursion-profiler` learns its quantiles from that record.
- **It is invisible to risk.** `exposure-limiter` values the book at the last
  price it saw; `margin-liquidation-watch` measures headroom against it.
- **Nothing reports it.** `feed-coverage-auditor` audits coverage of
  `symbol-universe` — and the symbol is no longer in the universe, so coverage
  reads complete. The gap is outside what the auditor is asked to look at.

The system was, in effect, holding a position it had stopped watching, and every
board said it was fine.

## What the fix must not be

**Not "capture more symbols".** Raising `captured_symbol_count` moves the cliff
without removing it, and it costs streams on a machine whose stream budget is
already planned against hardware capacity (`open_files_required` 204 against
`open_file_headroom` 128).

**Not "never drop a symbol".** The universe rotating with volume is the point:
the bot scans where the volume is. A universe that only grew would end up
subscribed to every symbol either venue has ever listed.

**Not a special case inside the readers.** Each reader consumes `stream-plan` and
should keep doing exactly what the plan says. A reader that second-guessed its
plan would be a reader the planner cannot govern (T-2).

## What is proposed

`symbol-catalogue-reader` consumes `position`, and **a symbol the bot holds is
kept in the universe whatever its volume rank**.

The rank cut stays exactly as it is for everything else. A held symbol is added
back after the cut, so the universe is "the top N by volume, plus whatever we are
still holding". It leaves as soon as the position is flat and its volume has not
recovered — which is the ordinary rotation, not an exception to it.

**Why the catalogue reader and not the stream budget planner.** The planner can
only plan streams for symbols that reach it, and the cut happens before that.
Putting it in the planner would mean passing the whole 746-symbol catalogue
downstream so the planner could re-add one — moving the decision without moving
the knowledge.

**Why `position` and not `open-position` or a new type.** `position` is what
`fill-reconciler` already publishes on every tick for everything held, and it
carries `is_flat`. Nothing new has to be produced for this; a type invented here
would be a second statement of a fact that already travels.

## The cost

One more consumer on `position`, which is already published at every tick, and one
more input for a part that currently consumes nothing. The universe grows by at
most the number of symbols held that have fallen out of the top N — bounded by the
number of open positions, which the capital desk already bounds.

## What proves it

A held symbol below the volume cut stays in the universe; the same symbol, once
flat, leaves on the next refresh. Both against a real captured catalogue, since
the ranking has to be the venue's own (RL-063).

Live: STORJUSDT reappears on the tape and its board row prices again.
