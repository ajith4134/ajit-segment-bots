# Three segments on one spine

**Proposed 2026-09-05**, for the temporary goal in `docs/goal.md`: index options,
stock options and cash equity intraday all paper trading on live data by Monday
2026-09-07. Operator chose the fan-out over three spines or a rotation.

## What the spine does today

`runtime.toml [segment_id]` is one value, `"index-options"`. Twenty-six parts read
it, `operate/run_live_spine.py` holds `live-spine.lock`, and every per-segment
number -- the capital allotment, the money mode, the paper account, the leverage
ceiling, the squared-off-daily rule -- is that one segment's.

So a second segment is a settings change today (`6aff93c`), and a second segment
**at the same time** is not possible at all.

## Why not three processes per part

The obvious fan-out gives each per-segment part one process per segment, named
`position-sizer@stock-options`. Measured against the real blueprint, it cannot be
built: the longest inbox address is already

    /run/user/1001/ajit-segment-bots/broker-underlying-price-frame-bridge.broker-subscribed-instrument-listing

at **106 of the 107** bytes `sun_path` allows. One byte of headroom, and that part
is itself one of the twenty-six. A segment suffix costs twelve to twenty-two.

Shortening the inbox root buys twelve bytes and would work, but it renames every
address on the bus two days before the deadline, and it triples the process count
on a box where 319 parts already cost about 8.6 cores steady.

## What is built instead

**The segment stops being a property of the spine and becomes a property of the
message.** No new part, which is what the goal asks for, and no address changes.

1. `[built_segments]` in `runtime.toml` lists the segments this spine trades.
   `segment_id` stays as the name of the one whose settings are the default for
   anything not yet keyed, and is retired when nothing reads it.
2. The three feed-side parts take the **union** of the built segments'
   `segment_underlying_trading_symbols` instead of one segment's.
3. `instrument-selector` already accepts `built_segments` as a tuple and is handed
   a one-tuple today. It is handed the real list, and `SEGMENT_OF` stops mapping
   every `OPTION` to `index-options`: an option is index or stock by whether its
   underlying is in the index segment's own symbol list -- settings, not a literal
   (RL-061). Cash equity is the `SPOT` instrument type on an intraday segment.
4. `instrument-choice` carries the segment it chose in, and every payload
   downstream of it carries that segment through to the fill and the position.
5. The capital and risk parts key their state by that segment rather than by the
   global: allotment, bounds, leverage ceiling, money mode, paper account, halt,
   exposure, drawdown, fund lock, PnL. Each publishes one level per segment under
   a `LatestByKey` **with an age bound** -- the 2026-08-26 trap.

## What this deliberately does not do

- **It does not make the equity segment the full NSE universe.**
  `segments/cash-equity-intraday.toml` lists fourteen F&O-eligible symbols.
  `docs/goal.md` item 4 already records the ~2000-symbol universe as a planned
  upgrade, not in scope; the temporary goal says "full universe" and this is the
  one place the two disagree. Named here rather than silently resolved.
- It does not verify Upstox's intraday margin rates or the 15:15 square-off time
  against the broker's own schedule. Same standing as `carry-replaces-funding`.

## How it is verified

Not by tests alone -- by the market. The three segments each reaching a paper fill
at Monday's open is the only thing that proves it, because RL-071 means the
decision path cannot be driven from replay (measured 2026-09-04). Before then:
all three blueprint checkers clean, the full suite, the real-subprocess launch
test, and a live restart showing every part reporting with the per-segment
counters non-zero for three segments rather than one.
