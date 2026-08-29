# Don't just find what moved -- find what is moving

**Proposed 2026-08-29, immediately after `rotate-the-watchlist-by-volume-and-
volatility.md`, once the operator pointed out its actual flaw: a 24-hour range
is wide for a symbol whose move happened 20 hours ago and has since gone
quiet, exactly as it is for one moving right now. Ranking by it alone still
picks yesterday's mover.**

## The gap in the first fix

`volume-and-volatility-blend` ranks a volume-qualified pool by 24-hour range as
a fraction of price. That range is a single number for the whole day -- it
cannot distinguish a symbol that spiked once, eight hours ago, and has sat flat
since, from one that is moving in the last fifteen minutes. Both show the same
wide 24-hour range. The first is exactly the case the operator was trying to
stop picking.

## What the exchanges actually let you check, verified 2026-08-29

Researched against primary sources (official connector source for Binance, the
Bybit v5 docs), not remembered or summarised:

- **Binance USDⓈ-M has no bulk endpoint shorter than 24 hours at all.**
  `GET /fapi/v1/ticker/24hr` takes no `windowSize` parameter -- confirmed
  against `binance/binance-futures-connector-python`'s `market.py`; that
  parameter exists on Binance's own Spot API and was never added to futures.
  Nothing else in the futures market-data surface bulk-serves a shorter window
  either. The only way to see 5m/15m/30m/1h is `GET /fapi/v1/klines`, **per
  symbol**.
- **Bybit's bulk ticker states one extra figure**: `prevPrice1h`, a raw price
  level from an hour ago -- enough for a whole-universe 1-hour net change, and
  nothing finer. A flat 1-hour net change cannot tell a fresh spike from a
  spike that already reversed within the hour, so it is a weak signal on its
  own (wired in as `symbol_selection_momentum_weight`, kept small).
- **Both venues' per-symbol kline call is the only way to see 5m/15m/30m at
  all**, and doing that for the whole ~1,700-symbol universe would exceed
  Binance's confirmed 2,400-weight-per-minute budget outright, before
  considering Bybit's undocumented-but-presumably-comparable limit.

## The metric: recent share of the window's own range

For a bounded, rotating slice of the liquidity pool, fetch one kline response
per symbol (`short_window_kline_interval`, `short_window_kline_count` bars --
12 five-minute bars by default, a one-hour baseline) and compute:

    baseline_range = max(closes) - min(closes)          # the whole hour
    recent_range = max(closes[-N:]) - min(closes[-N:])   # the last N bars
    acceleration = recent_range / baseline_range         # in [0, 1] by construction

A recent slice's range can never exceed the full window's it is drawn from, so
this is bounded without clamping. Close to 1 means most of the hour's movement
is concentrated in the last `short_window_recent_bars` (moving *now*); close to
0 means the hour's range happened earlier and this symbol has been flat since
-- which is precisely "already lost its momentum," named as a number rather
than left to a 24-hour aggregate that cannot see it.

## Why rotated, not scanned all at once

`symbol_selection_liquidity_pool_size` is 300 per venue. One kline call per
pool member per refresh is 300 REST calls every 900 seconds -- not obviously
unsafe, but the per-call weight for `/fapi/v1/klines` could not be confirmed
against Binance's own documentation text in this session (the docs page is a
client-rendered SPA; WebFetch returned nav shells rather than the parameter
table, the exact failure mode `~/.claude/CLAUDE.md`'s Rule 5 already warns
about for this reason). Rather than bet on an unverified number,
`symbol_selection_short_window_scan_size` bounds each refresh to a slice (60 by
default) and rotates through the pool the same way `cointegration-pair-finder`
already rotates through symbol pairs for an analogous cost reason -- one full
rotation of a 300-symbol pool at 60/refresh is 5 refreshes, 75 minutes.

## What is accepted, not solved, in this pass

**The scan's scores are memory-only.** A restart loses whatever the rotation
had built up, at a cost of up to one more full rotation (75 minutes) before
the picture rebuilds -- mild next to what a lost lot book or a lost regime
series costs, so it is not checkpointed here. Worth revisiting if restarts
turn out to be frequent enough that the acceleration signal is rarely warm.

**The whole 1,700-symbol universe still cannot be scanned for a fresh mover
outside the liquidity pool.** This fixes "the pool contains yesterday's
mover instead of today's," not "a symbol currently outside the pool is
having a fresh move right now and nobody notices." The latter genuinely has
no cheap answer with either venue's current REST surface; a websocket
mini-ticker/kline stream was the one alternative raised in research and was
not pursued here, since it swaps a REST rate-limit ceiling for a
stream-per-connection ceiling (`stream-budget-planner` exists for exactly
that reason) rather than removing the constraint.

## The change to the blueprint

None. `select_capturable_symbols` and `SymbolCatalogueReader` are internal to
`symbol-catalogue-reader`; no new part, no new data type, no new consume or
produce edge.
