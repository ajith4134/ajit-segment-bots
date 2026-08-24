# The price a quiet symbol has: all-market quote streams, measured

Taken 2026-08-24 16:2x-16:45 UTC against both live venues, read-only, no orders.
Every number below came from one of the three scripts beside this file; each
writes what it measured to the `.txt` beside it. Nothing here is quoted from
documentation -- the routing fact in particular is one the docs state and the
first run of it got wrong.

## Why it was measured

`instrument-selector` reads `symbol-price-frame`, which `price-level-sampler`
computes from `market-data` -- trades. A symbol that has not traded has no fresh
price, so sizing against it is refused. Read from the live heartbeat table at
16:2x:

    intents_seen              9945
    chosen                    9420
    refused_for_a_stale_price  525   (5.3%)
    refused_for_no_price_ever    0

`refused_for_no_price_ever` is zero, which is the whole point: this is not a
coverage gap. Every refusal is a symbol whose price we hold and whose price is
old, because nobody traded it. A last-trade feed re-delivered more often cannot
fix that -- it re-sends the same old number. A resting bid and ask is a live fact
for a symbol nobody is trading, and that is what was measured.

## Binance USDⓈ-M: the route decides whether a stream exists

`measure_binance_stream_routes.py`, 10 s per cell, `binance-stream-routes.txt`:

| stream | /ws | /market/ws | /public/ws |
|---|---|---|---|
| `!miniTicker@arr` | 0 symbols, 0 msgs | **608 symbols, 10 msgs** | 0 symbols, 0 msgs |
| `!ticker@arr` | 0 symbols, 0 msgs | **547 symbols, 10 msgs** | 0 symbols, 0 msgs |
| `!bookTicker` | **693 symbols, 1054 msgs** | 0 symbols, 0 msgs | not reached |

Each stream lives on exactly one route and is silent on the others -- open, no
error, no data, which is the hazard `runtime/venues/binance_usdm.py` already
documents for `/market` streams. The first run of this measurement used `/ws`
for all three and concluded `!miniTicker@arr` did not exist. It does; the route
was wrong. That is recorded here because the failure is invisible by design.

`!bookTicker` carries the best bid and ask for the whole market on one socket:

    {"e":"bookTicker","u":11376410367680,"s":"BTCUSDT","ps":"BTCUSDT",
     "b":"79759.90","B":"7.070","a":"79760.00","A":"3.158",
     "T":1787588643663,"E":1787588643663,"st":1}

`T` is the venue's own transaction time, so the age of a quote is the venue's
fact rather than ours -- the same discipline the tape keeps for a print.

## Binance: how quiet the quietest symbol is

`measure_binance_quote_gaps.py`, one `!bookTicker` socket, 99 s,
`binance-quote-gaps.txt`:

    768 symbols quoted, 10,577 updates
    median symbol's worst quote gap    8.3 s
    p95 of worst gaps                 26.7 s
    worst                             62.2 s  (GDXUSDT)
    quoted exactly once in the window      6 symbols

The tail is tokenised-equity perpetuals -- GDXUSDT, NAVERUSDT, SAMSUNGEMUSDT,
LGELECTRONICSUSDT -- which is the right tail to find there, and still seconds
rather than the minutes an illiquid perpetual goes between trades.

## Bybit linear: no wildcard, and it does not need one

`measure_bybit_universe_tickers.py`, 100 s, `bybit-universe-tickers.txt`:

    833 trading linear symbols; args array is 17006 chars vs the 21,000 cap
    subscribe ack: success=True
    window 100s: 833 of 833 symbols quoted, 119,076 updates
    never quoted in the window: 0

Bybit has no all-symbol topic, so every symbol is named. The venue's cap is on
characters rather than topics, and the whole universe serialises to 17,006 of
the 21,000 -- **81% of the cap, with 19% of headroom**. That headroom is a
measurement of today's symbol names, not a guarantee: a handful of long new
listings breaks it. The adapter already owns `does_topic_fit_connection`, and a
part built on this must ask it and open a second connection when the answer is
no, never assume one connection is enough.

Coverage was complete: every one of the 833 quoted, none silent. The worst
per-symbol gaps were 83.5 s (BIIBUSDT), 74.3 s (USDEUSDT), 65.2 s (POPMARTUSDT)
-- again the tokenised-equity tail.

## The rate, which decides the shape of what reads this

    Bybit quotes     1,190/s     against ~224/s of trades at 100 symbols
    Binance quotes     ~107/s

Quotes arrive faster than trades on Bybit by a factor of five. Handing them to
readers one at a time re-creates exactly the fan-out that
`apply_2026-08-24_sampled_price_levels.py` removed, so anything built on this
publishes on a cadence rather than per update.

## The all-market stream is throttled — measured 2026-08-24 20:2x

`measure_quote_stream_throttling.py`, two sockets, same symbol, same 30-second
window:

    per-symbol btcusdt@bookTicker   6,598 updates for BTCUSDT   (219.9/s)
    all-market !bookTicker              6 updates for BTCUSDT   (  0.2/s)

**About a thousand times fewer per symbol.** `!bookTicker` covers the whole
market on one socket, and that is a fact about coverage rather than about
freshness — a quote's entire value is its age, and one arriving every five
seconds is stale before it lands.

This overturns a choice made earlier the same day. The quote feed was built on the
all-market topic and `stream-budget-planner` collapsed its quote requests to it,
on the reasoning that planning 872 per-symbol topics and then opening one would
make the plan's own connection count fiction. That reasoning was sound and the
conclusion was wrong: the plan was honest and the quotes were useless.

What it cost, measured on the live run before the reversal: giving
`spread-reversion-detector` the quote fallback moved its refusals from 50% of
tests to 45%, when the defect it was built to fix was 34-50% of all its work.
469 legs a second really were priced from a quote — the mechanism worked — but
against the 1.0-1.5 s that a captured symbol's own moves say a price may be
believed, a five-second-old quote is refused just as a stale trade is.

Two things follow, and both are recorded rather than assumed:

* **Freshness is per symbol.** The plan names the symbols decisions are made on,
  for quotes exactly as for trades. Whole-universe coverage is still available —
  `every_symbol_quote_topic` remains the venue's answer for it — but it answers a
  different question.
* **Bybit was never affected.** It has no wildcard, so its captured symbols were
  always subscribed as `tickers.{SYMBOL}` and its quotes were always per-symbol.
  The defect was Binance-only, and it was invisible until the two were measured
  side by side rather than each on its own.
