# clock-skew-monitor watches the broker's clock

**Proposed 2026-09-06**, walking `observability` — feature 15 of 29 under the
audit temporary goal. Item 3: convert or replace, never leave in place.

## The gap

`clock-skew-monitor` consumes exactly one type, `raw-venue-order-status`, whose
only producers are `ccxt-order-router` and `order-state-poller` — both crypto,
both off. So the part is off, and the only thing in this project that would
notice its own clock drifting has never run.

Its own docstring says why that matters: *"the failure is quiet at first — an
occasional rejection that looks like bad luck — and then total: once drift
passes the window, every private order fails and the system is unable to trade
while appearing entirely healthy."*

## Half of it is already venue-agnostic

`observe_venue_time(venue_id, venue_time_ns, local_time_ns)` takes any stamped
message and records the offset. It knows nothing about crypto. Only the *input*
was crypto-shaped.

`broker-market-data` carries `broker_time_ns` — Upstox's own stamp, on every LTP
update, at thousands a second. That is exactly the reading this half wants, and
it is already on the bus.

## Why this is worth doing rather than deferring

Two timestamp traps were found in this project **on one day**, 2026-09-06:

- Upstox's historical rows are `+05:30`, and read as UTC every bar of the Indian
  session lands outside it (already carried in `read_historical_candles`).
- NSE's intraday chart writes IST wall-clock *as though it were an epoch* —
  measured, 3.88% disagreement against this project's own tape when read as UTC,
  0.30% when shifted back.

Both were caught by hand. Neither would have been caught by anything running. A
part that continuously measures the distance between this machine's clock and
the broker's is the thing that catches the next one, and it exists already.

## The change

| | before | after |
|---|---|---|
| consumes | `raw-venue-order-status` | `broker-market-data`, `raw-venue-order-status` |
| produces | `alert`, `part-health` | unchanged |

The rejection half is kept, not deleted. `raw-venue-order-status` is how a venue
*says* "your timestamp is wrong", and that remains the sharpest signal the day a
real order path exists. Today it carries nothing, which is honest; the offset
half carries continuously.

## What it still refuses to do

It does not correct anything. Drift is measured from the broker's own stamps
because a local clock cannot detect that it is the one that is wrong, and the
part's job is to say so early enough for a person to fix it — not to quietly
adjust a number and make the drift invisible.
