# The catalogue hurries the subscribed rows

**Origin:** 2026-09-15, the first live session after the 2026-09-12 scope change. The
operator: "fix all te issues found one by one".

## What was measured

`broker-instrument-catalogue-reader` restates Upstox's master (118,388 rows) evenly over
`broker_catalogue_restatement_cycle_seconds` (1,800 s). `subscribed-instrument-listing-filter`
can only restate a subscribed instrument to its ten consumers once that instrument's master
row has come round, so the ~2,000 subscribed rows arrived no faster than the other 116,000.

Live, eight minutes after the 05:28 UTC start:

| part | reading |
|---|---|
| `subscribed-instrument-listing-filter` | `unlisted_subscribed_instruments` 1,414 of 2,000, `cycles_completed` 0 |
| `broker-market-tape-writer` | `symbols_resolved` 184; 834,410 of 857,482 records (97%) written under raw instrument keys |
| `expiry-day-zero-to-hero-detector` | `instruments_known` 186 on a NIFTY expiry day |

A restart during market hours therefore costs up to half an hour of every name-joined
reading, and the spine restarts after every change a live part imports.

## What changes

The catalogue reader consumes `broker-subscription-state` (bounded by
`broker_subscription_state_maximum_age`, like the filter's own reader) and restates the
subscribed rows of the master on a second conveyor at
`subscribed_listing_restatement_cycle_seconds` (300 s), beside the whole master at its own
pace. Both are paced conveyors, so the added rate is 2,000 / 300 = 6.7 listings a second
against the 532 one socket buffer holds.

## What it does not do

It does not re-derive the subscription (the feed reader states it), change the master's
own cycle, or change what the filter restates. Parts that are not joined to the feed still
receive the whole master.

## Proof

`tests/parts/broker_adapter/test_broker_instrument_catalogue_reader.py::test_the_subscribed_rows_of_the_real_master_are_said_within_their_own_cycle`
drives the real master: 2,000 subscribed rows are all said within 301 s, while the whole
master still turns at its own rate.
