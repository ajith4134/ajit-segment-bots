# implied-vol-reader reads the broker's own option chain

**Proposed 2026-09-06**, walking `execution-venue-adapter` and
`paper-live-trading` under the audit temporary goal. This is item 3 of that goal
— "convert or replace, never leave in place" — applied to a part whose *core is
already right* and whose *input is still crypto*.

## The gap

`implied-vol-reader` has read nothing, ever. Measured on the live spine
2026-09-06:

    implied-vol-reader   reads 0   quotes_seen 0   options_feed_connected 0   surfaces_published 0

Its `start_part` drains `market-data`, throws it away and returns no read
requests at all. That is not a bug — it is a deliberate stub, and the part's own
docstring says why: *"Phase 1 has no options feed, and this part says so rather
than producing anything: options are a segment this system has not built
(RL-050)."*

RL-050 was the **crypto** build order: futures first, spot and options skeleton.
That ordering is retired. `docs/goal.md`'s Phase A is *index options and stock
options, both of them, fully, before anything else moves* — so the one segment
this part was told not to serve is now the first two segments in the plan.

## The data is already on the bus

Upstox publishes option greeks, implied volatility included, for every
subscribed contract. `BrokerOptionGreeks` carries `delta, theta, gamma, vega,
rho, implied_volatility, broker_time_ns`, and `broker-market-feed-reader`
already publishes it: 1,791 messages received by
`expiry-day-zero-to-hero-detector` in one short window.

Everything else the reader needs is also already flowing:

| the reader needs | already produced by |
|---|---|
| implied volatility per contract | `broker-option-greeks` (broker-market-feed-reader) |
| strike, expiry, CE/PE, underlying | `broker-subscribed-instrument-listing` (subscribed-instrument-listing-filter) |
| a two-sided bid/ask per contract | `market-quote` (broker-quote-bridge, built earlier today) |
| the underlying's spot | `symbol-price-frame` (broker-underlying-price-frame-bridge) |

Nothing new is fetched or subscribed. This is a rewiring of an existing part
onto types that are already carrying.

## Who is waiting for it

`implied-vol-surface` is consumed by `instrument-selector` — the part that picks
which contract the options bots actually trade — and by
`volatility-gap-detector`, which is one of the seven `entry-candidate`
producers. That detector currently reports `no_implied_surface: 2057` against
`tests: 2672`: it is running, testing, and refusing on a missing surface every
time.

## Why convert rather than add a part

The reader's core is not crypto-shaped at all. `ImpliedVolReader`,
`OptionQuote` and `ImpliedVolSurface` know nothing about a venue: they take
quoted options with venue-published implied volatilities and produce a surface
whose holes stay visible. Every rule in that core is exactly what an Indian
option chain needs — a strike with no two-sided quote is not a data point, a
stale quote is dropped rather than carried, a thin surface is published as thin
rather than smoothed. Only `start_part` is crypto-era, and only because it was
told there was no feed.

Adding a second part would leave a dead one behind and split
`implied-vol-surface` across two producers for no reason (T-6 is about growing
by adding parts, not about duplicating one that already fits).

## One real defect the conversion exposes

`observe_quote` does `self._quotes.setdefault(key, []).append(quote)` — an
append-only list per underlying, cleared only by `release()`. That was harmless
while the part received nothing. Fed a live chain at hundreds of quotes a
second it grows without bound for the life of the process, and the staleness
filter does not help because it runs at read time and leaves the dropped quotes
in the list.

A quote is a level: a contract's newer quote supersedes its older one, and
holding both cannot change the surface (the by-strike dict is last-write-wins)
while it can exhaust the machine. Changed to keep the newest quote per contract
per underlying.

## Contract change

| | before | after |
|---|---|---|
| consumes | `market-data` | `broker-option-greeks`, `broker-subscribed-instrument-listing`, `market-quote`, `symbol-price-frame` |
| produces | `implied-vol-surface`, `part-health` | unchanged |

## What it still refuses to do

Everything the original refused. It does not compute an implied volatility from
a price — it reads the one the broker published, because a locally-solved IV is
the easiest number in the system to fake and nothing downstream could tell. A
contract with no two-sided quote, no published IV, or a stale quote is dropped
and counted, and an underlying whose chain is too thin to interpolate is
published as `too-few-two-sided-quotes-to-form-a-surface` rather than smoothed
into completeness.

`options_feed_connected` becomes true only when greeks have actually arrived, so
"no feed" stays a reachable, honest state rather than a line that can no longer
be reached.
