# The conviction floor was pinned at 1.0 by a name, not by a number — 2026-09-08

The previous session left this open as a design question. It was not one. It was
`expiry-day-zero-to-hero-detector` publishing Upstox's `instrument_key` as the
candidate's `symbol` while every part downstream looks a price up by
`trading_symbol`.

## What was measured on the live spine, before the fix

    bull-opinion-composer     2,009 opinions, 2,009 stood down, last_floor 1.0
                              stood_down_by_reason.conviction-below-threshold 2,009

    bull-setup-filter         accepted 498,952
                              accepted_by_detector.expiry-day-zero-to-hero-detector 494,125  (99.0%)

    bull-exit-plan-proposer   plans_requested 218,520      plans_built 837
                              refused: no-price-for-this-symbol 216,227  (99.0%)

    bull-entry-timer          decisions 498,056
                              stood_down: no-price-for-this-symbol 493,388  (99.1%)

Three parts refusing 99% of their work for the same reason, and one detector
raising 99% of the flow. The detectors' candidate symbols, read from the live
trade-lifecycle journal that session:

| detector | symbols it names |
|---|---|
| `expiry-day-zero-to-hero-detector` | `NSE_FO\|42553`, `NSE_FO\|42555`, … |
| `mean-reversion-detector` | `LT`, `NIFTY 23500 PE 08 SEP 26`, `HINDUNILVR` |
| `spread-reversion-detector` | `ICICIBANK`, `MARUTI 13600 CE 29 SEP 26` |
| `volatility-gap-detector` | `ICICIBANK`, `ITC`, `KOTAKBANK`, `SENSEX` |

One of the four names contracts by the broker's key. `market-data`,
`symbol-price-frame` and the tape are all keyed by `trading_symbol` —
`broker-market-data-bridge` resolves the key on the way in, holding an update
until its listing arrives — so an instrument_key is a name none of them holds a
price under. The 837 plans that were built came from the other three detectors.

## Why that pinned the floor rather than merely starving it

The 837 plans that survived were on **underlyings** — stocks and indices —
whose live range is small, and `ConvictionFloor` was judging them against
`per_side_trading_cost_fraction`, the cost of trading an **option**:

    floor = (1 + 2 x 0.004266 / stop_fraction) / (1 + 1.8)

`reward_to_risk` is structurally 1.8 in the cold start — `_cold_start_targets`
places every target at a multiple of the stop, so the stop cancels out of the
weighted reward (1x0.4 + 2x0.4 + 3x0.2). The floor therefore moves with the stop
alone, and reaches 1.0 whenever `stop_fraction <= 0.474%`. The proposer's own
standing said `widest_live_range_fraction` **1.01%** across all 837 plans, so at
the 1.5x multiple the widest stop it ever placed was 1.52% — a floor of 55.8%
against a model reporting 50.0% (`challenger.positives` 48,202 of 97,420). Not
one plan could be acted on, and most were pinned at 1.0 outright.

## The instruments the detector was actually naming clear that floor easily

The 83 contracts it raised candidates on that session, measured off this
project's own tape for the day, over the 3,600 s horizon it claims:

    83 of 83 have a tape;  21 reach the 20-print bar in the window
    range p50 40.4%   floor p50 36.2%   uncrossable 0 of 21   crossable at p=0.50 21 of 21

    NSE_FO|42631  prints=4,322  range=40.37%  floor=36.2%
    NSE_FO|42633  prints=4,309  range=39.34%  floor=36.2%
    NSE_FO|42614  prints=4,289  range=26.79%  floor=36.5%

An expiry-day option moves tens of percent in an hour, so its stop is wide, the
round trip is small in units of that risk, and the floor lands near a third. The
floor arithmetic was never wrong. It was being handed the range of the wrong
instrument, because the right one was named in a namespace nothing could price.

A random sample across the whole tape, for scale (the script in this directory):

      option contracts   3600s  n=   8  range p50   5.45%  floor p50  39.6%  uncrossable 0
      equities           3600s  n=  18  range p50  12.22%  floor p50  37.5%  uncrossable 2
      equities            300s  n=  10  range p50   2.04%  floor p50  45.7%  uncrossable 1
      equities             60s         no symbol reached 20 prints

The 20-print bar over a 60 s window remains the separate, already-measured
starvation of `measurements/2026-09-07-exit-plan-starvation/`, and this fix does
not touch it.

## The fix

`expiry_day_zero_to_hero_detector.detect` names the candidate by
`listing.trading_symbol`, keeps `instrument_key` in the candidate's evidence, and
refuses with `listing-carries-no-trading-symbol` rather than falling back to the
key — a fallback would republish the defect silently.

The test fixture set `trading_symbol=instrument_key`, which is why 17 passing
tests never saw this. It now builds a name that differs from the key, and a new
test drives the detector over the real captured NSE instrument master and asserts
every candidate is named by the contract's own `trading_symbol` and never by
anything shaped `NSE_FO|…`.

## What this does NOT settle

The design question the previous session raised is still open, and still real for
the other three detectors: a `volatility-gap-detector -> ICICIBANK` candidate has
its stop measured in the **stock's** price range while the floor charges it the
**option's** round-trip cost, because that view is expressed through an option by
`instrument-selector` after the plan is built. That mismatch errs toward
refusing, and it now governs a minority of the flow rather than all of it.
