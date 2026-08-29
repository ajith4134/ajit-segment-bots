# The watchlist can rotate toward movement, not just liquidity

**Proposed 2026-08-29, after the operator asked whether the ~101-symbol
watchlist could cover more of the universe instead of sitting fixed on the
same highest-volume names while they show no movement.**

## What's already true, and what isn't

`symbol-catalogue-reader` already re-reads each venue's full listing every
`symbol_catalogue_refresh_interval` (900s) and re-picks the top
`captured_symbol_count` (50 per venue) by 24-hour quote volume -- so the
watchlist already drifts as volume rankings shift, and a position the bot
holds is always kept regardless of its rank (fixed 2026-08-26 after STORJUSDT's
tape went dark mid-position when its volume fell out of the top 50).

What doesn't happen: the ranking is volume only. A symbol can sit in the top 50
by volume for weeks while trading in a tight range, occupying a watchlist slot
that a smaller but genuinely moving symbol never gets to fill. `select_
capturable_symbols` also hard-refused any metric other than `quote-volume-24h`
by name -- deliberately, per its own docstring: *"ordering by the wrong metric
captures the wrong symbols, which is not recoverable later."*

## Why volatility can't replace volume, only sit beside it

Volatility is a statement about a symbol's recent price history -- it can't be
known before a symbol has been captured and priced for a while, which is
exactly backwards for a metric that decides *what to capture*. What both
venues *do* already publish, on the same 24-hour ticker response volume comes
from, is a same-day high/low range -- `highPrice`/`lowPrice` on Binance's
`/fapi/v1/ticker/24hr`, `highPrice24h`/`lowPrice24h` on Bybit's `tickers` --
which is available for every listed symbol, captured or not, at no extra
request. That is the volatility proxy this uses: 24-hour range as a fraction of
last price, comparable across symbols and venues the same way quote volume is.

A pure volatility ranking would surface untradeable extremes -- a low-float
symbol that spiked 200% on no real book is not a symbol worth a slot. So the
blend floors on liquidity first: only the top `symbol_selection_liquidity_pool_
size` (300 per venue, a first estimate) by volume are eligible at all, and
*within* that pool, volume and volatility are blended by **percentile rank**,
never by raw magnitude -- volume runs to billions of USDT and a range fraction
runs from zero to a few, so adding them as they stand would let volume decide
the order by itself regardless of any weight applied to it.

## The change

A second named metric, `volume-and-volatility-blend`
(`parts/market_data_feed/symbol_catalogue_reader.py`):

    volume_percentile = rank_by_volume / (pool_size - 1)          # 0 = highest volume
    volatility_percentile = rank_by_volatility / (pool_size - 1)  # 0 = widest range
    score = (1 - w) * volume_percentile + w * volatility_percentile   # lower wins

`w` is `symbol_selection_volatility_weight` (0.35, a judgement call with no
historical run yet to tune it against). The existing refusal for an unknown
metric name stays exactly as strict -- `quote-volume-24h` and
`volume-and-volatility-blend` are the only two names accepted, anything else
still refuses rather than falling back.

`CapturableSymbol` gained one field, `volatility_24h`, carried the same way the
funding fields are: None when the venue did not price the symbol on this read,
never filled in with a guess.

## What this does not do

**It does not raise `captured_symbol_count`.** The 2026-08-25 ruling to hold at
50 per venue until the project is fully built was about the *count* -- the
fan-out cost that broke the pair scanner and the price staleness that broke
decision freshness both scale with how many symbols are captured at once, not
with which ones. This changes *which* ~101 symbols are watched, not how many.

**It is not switched on by this change.** `symbol_selection_metric` stays at
`quote-volume-24h` in `runtime.toml` until an operator flips it. Changing the
live value changes which symbols get captured starting the next refresh --
capture is one-way, so that is a deliberate step for whoever runs this to take
knowingly, not something a code change should flip on its own.
