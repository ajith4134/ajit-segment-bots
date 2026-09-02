# broker-history-reader — prices for the hours the market is shut

**Proposed 2026-09-02.** Phase A's paper trading runs on live prices while the
market is open and on history while it is not — the user's own words the same
day:

> no when market is closed only then historic data, but if the market is open
> then on the live data paper trades

Nothing in the deciding half changes. The bull bot, the arbiter, the sizer and
the fill path see candles on the wire they already read, and no part learns
where they came from (T-4).

## Why this does not contradict RL-071

RL-071 says a tape replay is never what a *live run's* trading decision or
learning is made from. The ruling exists so a bot that could be reading real
prices is never fed recorded ones instead. A replay that runs **only when there
is no live market to read** is not standing in for anything, and
`market-session-state` is exactly the fact that tells the two situations apart.
What stays forbidden: replaying while the market is open, and letting a replayed
fill be recorded as a live one.

## What it produces, and the one thing it must not

It produces **`candle`**, not `broker-candle`.

That is the whole design decision, and it is about the tape. `broker-candle` is
consumed by `broker-market-tape-writer`, whose job is to record what this machine
actually received live. Publishing history there would write replayed bars into
the live capture, and a tape that mixes the two is worse than no tape — every
measurement taken over it afterwards would be quietly wrong, and nothing would
say so.

`candle` is consumed by `kline-window-builder`, `historical-bar-store` and
`feed-jump-detector` — precisely the parts that should see history — and by
nothing that records live capture. So history joins the circuit *after* the tape,
which is the correct place for it.

## What it reads

- `broker-instrument-listing` — the instrument keys and their trading symbols.
  Expired contracts are absent from Upstox's master (verified 2026-09-02:
  earliest listed expiry was 2026-09-08), so history is fetched for
  **currently listed** nearest-expiry contracts over the days they have already
  traded. That is enough, and it avoids needing an expired-contract vendor.
- `broker-token-standing` — the same token the live feed uses.
- `market-session-state` — it fetches only while the session is not open. When
  the market opens, the live feed is the source and this part has nothing to do.

## The facts it is built against

Verified against the live API on 2026-09-02, not from documentation alone:

    GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}

385 one-minute bars for NSE_FO|42654 (NIFTY 24350 PE 08 SEP 26) across
2026-09-01, 09:15 to 15:39 IST, ascending after the reader's own sort, every one
closed, each carrying open interest as well as OHLCV.

- Minute data begins **January 2022**; one month per request for 1-15 minute
  intervals. Both are bounds a caller plans requests against rather than
  discovers by being refused.
- Rows arrive **newest first** and are stamped **+05:30**. `UpstoxAdapter.
  read_historical_candles` converts both once, at the edge (commit `fa92a4b`).

## What it is not

Not a backtester. It publishes prices; what the bots do with them is the same
thing they do with live prices, judged by the same parts. The backtesting block
keeps its own job — walk-forward splits, look-ahead auditing, replay scoring —
and this part is not a substitute for any of it.
