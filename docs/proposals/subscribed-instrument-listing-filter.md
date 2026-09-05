# subscribed-instrument-listing-filter

**Proposed 2026-09-05.** Ten of the fifteen parts that consume
`broker-instrument-listing` can only ever use a listing for an instrument the
feed is actually subscribed to. They read the whole NSE instrument master
instead -- 102,940 rows against 2,000 subscribed -- and the conveyor that
carries it is dropping one delivery in seven.

## The measurement

`a8bdbb0` (2026-09-04) fixed the master being spoken once an hour into a
212,992-byte buffer that holds 532 of it: `broker-instrument-catalogue-reader`
now restates all 102,940 listings evenly, forever, at
`broker_catalogue_restatement_cycle_seconds`. Seventeen parts receive an input
none of them had, and the same commit named what it did not act on -- that
almost none of them want the whole master.

Live spine, 2026-09-05, 35 minutes after start:

    broker-instrument-catalogue-reader
      broker-instrument-listing   published 571,464   not delivered 77,477   13.6%

    broker-market-feed-reader     subscribed_instruments  2,000

    15 consumers of broker-instrument-listing, each drained at the full
    restatement rate:

    broker-underlying-price-frame-bridge   broker-order-book-bridge
    broker-symbol-universe-bridge          broker-market-data-bridge
    broker-history-reader                  broker-market-feed-reader
    bear-feature-builder                   broker-candle-bridge
    bull-feature-builder                   expiry-day-zero-to-hero-detector
    tail-crowding-detector                 exchange-announcement-reader
    instrument-selector                    cross-segment-signal-bridge
    corporate-action-adjuster

`broker-instrument-listing` is the second-worst undelivered type on the whole
spine, behind `cost-estimate`. Nothing reports it as a fault: every drop is an
inbox that was full when a datagram arrived, which is a decision the socket is
entitled to make.

## Why 2,000 is the right number, and 153 is not

The obvious narrowing is `symbol-universe` -- the ~153 symbols
`broker-symbol-universe-bridge` selects and the segment actually trades. It is
the wrong bound, and `expiry-day-zero-to-hero-detector` is the part that proves
it: its whole job is a far-out-of-the-money contract cheap enough to spike, and
the universe is ranked by distance from the underlying's own price and capped
per underlying. The contracts it exists to find are exactly the ones an
ATM-ranked cap excludes.

The master is the wrong bound in the other direction. Every one of the ten parts
below resolves a listing against something the **feed** delivers -- an LTP, a
greek, an open-interest reading, a depth snapshot. `broker-market-feed-reader`
subscribes 2,000 instruments, so a listing outside that set can never be joined
to anything:

| part | what it does with a listing | why unsubscribed is useless |
|---|---|---|
| `broker-market-data-bridge` | `instrument_key -> trading_symbol` | keys arrive only from the feed |
| `broker-order-book-bridge` | the same map for depth | the same |
| `broker-candle-bridge` | the same map for bars | the same |
| `broker-underlying-price-frame-bridge` | keeps tracked underlyings only | a frame needs a price |
| `bull-feature-builder` | `UnderlyingOpenInterestAggregator` | OI is published per subscribed contract |
| `bear-feature-builder` | the same aggregator | the same |
| `cross-segment-signal-bridge` | the same aggregator | the same |
| `tail-crowding-detector` | the same aggregator | the same |
| `instrument-selector` | `AtmStrikeTracker` -- premium and delta | both arrive only for subscribed contracts |
| `expiry-day-zero-to-hero-detector` | `detect` refuses `NO_PREMIUM`/`NO_DELTA` | the same two inputs |

The bound is therefore not what the segment trades and not what the exchange
lists. It is **what the feed is subscribed to**, which is a fact only the feed
reader knows.

## Five parts stay on the master, deliberately

- `broker-symbol-universe-bridge` -- it *selects* the universe from the master.
  Narrowing its input to the selection would be circular.
- `broker-market-feed-reader` -- it *makes* the subscription. It takes
  `symbol-universe` first and fills the rest of the connection from the master
  (`subscribe_the_universe_first`, `dfc5ff1`), so it must keep seeing rows it has
  not subscribed yet.
- `corporate-action-adjuster` -- a bonus or split is announced on any listed
  name, subscribed or not.
- `exchange-announcement-reader` -- a delisting or a rule change is likewise
  about the exchange, not about this project's subscription.
- `broker-history-reader` -- fetches history over REST for one named chain. It
  is not on the feed path at all, and tying its reach to a live subscription
  would couple two unrelated things.

`broker-news-reader` and `news-symbol-resolver` are declared consumers that are
not on the spine; both are announcement-shaped and stay on the master for the
same reason as the two above.

## What is added

**`broker-subscription-state`** (new data type) -- produced by
`broker-market-feed-reader`: the instrument keys it currently has subscribed,
and when it looked. A level, published when the set changes.

Not derived from `symbol-universe` plus the nearest-expiry rule, which is what
the feed reader itself uses to *choose*. Re-deriving the choice elsewhere would
be a second opinion free to disagree with the real one; the subscribed set is
measured and stated by the part that owns it (Rule 8).

**`subscribed-instrument-listing-filter`** (new part, block `broker-adapter`) --
consumes `broker-instrument-listing` and `broker-subscription-state`, produces
**`broker-subscribed-instrument-listing`**: the same `InstrumentListing` payload,
restated for subscribed instruments only, on its own conveyor with its own
restatement cycle.

A new part rather than a second `produces` on an existing one:

- T-6. `broker-instrument-catalogue-reader` fetches and restates the master;
  `broker-market-feed-reader` holds a connection open. Neither grows a second
  audience.
- The filter is switchable and its being off costs the feed nothing (T-2/T-3).
  A conveyor bolted onto the io-bound part whose `skipped_tick_effect` is
  `corrupts` is the one place a restatement loop must not live.
- Restatement is its own job, with its own rate, learned once already: a level
  restated all at once overruns every inbox that reads it, and a level restated
  once an hour reaches nobody who restarted (`a8bdbb0`).

**The filter holds the whole master**, the same as the catalogue reader and the
feed reader already do, and emits the subscribed subset from it. Holding only
the rows that were subscribed *when they passed* would be cheaper and wrong: a
contract subscribed a minute after its row went by would then wait a full
restatement cycle to reach anyone, and expiry-day contracts are subscribed on
expiry morning, which is precisely when the detector needs them. The price is
one more copy of a 102,940-row table, stated here rather than discovered later.

## What this is expected to change

Delivery attempts for the master fall from fifteen consumers to five. The
narrowed type carries 2,000 rows to ten consumers on its own cycle -- roughly
fifty times fewer rows each. The undelivered figure above is what to re-measure
against; the fix is wrong if it does not move.

It changes no part's judgement. Every one of the ten resolves the same listing
for the same instrument key it resolves today -- it simply stops being offered
100,940 rows it can do nothing with.

## Verification

    python3 dashboard/check_contracts.py
    python3 dashboard/check_payload_reads.py
    python3 dashboard/check_part_calls.py

and, once the code follows, the live figures above re-read from the same
heartbeat table: `not delivered` for `broker-instrument-listing`,
`subscribed_instruments`, and each rewired part's own listing receipts.
