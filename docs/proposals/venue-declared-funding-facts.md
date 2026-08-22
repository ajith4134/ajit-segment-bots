# The funding rate is a listing fact, and the selector should read the listing

**Proposed 2026-08-22, after the trading half was wired and stopped on one number.**

## What stopped

`test_an_intent_becomes_a_paper_fill` is `xfail(strict)` for a single reason:
`instrument-selector` refuses a perpetual whose carry it cannot price, and a
perpetual's carry is its funding rate multiplied by how many settlements fall
inside the intent's horizon. Nothing in the blueprint publishes either number, so
`carry_over` returns `None`, the only listed instrument is rejected, and the
choice comes back `no-listed-instrument-can-express-this-intent`.

The refusal is correct. Funding is real money — 0.01% per settlement, three
settlements a day, is 10.95% a year against a position, which is larger than most
of what the bot is trying to capture. A carry invented at the point of use would
make a perpetual look free and would be indistinguishable, in the record, from a
perpetual that genuinely was.

## Why the part is currently guessing at all

`instrument-selector.observe_price` registers the perpetual **from the fact that
the symbol printed a trade**, with `funding_rate_per_settlement=None`, and says so
in its own docstring: *"registered from the fact that it traded rather than from a
catalogue this part does not consume"*. That was the honest thing to do while
nothing carried a catalogue to it. It is still an inference: a symbol trading is
evidence that some contract exists, not a statement of that contract's terms.

The venue states the terms. Both of ours do, and neither statement is being read.

## What both venues actually publish — measured 2026-08-22

| | binance-usdm | bybit-linear |
|---|---|---|
| rate per settlement | `lastFundingRate` on `/fapi/v1/premiumIndex` — **875 of 875 listed symbols carry one** | `fundingRate` on `/v5/market/tickers`, **the response the reader already fetches** |
| settlement interval | `fundingIntervalHours` on `/fapi/v1/fundingInfo` — 760 entries; 4h for 444, 8h for 314, 1h for 2 | `fundingInterval` in minutes on `/v5/market/instruments-info`, **the catalogue response the reader already fetches** |
| BTCUSDT, checked | rate `0.00010000`, interval 8h | rate `0.0001`, interval 480 minutes |

Bybit costs **no extra request**: both facts are already in the two responses
`symbol-catalogue-reader` fetches every refresh and throws most of away. Binance
costs **two extra REST calls per refresh** against a catalogue interval measured in
minutes, which is nothing beside the 872-symbol `exchangeInfo` call beside it.

**132 of Binance's 872 listed symbols are absent from `fundingInfo`.** Their
interval is not declared, so they get `None` and the selector refuses them by name
— it does not fall back to the documented 8-hour default. An assumed interval is a
carry cost off by a factor of two on every 4-hour symbol, and the venue's own
`/fapi/v1/fundingRate` history settles any one of them in one request the day a
symbol we want to trade turns out to be in that set.

## The change to the blueprint — one edge

    instrument-selector: consumes += symbol-universe

`symbol-universe` is already defined as *"every symbol the segment can trade right
now, as listed by the venue"*, and venue-declared contract facts already travel on
it: `price_increment` rides there and is what `tick-size-resolver` reads. Funding
is the same class of fact about the same contract, from the same response, and it
is what makes a listing priceable rather than merely present.

No new part. No new data type. No new producer. The selector stops inferring which
instruments exist and reads the list of them, which is what a selector should have
been doing.

### Why not the alternatives

- **`funding-rate-forecaster` → `funding-forecast`.** A forecast is where the rate
  is *headed*; carry over a 60-second horizon is settled by the rate the venue has
  *declared*. Using a prediction where the venue publishes a fact would be worse
  data, not better — and the forecaster would still need the declared rate as the
  thing it forecasts from. It stays exactly where it is, for the part of the
  problem that is genuinely a forecast: a position held across a settlement whose
  rate has not been fixed yet.
- **Carry it on `liquidity-grade`.** The selector already consumes it, so it would
  need no blueprint edit — which is the entire argument for it. `liquidity-grader`
  consumes `order-book-snapshot` and `market-data` and has no way to know a funding
  rate; the type would have become "liquidity and also whatever else the selector
  needed", which is how a data type stops meaning anything.
- **A settings entry.** Funding changes every settlement. An operator-set number
  would be stale within hours and would carry the operator's name on a figure the
  venue owns.

## What this does not claim

That the resulting trade is good. It closes the one gap between an intent and a
fill, and the first fill proves the circuit conducts — nothing about whether the
number it produces is worth having.
