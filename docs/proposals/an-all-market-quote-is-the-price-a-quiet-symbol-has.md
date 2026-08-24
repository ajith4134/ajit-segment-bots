# An all-market quote is the price a quiet symbol has

Proposed by Claude 2026-08-24, from a question the user asked the same day:
*"instead of using stale symbol prices can't we use the live price so no symbols
will be refused"*, pointing at their own `ajith-ai-crypto-trading-bot`, which
carries the whole futures universe in one process off a single all-market socket.

The measurements behind every number here are in
`measurements/2026-08-24-all-market-quotes/`, taken live against both venues.

Scope, chosen by the user: **pricing only.** The tape, the trade streams and the
symbol walk are untouched. This changes what a decision is priced against; it does
not change what is captured or how much of the universe is captured.

## The problem, measured

`instrument-selector` reads `symbol-price-frame`, which `price-level-sampler`
computes from `market-data` -- trades, and nothing else. From the live heartbeat
table at 16:2x on 2026-08-24:

    intents_seen              9945
    chosen                    9420
    refused_for_a_stale_price  525   (5.3%)
    refused_for_no_price_ever    0

**`refused_for_no_price_ever` is zero, and that is the finding.** A coverage gap
would land there. It does not. Every one of the 525 refusals is a symbol whose
price this system holds, and whose price is old because *nobody traded it*.

So the walk from 30 to 50 to 100 symbols per venue cannot fix these refusals, and
neither can capturing all 833: the symbols being refused are already captured. The
missing thing is not more symbols. It is a different **fact**.

## Why a faster last-trade feed is the wrong fix

The obvious answer is to re-deliver the last price more often -- Binance's
`!miniTicker@arr` pushes the whole market once a second on one socket, measured at
608 symbols. But `c` in that payload is the *last traded price*. For a symbol that
has not traded in ten minutes it is the same ten-minute-old number, re-sent.

Stamp that arrival as the moment the price was true and the 525 refusals go to
zero while every one of the underlying prices is exactly as old as it was. That is
the 2026-08-23 ENAUSDT failure -- a trade decided at a fifty-six-minute-old price
-- with its alarm removed rather than its cause. This codebase already refuses the
trick: commit `852ef4d`, *a re-delivered price is one fact*.

**A refusal count that falls because the guard stopped looking is worse than the
refusals.** The 525 are the system correctly declining to size a position against
a number it cannot stand behind.

## What is actually live for a symbol nobody is trading

Its resting bid and ask. A quote exists whether or not anyone trades, and it
carries the venue's own timestamp, so its age stays honest.

Both venues serve the whole universe on one connection. Measured:

| | Binance USDⓈ-M | Bybit linear |
|---|---|---|
| how | `!bookTicker`, one socket | `tickers.{symbol}` x every symbol, one connection |
| symbols covered | 693-768 | **833 of 833, none silent** |
| updates | ~107/s | 1 190/s |
| median symbol's worst gap | 8.3 s | -- |
| p95 of worst gaps | 26.7 s | -- |
| worst | 62.2 s (GDXUSDT) | 83.5 s (BIIBUSDT) |

The tail on both is tokenised-equity perpetuals, and it is seconds where the trade
tape's tail is minutes.

Against this, the staleness bound is already per symbol and already learned
(`runtime/price_staleness.py`): a quiet symbol moves less per second, so it
tolerates an older price. Quotes at these gaps sit inside that bound for the
symbols now being refused. **Refusals should fall a long way and must not reach
zero** -- zero refusals means the guard is off, and this proposal is judged on
that pair of numbers together, never on the refusal count alone.

## Two venue facts this rests on, both of which bite

**Binance routes streams, and a wrong route is silent.** Measured across all three
routes: `!bookTicker` answers only on `/ws`; `!miniTicker@arr` and `!ticker@arr`
answer only on `/market/ws`; every other cell returned an open connection, no
error, and no data. The first run of that measurement used `/ws` throughout and
concluded `!miniTicker@arr` did not exist. `runtime/venues/binance_usdm.py`
already documents this hazard and confines it to `stream_endpoint_url`; the quote
stream is one more entry there, and it is a *different* route from `@depth`.

**Bybit has no wildcard and 19% of headroom.** The whole universe serialises to
17,006 characters against a documented 21,000-character cap on the `args` array --
81% used. That is a fact about today's symbol names, not a guarantee. The part
must ask `does_topic_fit_connection` and open a second connection when the answer
is no. A part that assumes one connection is a part that silently drops the tail
of the universe on the day a few long names list.

## What changes

Two new parts and two new data types, mirroring the trade path exactly, because
the fan-out argument that produced `price-level-sampler` applies here with more
force: Bybit's quotes arrive five times faster than its trades.

| | |
|---|---|
| `market-quote` (data type) | one symbol's best bid and ask on one venue, with the moment the venue stamped it |
| `symbol-quote-frame` (data type) | every symbol's latest bid and ask on one venue, published on a fixed cadence |
| `venue-quote-stream-reader` (part) | consumes `stream-plan`, `venue-standing`; produces `market-quote`, `part-health` |
| `quote-level-sampler` (part) | consumes `market-quote`; produces `symbol-quote-frame`, `part-health` |
| `instrument-selector` | consumes `symbol-quote-frame` in addition to `symbol-price-frame` |

`StreamKind` gains `QUOTE`. It is stored as one byte in the tape index, so a new
value costs nothing to existing files.

**Why two parts rather than one.** Reading a socket is bandwidth-bound and
publishing a cadence is CPU-bound; the governor switches them separately, and T-6
says grow by adding parts rather than by making one cleverer. It is also the shape
already proven on the trade path.

**Why the sampler is not optional.** 1,190 quote updates a second handed to every
reader is precisely the fan-out that `apply_2026-08-24_sampled_price_levels.py`
removed from the trade path. Publishing per update would undo that work on a feed
five times louder.

**Why the quote is not written to the tape.** The tape is what the venue printed,
and this phase's scope is pricing. A quote tape is defensible later and is a
separate decision with its own storage cost -- 1,190 records a second is more
than the trades this system writes today.

## How it is judged

Not by the refusal count alone. Before and after, on the live run:

1. `instrument-selector`: `refused_for_a_stale_price` falls substantially and
   `chosen` rises by about the same, with `refused_for_no_price_ever` still zero.
2. The refusal count does **not** reach zero. If it does, the bound is being
   satisfied by a quote that is itself stale, and that is a defect.
3. `Decision freshness` on the trade board holds at or below where it is now.
4. Worst part staleness does not move off the stream reader's own tick, and
   input loss stays at none -- the same two numbers the symbol walk is judged by,
   because this adds a feed to a system whose failure mode is fan-out.
