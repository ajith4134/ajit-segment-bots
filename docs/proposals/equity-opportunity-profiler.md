# equity-opportunity-profiler

**Proposed 2026-09-05.** Fetches each ordinary NSE share's own 52-week high,
52-week low, average daily volume, average true range and prior close from
Upstox's v3 historical-candle endpoint -- the history
`cash-equity-shortlist-ranker` needs and nothing on the spine currently reads.

## The gap

The operator asked (2026-09-05) for the day's top 50 cash-equity names, picked
by momentum, volume, 52-week range and more, instead of scanning every one of
the 2,444 ordinary shares `broker-symbol-universe-bridge` currently publishes
unranked and uncapped. Momentum and today's volume are already on the wire
(`broker-price-frame`, `candle`); a 52-week range and an average day are not.
`broker-history-reader` calls the same Upstox endpoint, but for a different job
-- filling the hours the market is shut (RL-071) -- and deliberately reaches
back only `broker_history_most_days_back` (1 day), never a year.

## What it does

Consumes `broker-instrument-listing`, filters to ordinary shares with
`EquityWithoutADerivative.admits` (`broker_symbol_universe_bridge.py`, reused
rather than restated -- T-6), and rotates a bounded slice through Upstox's
historical-candle endpoint, one window per due tick, the same shape
`broker-history-reader`'s own sweep uses. Publishes `equity-historical-profile`
per symbol: `week_52_high`, `week_52_low`, `average_daily_volume`,
`average_true_range`, `previous_close`, plus `upper_circuit_limit`/
`lower_circuit_limit` (always `None` in this build -- see below).

A window's key includes today's date, so a symbol profiled today is not
re-fetched again until tomorrow -- the sweep naturally idles once every
candidate has a fresh window, and the next calendar day reopens all of them at
once, the identical trick `broker-history-reader` already relies on.

It does not know what a segment is, what an F&O underlying is, or that a
shortlist exists downstream (T-4): "ordinary NSE share" is a fact about one
listing, nothing more.

## What goes in it, and why it is bounded

`equity_profile_symbols_per_sweep` bounds one sweep's requests, the same
reasoning `broker_history_most_instruments` bounds
`broker-history-reader`'s: a burst against Upstox's rate limit buys nothing the
next due tick would not, and the historical-candle payload for one symbol's
year of daily bars (~250 rows) is small next to a chain's minute bars.

## What this does NOT fix

- **Circuit limits are not fetched.** Neither the instrument master nor any
  live feed this project reads carries `upper_circuit_limit`/
  `lower_circuit_limit` (checked 2026-09-05). The field is named on the payload
  so `cash-equity-shortlist-ranker`'s shape does not have to change again once
  a real fetch exists; it is not approximated with an invented percentage
  (RL-061).
- **NSE delivery percentage is not fetched.** Attempted 2026-09-05 against
  `archives.nseindia.com`'s bhavcopy: refused with HTTP 503 by Akamai's
  bot-protection even with a warmed browser-header session -- a real,
  verified block, not a missing package (Rule 3). Building this reliably needs
  either a headless-browser fetch or a paid vendor, named here as future work
  rather than built as something fragile.
- **F&O exclusion is not this part's job.** Whether a profiled share is also an
  F&O underlying is decided where cash-equity's universe is actually published
  (`broker-symbol-universe-bridge`), not here.

## Contract

```
consumes  broker-instrument-listing, broker-token-standing
produces  equity-historical-profile, part-health
resource_class      io-bound
rate_risk           latency-only
skipped_tick_effect delays
```

## Settings

| setting | meaning |
|---|---|
| `equity_profile_days_back` | how far back one historical window reaches (days) |
| `equity_profile_atr_window_days` | how many trailing daily bars the average true range is taken over |
| `equity_profile_symbols_per_sweep` | how many symbols one due tick asks Upstox for |
| `equity_profile_fetch_interval_seconds` | how often a sweep is due |
