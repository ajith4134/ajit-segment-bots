# broker-quote-bridge

**Proposed 2026-09-06**, from the first feature walked under the audit temporary
goal (`docs/feature-audit.md`, feature 1 of 29: `market-data-feed`). This is
item 5 of that goal — "name any part a foundational feature is missing to do
its job fully" — answered with a measurement rather than an opinion.

## The gap

`market-quote` has exactly one producer in the whole blueprint:
`venue-quote-stream-reader`, which is a crypto venue part and is off. So:

    venue-quote-stream-reader  (crypto, OFF)
       -> market-quote            never produced
       -> quote-level-sampler     0 received, ever
       -> symbol-quote-frame      never produced
       -> spread-reversion-detector, instrument-selector    both cut off

Measured with `python3 dashboard/audit_feature_dataflow.py market-data-feed` on
2026-09-06: `quote-level-sampler` reads `in 0/1`, and it is one of only two
running parts in the feature with a dead input after the bridge fixes of the
same day.

## Why it matters, in this market specifically

A quote is `instrument-selector`'s **fallback when the last trade is too old to
size against**. `_reference_price` asks the trade first, and if the trade is
older than that symbol's own believable-age bound it asks the resting quote
before refusing (`priced_from_a_quote` on its standing counts exactly this).
`_price_refusal` does the same before it says `REFERENCE_PRICE_IS_TOO_OLD`.

`NormalisedQuote`'s own docstring records why the type was built at all: on the
live run of 2026-08-24, `instrument-selector` refused **525 of 9,945 intents for
a price too old** and none at all for never having seen a price. Those symbols
were captured; nobody had traded them.

Indian options make that the ordinary case rather than the corner. A strike a
few steps out of the money can go minutes without a print while carrying a
perfectly live bid and ask, and the three segment bots select from exactly those
chains. Without this part, every one of them is refused on staleness on Monday's
open while the market is quoting them the whole time.

## Why a bridge and not a new feed

Nothing new is subscribed. `broker-market-feed-reader` already decodes Upstox's
`MarketFullFeed` depth into `BrokerOrderBookUpdate`, whose `levels` carry
`bid_price`, `bid_quantity`, `ask_price`, `ask_quantity` per level — the exact
four fields `NormalisedQuote` needs — and it already publishes them as
`broker-order-book-snapshot`, which `broker-order-book-bridge` reads today. This
part reads the same wire and states the best level of it as a quote.

That makes it the fourth member of an existing family, built the same way:

    broker-market-data-bridge     broker-market-data          -> market-data
    broker-candle-bridge          broker-candle               -> candle
    broker-order-book-bridge      broker-order-book-snapshot  -> order-book-snapshot
    broker-quote-bridge           broker-order-book-snapshot  -> market-quote     (this)

It is a new part rather than more cleverness inside `broker-order-book-bridge`
because T-6 says grow by adding parts, and because the two answer different
questions: one states the whole book for a fill to be walked against, the other
states the top of it as the price a symbol can be believed at. A consumer of one
does not want the other, and the resource governor must be able to shed either
without the other.

## What it does, precisely

- Reads `broker-subscribed-instrument-listing` to resolve `instrument_key` to
  `trading_symbol`, and holds an unresolved update until its listing arrives —
  `runtime/pending_instrument_updates.py`, the same mechanism and the same
  `unresolved_broker_update_hold_limit` setting the other three bridges use, for
  the same measured reason (the connect burst meets an empty listing map).
- Takes the **best** bid and the **best** ask across the update's levels, by
  price, rather than trusting `levels[0]` — the same decision
  `broker-order-book-bridge` already made and documented.
- **Drops a zero-priced level before choosing.** Upstox pairs bid and ask at the
  same depth index and pads the thinner side with `price=0, quantity=0`; taking
  the minimum ask over a padded level would report a best ask of 0, which is the
  exact bug fixed in `broker-order-book-bridge` on 2026-09-02 and would poison
  every consumer of the mid.
- **Publishes nothing when either side is absent.** `NormalisedQuote.mid_price`
  is `(bid + ask) / 2` with no notion of a one-sided market, so a quote built
  from a book with no ask would state a mid halfway to zero. A genuinely
  one-sided book is a real state of a thin option chain and the honest answer is
  no quote, not a fabricated one.
- `venue_time_ns` is Upstox's own `broker_time_ns`, never arrival time — the
  whole value of a quote is its age, and an age measured from when we read the
  socket is our latency rather than the market's (`NormalisedQuote`'s own rule).

## What it does not do

It does not merge one-sided deltas across updates. Upstox sends full depth per
update rather than deltas (`broker-order-book-bridge`'s own verified note), so
there is nothing to merge and `quote-level-sampler` already handles the stalest-
side stamp for sources that do.

## Contract

| | |
|---|---|
| consumes | `broker-subscribed-instrument-listing`, `broker-order-book-snapshot` |
| produces | `market-quote`, `part-health` |
| category | `market-data-feed` |
| resource class | bandwidth-bound |
| rate risk | changes-the-answer |
| skipped tick | delays |

No new data type: `market-quote` already exists and already has a consumer
waiting for it.
