# broker-candle-bridge

Given by the user 2026-09-02: "market is open," asking to get the live spine
running on real Upstox data. Cutting the crypto venue-adapter cluster
(`ccxt-venue-reader`, `venue-trade-stream-reader`, and the rest) out of
`operate/run_live_spine.py` would have silently taken `kline-window-builder`
-- and through it `kronos-forecaster`, `kronos-finetuner`,
`bull-conviction-model`, `bear-conviction-model`, `volatility-feature-
builder`, `forecast-distribution-gate`, `symbolic-hypothesis-miner` -- to
zero input, since no Indian producer of `candle` existed. Unlike
`market-data`/`order-book-snapshot`/`symbol-price-frame`, this bridge did
not exist yet. Built before the cutover rather than after, so the
conviction-model chain never goes dark.

Republishes Upstox's `broker-candle` (`BrokerCandle`) as `candle`
(`NormalisedCandle`, the real wire type `kline-window-builder.start_part`
already filters for). Three fields Upstox's OHLC entry does not carry,
each handled honestly rather than guessed:

- **`is_closed`**: Upstox restates the forming bar on every tick (`.proto`
  has `ohlc` as a repeated field, same behaviour as Binance/Bybit) but
  states no closed/live flag on any entry (verified against the committed
  `.proto` -- `runtime/brokers/upstox.py`'s own `_read_ohlc` comment).
  Binance's `k.x` / Bybit's `confirm` are read from the venue; here it is
  computed instead, from the only fact Upstox does give: a bar is closed
  once wall-clock time has passed its own interval past its open time.
  `interval` codes are Upstox's documented ones (upstox.com/developer/
  api-documentation/v3/get-market-data-feed, fetched 2026-09-02: "1d for
  daily, I1 for 1minute"), generalised to `I<n>` = n minutes, `<n>d` =
  n days. An interval outside that pattern is refused, not guessed.
- **`quote_volume`**: no per-bar turnover field exists on `BrokerCandle` at
  all. `close * volume` is a documented approximation (RL-061), never
  presented as the real sum of price*quantity a turnover-reporting venue
  would give.
- **`trades`**: `None`, not a fabricated `0` -- the exact shape
  `NormalisedCandle.trades` already carries for Bybit, whose kline stream
  also sends no count (see that field's own docstring: "None when the
  venue does not send a count... zero would read as a minute in which
  nothing traded").

10/10 tests pass, all against real Upstox interval codes and OHLC field
shapes, never an invented one (RL-063).

**Not done here:** the actual `LIVE_SPINE` cutover (removing the crypto
venue-adapter cluster, wiring the 10 broker-adapter parts in). This bridge
is the prerequisite that makes that cutover safe; the cutover itself is a
separate step.
