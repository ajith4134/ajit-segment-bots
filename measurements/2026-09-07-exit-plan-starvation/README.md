# Why the exit-plan proposers build almost nothing — 2026-09-07

Measured on the live spine at 09:20 UTC, with all three segment bots running on
real NSE data:

    bull-exit-plan-proposer    2,141 requested     290 built
    bear-exit-plan-proposer   68,443 requested      43 built

    refused: too-few-prints-in-the-window-to-measure-a-range   57,702  (84%)
    refused: no-excursion-record-for-this-symbol               10,698

Downstream this is why nothing can be sized: an intent with no exit plan carries
no stop, and `position-sizer` counted `missing_stop_price` 17,136 of 19,667
actionable intents with `stop_from_refined_plan` **0** and `sized` **0**.

## The bar, and why it is not the thing to move

`bull_cold_start_minimum_prints` / `bear_cold_start_minimum_prints` are 20, with
this provenance: *"Measured on the tape 2026-08-22: BTCUSDT prints about 4 a
second, so a 60s horizon carries roughly 240 — the bar binds on quiet symbols,
which is exactly where it should."*

That is a crypto measurement. On this project's own tape for 2026-09-07, 252
option contracts with ≥100 prints:

| horizon | prints in window (p25/p50/p75) | contracts reaching 20 |
|---|---|---|
| 60 s | 2 / 5 / 26 | 35.3% |
| 300 s | 7 / 20.5 / 110 | 50.8% |
| 900 s | 17 / 45 / 269 | 69.4% |

The median NSE option contract puts **5 prints** inside a 60-second horizon
against BTCUSDT's 240. So the bar refuses most requests.

**Lowering the bar is the wrong fix, and the measurement says so.** Holding the
window fixed at 300 s and subsampling n of the prints inside it — which isolates
sample size from span — the share of the window's true range a sample of n
recovers, over 159 contracts:

| prints | p25 | p50 |
|---|---|---|
| 3 | 19.2% | 42.9% |
| 5 | 40.0% | 60.5% |
| 8 | 52.7% | 72.9% |
| 10 | 61.5% | 80.0% |
| 15 | 71.1% | 88.0% |
| **20** | **77.8%** | **92.9%** |
| 30 | 85.7% | 100.0% |

Twenty prints recovers 92.9% of the range at the median. Ten recovers 80%, and
77.8% at p25. The range is what the **stop distance** is measured from, so an
under-measured range is a stop placed too tight — the dangerous direction. Moving
the bar to 10 would place stops roughly 20% too tight at the median and 38% too
tight on a quarter of contracts, and it would do it invisibly.

So the crypto bar is statistically sound and survives the pivot unchanged. What
does not survive is the **horizon**: 60 seconds was the span over which a crypto
perpetual printed 240 times, and it is the span over which an NSE option prints
5. The detector claims the horizon and the proposer measures the range over
exactly that span (T-4, correctly), so the number to re-derive is the detectors'
own horizon — not this bar.

## What to do next, in order

1. Re-derive the detectors' claimed horizons from Indian print rates. At 300 s
   the median contract clears the bar; at 900 s, 69% of contracts do.
2. Or correct the small-sample bias in the range estimator itself, so a range
   from 5 prints is reported as the underestimate it is rather than used raw.
   The table above is the correction factor, already measured.
3. `no-excursion-record-for-this-symbol` (10,698) is a different and healthier
   refusal: it fills in as trades close, and `symbols_with_an_excursion_profile`
   was already 537.

Neither was done on 2026-09-07: both change what every stop in the system is
placed from, and doing that from a number picked at the end of a session — rather
than derived and then watched for a session — is the failure this project keeps
writing measurements to avoid.
