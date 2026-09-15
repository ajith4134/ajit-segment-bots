# Expiry-day far strikes

**Origin:** 2026-09-15, the operator: "fix the zero to hero subscription, you decide the strikes".

## What was measured

`expiry-day-zero-to-hero-detector` fired nothing on NIFTY's 2026-09-15 expiry. The universe held
the 8 contracts nearest the money per underlying (220 underlyings, 1,980 of the connection's
2,000 keys), so the detector's only expiring contracts were NIFTY 23250-23400, and 156,150 of
its checks refused `premium_too_high`.

On NSE's own intraday chart for NIFTY 15 SEP 26 at 12:20 IST, spot 23,326
(`measurements/2026-09-15-zero-to-hero-strikes/nifty-15sep-premiums-at-0650.txt`):

| side | first strike at Rs5 or less | five strikes further | beyond |
|---|---|---|---|
| CE | 23600 (+1.17%), 3.65 | 23800, 1.15 | Rs0.55-0.85 floor |
| PE | 23100 (-0.97%), 2.90 | 22900, 0.80 | Rs0.35-0.55 floor |

A far strike must be chosen before it is subscribed, so before Upstox states its delta.
Black-Scholes with the index's own at-the-money implied volatility and **calendar seconds to the
15:30 close** reproduced Upstox's stated delta to a median 0.004 over 69 samples; trading
seconds missed by 0.087, calendar seconds to the master's 23:59:59 expiry stamp by 0.064
(`upstox-delta-time-basis.txt`).

## What changes

On the day an index expires (IST), `broker-symbol-universe-bridge` adds, per side, the
`expiry_day_far_strikes_per_side` (5) strikes nearest the money whose estimated |delta| is inside
the detector's own `zero_to_hero_maximum_abs_delta` (0.10). At the measured moment that is
CE 23500-23700 and PE 23150-22950.

- The bridge consumes `broker-option-greeks` for implied volatility only. With none yet for an
  expiring index it adds nothing and counts `expiry_day_underlyings_waiting_for_implied_volatility`.
- Only spare connection keys are spent: capacity is the adapter's declared FULL-mode limit, shared
  evenly across indices expiring that day, and `expiry_day_far_strikes_trimmed_by_capacity` says
  what did not fit. The feed stops at the cap in universe order, where index keys sort after every
  share, so exceeding it would drop indices first.
- New settings `expiry_day_far_strikes_per_side` and `option_delta_seconds_per_year`, each with its
  measurement.

## What it does not do

It does not change the detector's premium or delta rules, the ATM chain width, or any other
underlying's contracts. The nearest far strikes sit where skew puts Upstox's delta just outside
0.10; the detector judges that, not this part. Monthly expiries with several indices get fewer
strikes each until a key budget beyond the spare 20 is decided.
