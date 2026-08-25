# `market-data` carried three shapes, and a candle crashed a trade reader

**Proposed by Claude, 2026-08-25, minutes after `ccxt-venue-reader` was restarted
with the repaired tape.**

## What happened

`feed-gap-detector` began restarting every two seconds. It reads
`trade.sequence`, and the payload it was handed had no sequence: it was a
`NormalisedCandle`, published on `market-data` by `ccxt-venue-reader`, which had
just started for the first time since phase 6.

Thirty parts consume `market-data`. Classified by the fields they actually read,
they fall into three groups:

| shape | fields read | parts |
|---|---|---|
| a trade | `price`, `quantity`, `venue_time_ns`, `sequence`, `quote_volume` | 12 |
| a candle | `open`, `high`, `low`, `close`, `volume`, `is_closed`, `open_time_ns` | 3 |
| a book | `asks`, `bids` | 3 |

The payload-read checker cannot see this: it asks whether a field exists on
*some* type a producer can publish, and on a wire with three shapes every field
exists on one of them. A wire with one name and three shapes defeats the check by
construction, which is a better reason to split it than the crash is.

## The change

    ccxt-venue-reader      produces  candle          (instead of market-data)
    kline-window-builder   consumes  candle          (instead of market-data)
    feed-jump-detector     consumes  candle          (instead of market-data)
    historical-bar-store   consumes  candle          (instead of market-data)

    ground-truth-snapshot-builder  consumes += order-book-snapshot
    market-anomaly-detector        consumes += order-book-snapshot
    symbol-profile-store           consumes += order-book-snapshot

`market-data` keeps its name and means what twelve of its consumers already
assume: one trade, as a venue printed it. A candle is not a trade -- it is a
summary of many, and the two are only the same shape if you never look at the
fields. The book already has its own type (`order-book-snapshot`, produced by
`order-book-reader`), and the three parts reading `asks` and `bids` off
`market-data` were reading a field no trade has ever carried.

## What this does not change

The tape. Candles are recorded on their own per-kind tape files as of the same
day, and this is the bus half of that same separation: one writer per file, one
shape per wire.
