# Two option segments, on a universe nothing types by hand

**2026-09-12.** The operator: *"Lets focus only on option index and option stocks
full universe and retire the intraday cash"*, then, while it was being built:
*"make sure notin isard coded and detects auto maticly every tme"*.

## What "full universe" can and cannot mean

Measured on Upstox's real instrument master (118,388 rows):

| | |
|---|---|
| index option underlyings | **10** — NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, NIFTYFPI, FOCIT, SENSEX, BANKEX, SENSEX50 |
| stock option underlyings | **210** |
| option contracts, all expiries | **36,178** (27,012 stock, 9,166 index) |
| contracts in the nearest expiry alone | **13,738** |
| what one connection accepts | **2,000 instrument keys**, and nothing evicts |

So the full universe of *contracts* is not subscribable and never was. The full
universe of **underlyings** is: 220 of them, with a bounded chain on each.

    220 underlying keys + 220 x 8 contracts = 1,980 of 2,000, headroom 20

Eight is four strikes either side of the money on the nearest expiry, ranked by
distance from it — which is where the volume is. Measured 2026-09-04: only 76 of
891 traded NSE_FO instruments had traded often enough to fill a detector window,
and the ones that had not are the contracts far from the money.

## Nothing is named by hand

Both segments' `segment_universe_selection` is a **rule**, evaluated against the
broker's own master every time it is restated:

| segment | rule | what it admits |
|---|---|---|
| `index-options` | `every-nse-index-with-an-option` | an NSE_INDEX or BSE_INDEX listing some CE/PE contract names as its underlying |
| `stock-options` | `every-nse-stock-with-an-option` | an ordinary NSE share (instrument_type EQ, security_type NORMAL) some CE/PE contract names as its underlying |

This follows the precedent `cash-equity-intraday` already set with
`every-nse-share-without-a-derivative`, and `StockWithAnOption` is that rule's
exact complement over the same two facts. The reasoning is the one that note
already gives: **210 trading symbols typed into a settings file is fiction the
day NSE revises the F&O list, and nobody can audit it.** The same is true of ten
index names the day an exchange lists options on an eleventh.

**MCX is not blacklisted; it excludes itself.** MCXBULLDEX carries options and
sits in `MCX_INDEX`, and the rule admits `NSE_INDEX` and `BSE_INDEX`. A
blacklist would need editing the day MCX lists a second index, which is the
thing this change removes.

Each segment's stated `segment_underlying_trading_symbols` stays in its file as a
**fallback** and is no longer the universe. The bridge's constructor still
refuses an empty seed, because an empty universe is what let
`universal-symbol-sweeper` run 3,114 sweeps over nothing on 2026-09-04 with every
skip counter reading zero.

### Where the second half of each rule is answered

`admits` decides only "is this the right kind of listing", because the other half
— *is an option written on it* — needs the whole master, and a share arrives
before or after its contracts depending on where the catalogue conveyor is. So
candidate listings are held, and `promote_shares_an_option_is_written_on()`
completes the rule each tick against `_derivative_underlying_keys`. It is
idempotent and converges: once promoted, a listing is skipped.

## The feed reader had to change, or none of this would have worked

`only_what_the_segments_trade` returned **every** nearest-expiry contract of every
tracked underlying, unranked, and `plan_subscriptions` took whatever fit in
listing order. At 3 indices and 14 shares that was 1,528 contracts against 2,000
keys and the slack absorbed it. At 220 underlyings it is **13,738**, and listing
order has no relationship to what is worth subscribing.

That function holds no prices, so it cannot rank by distance from the money.
`broker-symbol-universe-bridge` does, and already publishes each chain capped and
sorted. So the division of labour is now: **the feed reader selects the
underlyings, whose prices are what make a ranking possible at all, and
`subscribe_the_universe_first` supplies the ranked contracts ahead of everything
else.** The remainder is counted on the standing as
`contracts_left_to_the_universe` rather than dropped silently.

The cost is that no option is subscribed until the bridge has priced its
underlying, which is the bootstrap the caller already documented. The alternative
has already happened, on 2026-09-06: **a slot spent on the wrong instrument is
spent for good**, and the connection filled with SILVERM, GOLD, USDINR and JPYINR
while NIFTY's chain waited.

## cash-equity-intraday is retired, not removed

The operator chose settings-off over deletion. It is gone from `built_segments`
and its file carries a banner saying so; **every part that serves it stays built
and unchanged** — `cash-equity-shortlist-ranker`, `equity-opportunity-profiler`,
`intraday-square-off-placer`, `leverage-selector`. What turns it off is its
absence from that list: `any_segment_takes_shares_without_a_derivative` now
answers false, so no equity universe is derived and those parts go idle rather
than missing. Bringing it back is one line and a restart.

Its ₹5,000,000 allotment was split between the two options segments, which hold
**₹7,500,000 each** now. Total capital at risk is unchanged at ₹1.5 crore.

## What this does not settle

**`minimum_capital_per_trade` and the exchange's freeze quantity together impose
a floor on the option premium each segment can trade**, and the two settings were
each chosen without the other in view. NIFTY's single-order limit is 1,755 units,
so ₹100,000 ÷ 1,755 ≈ **₹57**. Replaying the real 2026-09-08 tape after the
freeze limit was enforced, index-options could open **4 of 8** contracts; the
four refused read:

    NIFTY 23600 PE  27 whole lot(s) commit 20,182.50, below this segment's
                    minimum_capital_per_trade of 100,000.00 -- capped at the
                    1755 units this exchange takes in one order

This is a real constraint, not a bug — and the contracts it excludes are exactly
the cheap out-of-the-money ones that were 91.4% of every rupee this project has
lost (`no-order-larger-than-the-exchange-accepts.md`). But it is the operator's
call whether to lower `minimum_capital_per_trade` so more of the chain is
reachable, or to leave it and trade only the liquid middle. **Nothing should
change it without that decision.**

Two smaller ones:

- The 220-underlying universe puts ~1,980 priced symbols in front of
  `cointegration-pair-finder` and `spread-reversion-detector`, which grow with
  the **square** of the universe. `runtime/pair_sweep.py` already yields pairs
  lazily from a cursor and never materialises the list, so this does not blow up
  — but pair *coverage* becomes very thin, and that is worth measuring before
  either detector's output is trusted at this width.
- Headroom is 20 keys of 2,000. A held position is forced into the universe
  regardless of ranking (correctly — an unpriced open position is how ten
  stock-options trades went unpriced for their whole life on 2026-09-08), so a
  session with many open positions can exceed the cap. The feed stops at the cap
  rather than erroring, but what it drops there is not chosen.

## Verification

- `tests/parts/market_data_feed/test_the_option_universes_are_derived_not_typed.py`
  — six cases against the real master. Both rules are exercised with a seed that
  deliberately names nothing any segment trades, so everything found is derived:
  **220 underlyings, 10 indices and 210 shares, 1,980 keys, 8 contracts on every
  one of the 220.**
- `tests/parts/broker_adapter/test_broker_market_feed_reader.py` — five cases
  updated to the new division of labour.
- `tests/runtime/test_segment_settings.py` — the live-settings tests now read
  `built_segments` rather than naming three by hand, so a retirement cannot fail
  them for the wrong reason.
- 4,220 tests pass; all four checkers pass. (`tests/runtime/test_open_web_sources.py`
  fails intermittently on live calls to arXiv and GitHub — a file this work never
  touched, and it passed on the preceding run.)
- The spine restarts clean and both changed parts report live:
  `contracts_published` **1,760** = 220 × 8, subscriptions climbing past 1,366 as
  the catalogue conveyor completes its cycle.
