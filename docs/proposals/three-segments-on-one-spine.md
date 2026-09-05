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

---

## What was actually built, 2026-09-05

The fan-out landed as described above (`0040555`, `b0a2d37`). Three things the
work turned up that this proposal did not anticipate, each recorded because the
reasoning is worth more than the change.

### An unattributed payload takes the only level there is

Splitting every money level per segment made an order that names **no** segment
match none of them, and the end-to-end chain went silent: `bounded-order` 0,
`order-request` 0, `fill` 0, every part alive, nothing reporting a fault.

`runtime.input_assembly.level_for_segment` is the rule, and it is deliberately
asymmetric. A payload naming its segment gets that segment's level and no other
-- falling back would size one bot's order against another's account, which is
the whole reason the levels were split. A payload naming none gets the level only
when exactly one is published, because then there is nothing to be wrong about.
More than one and it returns None, and the caller refuses by its own name.

### A helper must read the settings directory its part was given

`read_segment_setting(segment, name)` resolved the operator's directory through
`settings_directory()`, while the part itself had been launched against a
different one. On the live spine they are the same, so nothing showed it; in a
test that copies the settings the part read the copy for its own scope and the
operator's for its segment's. `PartContext.settings_root` is where a part's own
directory now comes from, and every segment helper takes it.

### The cash-equity universe is a rule, not a list

The operator's instruction: the intraday cash bot trades every NSE share the
derivatives segments do not already cover, so no two bots hold one underlying --
separate segments have separate risk limits, and one name in both is exposure
nothing bounds.

Measured against the real NSE master the same day:

    ordinary shares (instrument_type EQ, security NORMAL)   2,654
    of those, F&O underlyings                                 210
    the cash-equity segment's own universe                  2,444

`EquityWithoutADerivative` states it and `broker-symbol-universe-bridge`
evaluates it against the broker's own master. Never a list: 2,444 names typed
into a settings file are fiction the day NSE adds an F&O name, and nobody can
audit them. The join is exact rather than by symbol string -- a derivative's
`underlying_key` **is** the share's own `instrument_key`, and all 210 matched.

Two decisions inside it worth not undoing:

- **Nothing is published until the master has been heard through once.** It is
  restated over a 30-minute cycle, so a share whose options have not been spoken
  yet reads as having none -- and publishing an F&O name into the cash segment
  for half an hour is exactly the double exposure the rule prevents. A full cycle
  is detected by the first listing coming round again.
- **Ordinary means `EQ` and `NORMAL`.** That excludes 242 BE-series shares, which
  are trade-for-trade and cannot be traded intraday at all, 558 SME listings, and
  every sovereign gold bond, government security, treasury bill and NCD sharing
  the NSE_EQ segment. An intraday bot holding any of them places orders the
  exchange rejects.

**What this does not yet do, and it is the next piece of work.** The derived
shares reach the feed, the tape and `instrument-selector`, but not
`symbol-price-frame` -- `broker-underlying-price-frame-bridge` frames only the
segments' *stated* underlyings. The detectors consume price frames, so the cash
bot cannot yet form an opinion on any of the 2,444. Extending the bridge is one
change; what makes it a measured decision rather than a switch is that
`correlation-cluster-mapper` and `cointegration-pair-finder` are all-pairs over
whatever is framed. Measured 2026-08-26: 2,211 pairs cost 76% of a core, and
pairs grow with the square, so 2,444 symbols is about 3.0 million pairs. The fix
is a pair budget in those two parts, not a smaller universe.
